"""Fake Node 1 (the ESP32-S3 inside the purifier), for testing without hardware.

Stands in for "real Node 1 + uart_bridge": it publishes what the bridge would publish
and obeys the same commands (docs/protocol.md):

  ab/node1/telemetry  every second
  ab/node1/event      wake / stop_pressed / servo_timeout
  ab/node1/ack        {ack_seq} for every command that carries a seq
  ab/node1/cmd        (subscribed) aim / fan / stop / survey_step

PM2.5 follows a small room model: dust sources push it up and the fan pulls it down,
so decision.py can be tested as a closed loop.

Run from gateway/:  python3 -m tools.sim_node1 --help
"""
import json
import random
import threading

from tools.sim_common import Link, Seq, base_args, clamp, log, now, run_every, start_keys

NODE_ID = "node1"
TEL_TOPIC = "ab/node1/telemetry"
EVENT_TOPIC = "ab/node1/event"
CMD_TOPIC = "ab/node1/cmd"
ACK_TOPIC = "ab/node1/ack"

PAN_LIMIT = 180.0     # firmware limit, +-180 deg (CLAUDE.md)
TILT_LIMIT = 30.0     # louver +-30 deg
PAN_SPEED = 12.0      # deg/s: the head turns 180 deg in about 15 s
TILT_SPEED = 60.0     # deg/s
FAN_MAX_RPM = 1700.0
FAN_MIN_RPM = 300.0
FAN_LAG_S = 2.0       # fan reaches a new speed in a few seconds
CLOGGED = 0.3         # a clogged filter cuts RPM by 30 %
OUTDOOR_PM = 30.0     # ug/m3 that leaks in from outside
LEAK_RATE = 0.002     # 1/s
CLEAN_RATE = 0.01     # 1/s with the fan at full speed
AMBIENT_C = 30.0
HOT_BATTERY_C = 52.0  # where the battery heads when "hot battery" is switched on
BATT_LAG_S = 120.0

# name: (ug/m3 added per second, how long in seconds)
DUST_SOURCES = {
    "cooking": (0.8, 300.0),
    "incense": (0.4, 600.0),
    "smoking": (1.5, 120.0),
}


class Node1Model(object):
    """Physics and state of the fake Node 1. No MQTT here, so it can be unit-tested."""

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.pm = 13.0
        self.dust_rate = 0.0
        self.dust_left = 0.0
        self.clog = 0.0
        self.fan_pct = 25
        self.fan_rpm = self._target_rpm()
        self.pan = 0.0
        self.pan_target = 0.0
        self.tilt = 0.0
        self.tilt_target = 0.0
        self.button = False
        self.batt_temp = AMBIENT_C
        self.hot_battery = False
        self.last_cmd_seq = None

    def _target_rpm(self):
        if self.fan_pct <= 0:
            return 0.0
        rpm = max(FAN_MIN_RPM, FAN_MAX_RPM * self.fan_pct / 100.0)
        return rpm * (1.0 - self.clog)

    def start_dust(self, kind):
        self.dust_rate, self.dust_left = DUST_SOURCES[kind]

    def press_stop(self, pressed):
        self.button = pressed
        if pressed:
            self._halt()

    def _halt(self):
        self.fan_pct = 0
        self.pan_target = self.pan
        self.tilt_target = self.tilt

    def step(self, dt):
        """Advance the simulation by dt seconds (split into small steps for stability)."""
        while dt > 0:
            h = min(dt, 1.0)
            self._step(h)
            dt -= h

    def _step(self, h):
        source = 0.0
        if self.dust_left > 0:
            source = self.dust_rate
            self.dust_left -= h
        self.fan_rpm += (self._target_rpm() - self.fan_rpm) * min(1.0, h / FAN_LAG_S)
        cleaning = CLEAN_RATE * self.fan_rpm / FAN_MAX_RPM
        self.pm += (LEAK_RATE * (OUTDOOR_PM - self.pm) + source - cleaning * self.pm) * h
        self.pm = max(0.0, self.pm)
        self.pan = _move_toward(self.pan, self.pan_target, PAN_SPEED * h)
        self.tilt = _move_toward(self.tilt, self.tilt_target, TILT_SPEED * h)
        target_temp = HOT_BATTERY_C if self.hot_battery else AMBIENT_C
        self.batt_temp += (target_temp - self.batt_temp) * min(1.0, h / BATT_LAG_S)

    def apply_cmd(self, msg):
        """Apply one ab/node1/cmd message. Returns a short note for the log."""
        seq = msg.get("seq")
        if seq is not None and seq == self.last_cmd_seq:
            return "duplicate seq {}: ACK sent again, not applied".format(seq)
        self.last_cmd_seq = seq
        cmd = msg.get("cmd")
        if cmd == "stop":
            self._halt()
            return "stopped fan and motors"
        if self.button:
            return "ignored: the stop button is pressed"
        if cmd == "fan":
            self.fan_pct = int(clamp(int(msg.get("pct", 0)), 0, 100))
            return "fan {} %".format(self.fan_pct)
        if cmd == "aim":
            if msg.get("pan_deg") is not None:
                self.pan_target = clamp(float(msg["pan_deg"]), -PAN_LIMIT, PAN_LIMIT)
            if msg.get("tilt_deg") is not None:
                self.tilt_target = clamp(float(msg["tilt_deg"]), -TILT_LIMIT, TILT_LIMIT)
            return "aim pan {:.1f} tilt {:.1f}".format(self.pan_target, self.tilt_target)
        if cmd == "survey_step":
            self.pan_target = clamp(float(msg["pan_deg"]), -PAN_LIMIT, PAN_LIMIT)
            return "survey step to pan {:.1f}".format(self.pan_target)
        return "unknown cmd {!r}".format(cmd)

    def telemetry(self):
        gauss = self.rng.gauss
        pm25 = max(0, int(round(self.pm + gauss(0, 1.5))))
        return {
            "pm1": max(0, int(round(pm25 * 0.7 + gauss(0, 0.8)))),
            "pm25": pm25,
            "pm10": max(pm25, int(round(pm25 * 1.3 + gauss(0, 1.5)))),
            "fan_rpm": int(round(self.fan_rpm)),
            "pan_deg": round(self.pan, 1),
            "tilt_deg": round(self.tilt, 1),
            "button": self.button,
            # Proposed in docs/protocol.md, not agreed yet:
            "batt_temp_c": round(self.batt_temp + gauss(0, 0.1), 1),
            "fan_pct": self.fan_pct,
        }


