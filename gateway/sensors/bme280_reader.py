"""BME280 on the Jetson I2C bus -> ab/gw/env (AIR-12, GW-06).

Wiring: Jetson header pin 1 (3.3 V) -> VIN, pin 6 -> GND, pin 3 (SDA, bus 1) -> SDA, pin 5 (SCL) -> SCL.
Address 0x76 (SDO to GND) or 0x77. Check with: sudo i2cdetect -y -r 1

Every `period_s` (2 s) it reads temperature, humidity and pressure in forced mode and publishes
  ab/gw/env {ts, temp_c, rh, pressure_hpa}
decision.py uses rh for the PM2.5 humidity correction, cause_rules for "humidity rising = cooking", and the
uploader copies it to Firebase env/latest and the history. Readings outside the sensor's range are dropped; after
`retry_s` of I2C errors it reopens the bus (a loose wire should not need a restart).

Compensation: the double-precision formulas in the Bosch BME280 datasheet (section 8.1).

Run on the Jetson from gateway/:  python3 -m sensors.bme280_reader        (service)
                                   python3 -m sensors.bme280_reader --once (print one reading)
"""
import logging
import struct
import time

from common.protocol import TOPIC_GW_ENV, now_ts

log = logging.getLogger("bme280")

CHIP_ID = 0x60
REG_ID, REG_RESET, REG_CTRL_HUM, REG_STATUS, REG_CTRL_MEAS, REG_CONFIG, REG_DATA = (
    0xD0, 0xE0, 0xF2, 0xF3, 0xF4, 0xF5, 0xF7)
DEFAULTS = {"bus": 1, "address": 0x76, "period_s": 2.0, "retry_s": 10.0, "temp_offset_c": 0.0}


class Calibration(object):
    def __init__(self, t, p, h):
        self.t, self.p, self.h = t, p, h

    @classmethod
    def from_registers(cls, block1, block2):
        """block1 = 26 bytes from 0x88 (T1..P9, then 0xA1 = H1 at index 25); block2 = 7 bytes from 0xE1."""
        t = struct.unpack_from("<Hhh", bytes(block1), 0)
        p = struct.unpack_from("<Hhhhhhhhh", bytes(block1), 6)
        b = bytes(block2)
        h1 = block1[25]
        h2 = struct.unpack_from("<h", b, 0)[0]
        h3 = b[2]
        h4 = (_s8(b[3]) << 4) | (b[4] & 0x0F)
        h5 = (_s8(b[5]) << 4) | (b[4] >> 4)
        h6 = _s8(b[6])
        return cls(t, p, (h1, h2, h3, h4, h5, h6))


def _s8(v):
    return v - 256 if v > 127 else v


