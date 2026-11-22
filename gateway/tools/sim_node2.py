"""Fake Node 2 (the charging box, XIAO ESP32C3), for testing without hardware.

Runs the charging state machine from CLAUDE.md (IDLE / DOCKED / CHARGING / FULL /
FAULT / FIELD_TEST) with a simple 3S2P battery model, over MQTT (docs/protocol.md):

  ab/node2/telemetry   every 2 s
  ab/node2/ack         {ack_seq} for every command that carries a seq
  ab/node2/cmd         (subscribed) charge_allowed, plus proposed field_test / reset_fault
  ab/node1/telemetry   (subscribed) battery temperature batt_temp_c from Node 1

Nothing sends charge_allowed until decision.py exists, so use --auto-allow to fake it.

Run from gateway/:  python3 -m tools.sim_node2 --help
"""
import json
import random
import threading

from tools.sim_common import Link, Seq, base_args, log, now, run_every, start_keys

NODE_ID = "node2"
TEL_TOPIC = "ab/node2/telemetry"
CMD_TOPIC = "ab/node2/cmd"
ACK_TOPIC = "ab/node2/ack"
NODE1_TEL_TOPIC = "ab/node1/telemetry"

# Battery and charger (3S2P 18650, XL4015 set to 12.6 V / 3 A)
CAPACITY_AH = 7.0
V_EMPTY = 9.9          # 3.3 V per cell
V_CV = 12.6            # 4.2 V per cell
I_CC = 3.0
R_INTERNAL = 0.15      # ohm, pack + cable
LOAD_A = 1.2           # what the running unit draws (Jetson, fan, motors)

# Rules from CLAUDE.md "Node 2 (charging box) firmware"
FULL_V = 12.55
FULL_I = 0.15
FULL_HOLD_S = 60.0
RECHARGE_V = 12.0
OVER_CURRENT_A = 3.5
BATT_HOT_C = 45.0
BOX_HOT_C = 60.0
CHARGE_TIMER_S = 4 * 3600.0
ALLOW_TIMEOUT_S = 60.0
BATT_TEMP_STALE_S = 10.0   # older Node 1 battery temperature counts as unknown

AMBIENT_C = 32.0
BOX_C_PER_A = 6.0          # XL4015 warms up with current
BOX_LAG_S = 90.0
BOX_HEAT_C = 20.0          # extra heat when "hot box" is switched on


