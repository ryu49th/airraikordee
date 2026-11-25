import struct

import pytest

from common.protocol import TOPIC_GW_ENV
from conftest import FakeMqtt
from sensors.bme280_reader import Calibration, EnvReader, compensate

# Worked example from the Bosch BMP280 datasheet (same temperature and pressure formulas as the BME280)
T = (27504, 26435, -1000)
P = (36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000)
H = (75, 362, 0, 324, 50, 30)   # a typical BME280 humidity calibration


def test_temperature_and_pressure_match_the_datasheet_example():
    t, p, _ = compensate(Calibration(T, P, H), 519888, 415148, 30000)
    assert t == pytest.approx(25.08, abs=0.01)
    assert p == pytest.approx(1006.5327, abs=0.01)


def test_humidity_rises_with_the_adc_value_and_stays_in_range():
    cal = Calibration(T, P, H)
    values = [compensate(cal, 519888, 415148, adc)[2] for adc in (20000, 26000, 32000, 40000)]
    assert values == sorted(values) and 0 <= values[0] < values[-1] <= 100
    assert compensate(cal, 519888, 415148, 0)[2] == 0.0
    assert compensate(cal, 519888, 415148, 65000)[2] == 100.0


def registers():
    block1 = bytearray(struct.pack("<Hhh", *T) + struct.pack("<Hhhhhhhhh", *P) + b"\x00" + bytes([H[0]]))
    h4, h5 = H[3], H[4]
    block2 = bytearray(struct.pack("<h", H[1]) + bytes([H[2], (h4 >> 4) & 0xFF, (h4 & 0x0F) | ((h5 & 0x0F) << 4),
                                                         (h5 >> 4) & 0xFF, H[5] & 0xFF]))
    return block1, block2


class FakeBus(object):
    def __init__(self, chip=0x60, data=None, fail=False):
        self.chip = chip
        self.data = data or [0x65, 0x5A, 0xC0, 0x7E, 0xED, 0x00, 0x75, 0x30]   # adc_p 415148, adc_t 519888
        self.fail = fail
        self.writes = []

    def read_byte_data(self, addr, reg):
        if self.fail:
            raise OSError(121, "Remote I/O error")
        return self.chip if reg == 0xD0 else 0x00

    def write_byte_data(self, addr, reg, value):
        self.writes.append((reg, value))

    def read_i2c_block_data(self, addr, reg, n):
        if self.fail:
            raise OSError(121, "Remote I/O error")
        b1, b2 = registers()
        return list({0x88: b1, 0xE1: b2}.get(reg, self.data))[:n]


def test_calibration_registers_unpack():
    cal = Calibration.from_registers(*registers())
    assert cal.t == T and cal.p == P and cal.h == H


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_reader_publishes_env():
    mqtt = FakeMqtt()
    r = EnvReader({}, mqtt, open_bus=lambda: FakeBus())
    msg = r.step()
    assert msg["temp_c"] == 25.1 and msg["pressure_hpa"] == pytest.approx(1006.5, abs=0.1) and 0 < msg["rh"] < 100
    assert mqtt.on(TOPIC_GW_ENV) == [msg]


def test_wrong_chip_and_i2c_errors_reopen_the_bus():
    clock = Clock()
    buses = [FakeBus(chip=0x58), FakeBus(fail=True), FakeBus()]
    opened = []

    def open_bus():
        opened.append(1)
        return buses[min(len(opened) - 1, 2)]

    r = EnvReader({"bme280": {"retry_s": 10}}, FakeMqtt(), open_bus=open_bus, clock=clock)
    assert r.step() is None          # BMP280 chip id: no humidity
    clock.t += 11
    assert r.step() is None          # I2C error
    clock.t += 11
    assert r.step() is not None and r.stats["reopened"] == 1


def test_out_of_range_reading_is_dropped():
    mqtt = FakeMqtt()
    r = EnvReader({"bme280": {"temp_offset_c": 200}}, mqtt, open_bus=lambda: FakeBus())
    assert r.step() is None and r.stats["dropped"] == 1 and mqtt.on(TOPIC_GW_ENV) == []