def _move_toward(value, target, max_step):
    if abs(target - value) <= max_step:
        return target
    return value + max_step if target > value else value - max_step


KEYS_HELP = """Keys (type a letter, then Enter):
  d  dust event now (cooking / incense / smoking at random)
  w  wake word heard
  s  press / release the stop button
  t  servo timeout event
  c  clogged filter on / off (lower fan RPM)
  h  hot battery on / off (battery heads to 52 C)
  ?  show this help"""


def main():
    p = base_args("Fake Node 1 (purifier ESP32-S3 + uart_bridge) on MQTT.")
    p.add_argument("--period", type=float, default=1.0, help="seconds between telemetry messages")
    p.add_argument("--dust-every", type=float, default=900.0,
                   help="average simulated seconds between random dust events (0 = only key d)")
    p.add_argument("--drop-tel", type=float, default=0.0,
                   help="fraction of telemetry to drop on purpose (tests seq-gap metrics)")
    p.add_argument("--drop-ack", type=float, default=0.0,
                   help="fraction of ACKs to drop on purpose (tests retry)")
    args = p.parse_args()

    rng = random.Random(args.seed)
    model = Node1Model(rng)
    lock = threading.Lock()
    seq = Seq()
    sim_dt = args.period * args.speed

    def send_event(kind):
        link.publish(EVENT_TOPIC, {"node_id": NODE_ID, "seq": seq.next(), "ts": now(), "type": kind}, qos=1)
        log("event " + kind)

    def on_json(topic, msg):
        with lock:
            try:
                note = model.apply_cmd(msg)
            except (KeyError, TypeError, ValueError) as e:
                note = "bad command: {!r}".format(e)
        log("cmd {} -> {}".format(json.dumps(msg, ensure_ascii=False), note))
        if "seq" in msg:
            if rng.random() < args.drop_ack:
                log("  ACK dropped on purpose")
            else:
                link.publish(ACK_TOPIC, {"ack_seq": msg["seq"]}, qos=1)

    def on_key(key):
        with lock:
            if key == "d":
                kind = rng.choice(sorted(DUST_SOURCES))
                model.start_dust(kind)
                log("dust event: " + kind)
            elif key == "s":
                model.press_stop(not model.button)
                pressed = model.button
            elif key == "c":
                model.clog = 0.0 if model.clog else CLOGGED
                log("filter " + ("clogged" if model.clog else "clean"))
            elif key == "h":
                model.hot_battery = not model.hot_battery
                log("hot battery " + ("on" if model.hot_battery else "off"))
            elif key not in ("w", "t"):
                print(KEYS_HELP, flush=True)
        if key == "w":
            send_event("wake")
        elif key == "t":
            send_event("servo_timeout")
        elif key == "s":
            if pressed:
                send_event("stop_pressed")
            else:
                log("stop button released")

    ticks = [0]

    def tick():
        with lock:
            if args.dust_every > 0 and model.dust_left <= 0 and rng.random() < sim_dt / args.dust_every:
                kind = rng.choice(sorted(DUST_SOURCES))
                model.start_dust(kind)
                log("dust event: " + kind)
            model.step(sim_dt)
            reading = model.telemetry()
        msg = {"node_id": NODE_ID, "seq": seq.next(), "ts": now()}
        msg.update(reading)
        if rng.random() < args.drop_tel:
            log("telemetry seq {} dropped on purpose".format(msg["seq"]))
        else:
            link.publish(TEL_TOPIC, msg)
        ticks[0] += 1
        if ticks[0] % 5 == 0:
            log("PM2.5 {pm25:>3} | fan {fan_pct:>3} % {fan_rpm:>4} rpm | pan {pan_deg:>6} tilt {tilt_deg:>5}"
                " | batt {batt_temp_c} C | stop {button}".format(**msg))

    link = Link("sim_node1", args, [CMD_TOPIC], on_json)
    link.start()
    start_keys(on_key, KEYS_HELP, enabled=not args.no_keys)
    run_every(args.period, tick)
    link.stop()


if __name__ == "__main__":
    main()