class Node2Model(object):
    """Charging box state machine and battery physics. No MQTT here, so it can be unit-tested."""

    def __init__(self, soc=0.5, plugged=True, battery=True, capacity_ah=CAPACITY_AH, rng=None):
        self.rng = rng or random.Random()
        self.capacity_ah = capacity_ah
        self.mode = "IDLE"
        self.fault = None
        self.plugged = plugged
        self.battery = battery
        self.soc = soc
        self.box_temp = AMBIENT_C
        self.box_heat = 0.0
        self.batt_temp = None
        self.batt_temp_age = float("inf")
        self.fixed_batt_temp = None  # use this when no Node 1 is running
        self.allowed = False
        self.allowed_age = float("inf")
        self.reason = None
        self.auto_allow = False
        self.field_test = False
        self.over_current = False
        self.charge_time = 0.0
        self.full_time = 0.0
        self.voltage = 0.0
        self.current = 0.0
        self.charge_a = 0.0
        self.last_cmd_seq = None
        self.changes = []  # (old mode, new mode, fault) not yet logged

    # ---- inputs -------------------------------------------------------------

    def set_batt_temp(self, temp_c):
        self.batt_temp = temp_c
        self.batt_temp_age = 0.0

    def apply_cmd(self, msg):
        """Apply one ab/node2/cmd message. Returns a short note for the log."""
        seq = msg.get("seq")
        if seq is not None and seq == self.last_cmd_seq:
            return "duplicate seq {}: ACK sent again, not applied".format(seq)
        self.last_cmd_seq = seq
        notes = []
        if "charge_allowed" in msg:
            self.allowed = bool(msg["charge_allowed"])
            self.allowed_age = 0.0
            self.reason = msg.get("reason")
            notes.append("charge_allowed={} ({})".format(self.allowed, self.reason))
        if "field_test" in msg:
            self.field_test = bool(msg["field_test"])
            notes.append("field_test={}".format(self.field_test))
        if msg.get("reset_fault"):
            notes.append(self.reset_fault())
        return ", ".join(notes) or "nothing to do"

    def reset_fault(self):
        if self.mode != "FAULT":
            return "no fault to reset"
        if self.box_temp >= BOX_HOT_C or self._batt_hot():
            return "fault not reset: still too hot"
        self._set_mode("DOCKED" if self.plugged else "IDLE")
        return "fault reset"

    # ---- state machine ------------------------------------------------------

    def allowed_now(self):
        return self.allowed and self.allowed_age <= ALLOW_TIMEOUT_S

    def _batt_known(self):
        return self.batt_temp is not None and self.batt_temp_age <= BATT_TEMP_STALE_S

    def _batt_hot(self):
        return self._batt_known() and self.batt_temp >= BATT_HOT_C

    def _can_charge(self):
        return (self.allowed_now() and self.battery and self._batt_known()
                and not self._batt_hot() and self.box_temp < BOX_HOT_C)

    def _set_mode(self, mode, fault=None):
        if mode != self.mode:
            self.changes.append((self.mode, mode, fault))
            if mode == "CHARGING":
                self.charge_time = 0.0
                self.full_time = 0.0
        self.mode = mode
        self.fault = fault if mode == "FAULT" else None

    def step(self, dt):
        """Advance the simulation by dt seconds (split into 1 s steps)."""
        while dt > 0:
            h = min(dt, 1.0)
            self._step(h)
            dt -= h

    def _step(self, h):
        self.allowed_age += h
        self.batt_temp_age += h
        if self.auto_allow:
            self.allowed, self.allowed_age, self.reason = True, 0.0, "sim auto-allow"
        if self.fixed_batt_temp is not None:
            self.set_batt_temp(self.fixed_batt_temp)
        self._update_mode()
        self._update_power(h)

    def _update_mode(self):
        mode = self.mode
        if mode == "FAULT":
            return  # latched until reset_fault()
        if not self.plugged:
            self._set_mode("IDLE")
        elif mode == "IDLE":
            self._set_mode("DOCKED")
        elif self.field_test and not self.battery:
            self._set_mode("FIELD_TEST")
        elif mode == "FIELD_TEST":
            self._set_mode("DOCKED")  # flag off or a battery appeared
        elif mode == "DOCKED":
            if self._can_charge():
                self._set_mode("CHARGING")
        elif mode == "CHARGING":
            if self.over_current or self.current > OVER_CURRENT_A:
                self.over_current = False  # one-shot: the cause is gone once the MOSFET is off
                self._set_mode("FAULT", "over_current")
            elif self._batt_hot() or self.box_temp >= BOX_HOT_C:
                self._set_mode("FAULT", "over_temp")
            elif self.charge_time > CHARGE_TIMER_S:
                self._set_mode("FAULT", "timer")
            elif not self._can_charge():
                self._set_mode("DOCKED")  # not allowed, refresh timed out, or battery temp unknown
            elif self.full_time >= FULL_HOLD_S:
                self._set_mode("FULL")
        elif mode == "FULL":
            if self._ocv() < RECHARGE_V and self._can_charge():
                self._set_mode("CHARGING")

    def _ocv(self):
        return V_EMPTY + (V_CV - V_EMPTY) * self.soc

    def _update_power(self, h):
        self.charge_a = 0.0
        if self.mode == "CHARGING":
            ocv = self._ocv()
            # The box also feeds the running unit, so the INA226 sees charge + load,
            # and the XL4015 3 A limit is shared between them.
            wanted = max(0.0, (V_CV - ocv) / R_INTERNAL)
            self.charge_a = min(wanted, I_CC - LOAD_A)
            self.voltage = ocv + self.charge_a * R_INTERNAL
            self.current = self.charge_a + LOAD_A
            self.soc = min(1.0, self.soc + self.charge_a * h / 3600.0 / self.capacity_ah)
            self.charge_time += h
            # The sim knows the charge current on its own. The real box only sees the
            # total, so "I < 150 mA" can never be true while the unit is running.
            if self.voltage >= FULL_V and self.charge_a < FULL_I:
                self.full_time += h
            else:
                self.full_time = 0.0
        elif self.mode == "FIELD_TEST":
            self.voltage = V_CV
            self.current = LOAD_A
        else:
            # MOSFET off: the unit runs on its own battery.
            self.current = 0.0
            self.voltage = self._ocv() if (self.plugged and self.battery) else 0.0
            if self.battery:
                self.soc = max(0.0, self.soc - LOAD_A * h / 3600.0 / self.capacity_ah)
        target = AMBIENT_C + BOX_C_PER_A * self.current + self.box_heat
        self.box_temp += (target - self.box_temp) * min(1.0, h / BOX_LAG_S)

    # ---- output -------------------------------------------------------------

    def telemetry(self):
        gauss = self.rng.gauss
        known = self.plugged and self.battery
        return {
            "docked": self.plugged,
            "charging": self.mode == "CHARGING",
            "voltage": round(max(0.0, self.voltage + gauss(0, 0.01)), 2) if self.voltage else 0.0,
            "current_a": round(max(0.0, self.current + gauss(0, 0.01)), 2) if self.current else 0.0,
            "temp_c": round(self.box_temp + gauss(0, 0.1), 1),
            # The box can only estimate the battery while the cable is plugged in.
            "est_percent": int(round(self.soc * 100)) if known else None,
            # Proposed in docs/protocol.md, not agreed yet:
            "mode": self.mode,
            "fault": self.fault,
        }