def compensate(cal, adc_t, adc_p, adc_h):
    """Raw ADC values -> (temp_c, pressure_hpa, rh). Datasheet floating-point formulas."""
    T1, T2, T3 = cal.t
    var1 = (adc_t / 16384.0 - T1 / 1024.0) * T2
    var2 = ((adc_t / 131072.0 - T1 / 8192.0) ** 2) * T3
    t_fine = var1 + var2
    temp_c = t_fine / 5120.0

    P1, P2, P3, P4, P5, P6, P7, P8, P9 = cal.p
    var1 = t_fine / 2.0 - 64000.0
    var2 = var1 * var1 * P6 / 32768.0
    var2 = var2 + var1 * P5 * 2.0
    var2 = var2 / 4.0 + P4 * 65536.0
    var1 = (P3 * var1 * var1 / 524288.0 + P2 * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * P1
    if var1 == 0:
        pressure = None
    else:
        p = 1048576.0 - adc_p
        p = (p - var2 / 4096.0) * 6250.0 / var1
        var1 = P9 * p * p / 2147483648.0
        var2 = p * P8 / 32768.0
        pressure = (p + (var1 + var2 + P7) / 16.0) / 100.0

    H1, H2, H3, H4, H5, H6 = cal.h
    h = t_fine - 76800.0
    h = (adc_h - (H4 * 64.0 + H5 / 16384.0 * h)) * (
        H2 / 65536.0 * (1.0 + H6 / 67108864.0 * h * (1.0 + H3 / 67108864.0 * h)))
    h = h * (1.0 - H1 * h / 524288.0)
    rh = max(0.0, min(100.0, h))
    return temp_c, pressure, rh


class Bme280(object):
    def __init__(self, bus, address=0x76):
        self.bus = bus
        self.address = address
        chip = bus.read_byte_data(address, REG_ID)
        if chip != CHIP_ID:
            raise IOError("chip id 0x%02x at 0x%02x is not a BME280 (0x60); a BMP280 (0x58) has no humidity"
                          % (chip, address))
        self.cal = Calibration.from_registers(bus.read_i2c_block_data(address, 0x88, 26),
                                              bus.read_i2c_block_data(address, 0xE1, 7))
        bus.write_byte_data(address, REG_CONFIG, 0x00)      # no IIR filter, standby irrelevant in forced mode

    def read(self, sleep=time.sleep):
        """One forced measurement (x1 oversampling each). Returns (temp_c, pressure_hpa, rh)."""
        b = self.bus
        b.write_byte_data(self.address, REG_CTRL_HUM, 0x01)    # humidity x1 (takes effect on the next ctrl_meas)
        b.write_byte_data(self.address, REG_CTRL_MEAS, 0x25)   # temp x1, pressure x1, forced mode
        for _ in range(20):
            sleep(0.005)
            if not b.read_byte_data(self.address, REG_STATUS) & 0x08:
                break
        d = b.read_i2c_block_data(self.address, REG_DATA, 8)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        adc_h = (d[6] << 8) | d[7]
        if adc_t == 0x80000 or adc_p == 0x80000 or adc_h == 0x8000:
            raise IOError("measurement skipped (reset value)")
        return compensate(self.cal, adc_t, adc_p, adc_h)


def plausible(temp_c, pressure_hpa, rh):
    return (-40 <= temp_c <= 85 and pressure_hpa is not None and 300 <= pressure_hpa <= 1100 and 0 <= rh <= 100)


class EnvReader(object):
    def __init__(self, cfg, mqtt, open_bus=None, clock=time.time):
        c = dict(DEFAULTS)
        c.update(cfg.get("bme280") or {})
        if isinstance(c["address"], str):
            c["address"] = int(c["address"], 0)
        self.c = c
        self.mqtt = mqtt
        self.open_bus = open_bus or self._smbus
        self.clock = clock
        self.sensor = None
        self._down_since = None
        self.stats = {"published": 0, "errors": 0, "dropped": 0, "reopened": 0}

    def _smbus(self):
        from smbus2 import SMBus
        return SMBus(int(self.c["bus"]))

    def step(self):
        """Read once and publish. Returns the message or None."""
        now = self.clock()
        try:
            if self.sensor is None:
                self.sensor = Bme280(self.open_bus(), self.c["address"])
                if self._down_since is not None:
                    self.stats["reopened"] += 1
                    log.info("BME280 back after %.0f s", now - self._down_since)
                self._down_since = None
            t, p, h = self.sensor.read()
        except (IOError, OSError) as e:
            self.stats["errors"] += 1
            if self._down_since is None:
                self._down_since = now
                log.warning("BME280 read failed: %s", e)
            if now - self._down_since >= self.c["retry_s"]:
                self.sensor = None  # reopen the bus on the next try
            return None
        t += float(self.c["temp_offset_c"])
        if not plausible(t, p, h):
            self.stats["dropped"] += 1
            log.warning("BME280 reading out of range, dropped: %.1f C %.1f hPa %.1f %%", t, p or 0, h)
            return None
        msg = {"ts": now_ts(), "temp_c": round(t, 1), "rh": round(h, 1), "pressure_hpa": round(p, 1)}
        self.mqtt.publish(TOPIC_GW_ENV, msg, qos=0)
        self.stats["published"] += 1
        return msg


def main():
    import argparse
    import threading

    from common.config import load_config
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    ap = argparse.ArgumentParser(description="BME280 -> ab/gw/env (AIR-12)")
    ap.add_argument("--once", action="store_true", help="print one reading and exit (no MQTT)")
    args = ap.parse_args()
    cfg = load_config()
    setup_logging("bme280", cfg)

    if args.once:
        class Print(object):
            def publish(self, topic, msg, qos=1):
                print(topic, msg)
        if EnvReader(cfg, Print()).step() is None:
            raise SystemExit("no reading, see the log")
        return

    client = MqttClient("bme280", cfg)
    client.start(wait=False)
    reader = EnvReader(cfg, client)
    stop = threading.Event()

    def loop():
        while not stop.wait(float(reader.c["period_s"])):
            reader.step()
    threading.Thread(target=loop, daemon=True).start()
    log.info("bme280 reader on bus %s address 0x%02x", reader.c["bus"], reader.c["address"])
    try:
        run_until_signal(lambda: log.info("stats %s", reader.stats), tick_s=300)
    finally:
        stop.set()
        client.stop()


if __name__ == "__main__":
    main()