KEYS_HELP = """Keys (type a letter, then Enter):
  p  plug / unplug the DC cable
  b  battery in / out (unplug the XT60 for the field test)
  a  auto-allow charging on / off (pretends decision.py sends charge_allowed)
  f  field_test flag on / off
  o  over-current spike (INA226 ALERT)
  h  hot charging box on / off (+20 C)
  r  reset FAULT (the box button)
  ?  show this help"""


def main():
    p = base_args("Fake Node 2 (charging box) on MQTT.")
    p.add_argument("--period", type=float, default=2.0, help="seconds between telemetry messages")
    p.add_argument("--soc", type=float, default=0.5, help="starting battery charge, 0 to 1")
    p.add_argument("--unplugged", action="store_true", help="start with the cable unplugged")
    p.add_argument("--no-battery", action="store_true", help="start without a battery (field test)")
    p.add_argument("--auto-allow", action="store_true",
                   help="allow charging without decision.py (refreshes charge_allowed by itself)")
    p.add_argument("--batt-temp", type=float, default=None,
                   help="fixed battery temperature in C when sim_node1 is not running")
    args = p.parse_args()

    rng = random.Random(args.seed)
    model = Node2Model(soc=args.soc, plugged=not args.unplugged, battery=not args.no_battery, rng=rng)
    model.auto_allow = args.auto_allow
    model.fixed_batt_temp = args.batt_temp
    lock = threading.Lock()
    seq = Seq()
    sim_dt = args.period * args.speed

    def on_json(topic, msg):
        if topic == NODE1_TEL_TOPIC:
            temp = msg.get("batt_temp_c")
            if isinstance(temp, (int, float)):
                with lock:
                    model.set_batt_temp(float(temp))
            return
        with lock:
            note = model.apply_cmd(msg)
        log("cmd {} -> {}".format(json.dumps(msg, ensure_ascii=False), note))
        if "seq" in msg:
            link.publish(ACK_TOPIC, {"ack_seq": msg["seq"]}, qos=1)

    def on_key(key):
        with lock:
            if key == "p":
                model.plugged = not model.plugged
                log("cable " + ("plugged" if model.plugged else "unplugged"))
            elif key == "b":
                model.battery = not model.battery
                log("battery " + ("in" if model.battery else "out"))
            elif key == "a":
                model.auto_allow = not model.auto_allow
                if not model.auto_allow:
                    model.allowed = False
                log("auto-allow " + ("on" if model.auto_allow else "off"))
            elif key == "f":
                model.field_test = not model.field_test
                log("field_test " + ("on" if model.field_test else "off"))
            elif key == "o":
                if model.mode == "CHARGING":
                    model.over_current = True
                    log("over-current spike")
                else:
                    log("over-current only matters while CHARGING (now {})".format(model.mode))
            elif key == "h":
                model.box_heat = 0.0 if model.box_heat else BOX_HEAT_C
                log("hot box " + ("on" if model.box_heat else "off"))
            elif key == "r":
                log(model.reset_fault())
            else:
                print(KEYS_HELP, flush=True)

    warned = [False]
    ticks = [0]

    def tick():
        with lock:
            model.step(sim_dt)
            reading = model.telemetry()
            changes, model.changes = model.changes, []
            no_batt_temp = model.mode == "DOCKED" and model.allowed_now() and not model._batt_known()
        for old, new, fault in changes:
            log("mode {} -> {}{}".format(old, new, " ({})".format(fault) if fault else ""))
        if no_batt_temp and not warned[0]:
            log("not charging: no battery temperature. Run sim_node1 or pass --batt-temp 30")
            warned[0] = True
        msg = {"node_id": NODE_ID, "seq": seq.next(), "ts": now()}
        msg.update(reading)
        link.publish(TEL_TOPIC, msg)
        ticks[0] += 1
        if ticks[0] % 5 == 0:
            log("{mode:<10} | {voltage:>5} V {current_a:>4} A | box {temp_c} C"
                " | battery {est_percent} %".format(**msg))

    link = Link("sim_node2", args, [CMD_TOPIC, NODE1_TEL_TOPIC], on_json)
    link.start()
    start_keys(on_key, KEYS_HELP, enabled=not args.no_keys)
    run_every(args.period, tick)
    link.stop()


if __name__ == "__main__":
    main()
