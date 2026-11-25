"""The brain (AIR-13 fan curve, AIR-14 modes, AIR-15 charge permission and alerts; spec section 4 decision.py).

Once a second it looks at the latest messages and decides:

PM and fan (AIR-13)
  pm25_corrected = pm25 / (1 + k * max(0, rh - 60) / 40)   (rh from ab/gw/env; without it, no correction)
  fan % from `fan_table` on pm25_corrected, at most `night_fan_max` at night. Sent as ab/node1/cmd fan when the
  value changes and again every `fan_resend_s`. With fan_mode "manual" on the dashboard, config_listener sends the
  fan and this module only reports it. Nothing is sent while the stop button is held.

Mode (AIR-14), first match wins
  night       inside night_start..night_end (dashboard settings): fan capped, the head keeps its last aim
  survey      vision.py is turning the head through its 8 photos (it starts surveys itself: every 30 min and
              on a dust spike from cause_rules); this module does not aim meanwhile
  aim_person  a person was seen within idle_after_s (10 min): P-control on the nearest person's pan/tilt error
              (deadband 3 deg, at most 10 deg per update); after the person leaves the head stays where it was
  idle        nobody for 10 min: the head turns back to the room centre (pan 0, tilt 0) once

Charging (AIR-15, safety rule 4.3: never charge lithium unattended)
  charge_allowed = docked AND a person seen (or "I am here" pressed) within 15 min AND battery temperature known
  and < 45 C. Sent on ab/node2/cmd when it changes and every `charge_refresh_s` (30 s; the box stops by itself
  60 s after the last refresh, so a lost Jetson or Wi-Fi stops charging too).

Alerts (in ab/gw/state.alerts while active; the uploader stores each new one and notify/discord.py sends it)
  pm_high          pm25_corrected > pm_alert for pm_alert_min minutes (ends below 90 % of pm_alert)
  charge_cut_temp  docked and charging refused for temperature, or Node 2 latched an over-temperature FAULT
  charge_fault     Node 2 in FAULT for any other reason
  battery_low      est_percent < 25
  charge_full      Node 2 in FULL
  filter_clogged   health.py's fan_rpm_ratio below `clogged_ratio` for 10 min
  stop_pressed     the stop button is held
  (node_offline comes from health.py and uart_fail from uart_bridge, as one-off ab/gw/alert messages.)

Publishes ab/gw/state {ts, mode, fan_pct, target {pan_deg, tilt_deg}, pm25_corrected, alerts: [...]} every second.

Run on the Jetson from gateway/:  python3 -m brain.decision
"""
import logging
import threading
import time

from common.protocol import (TOPIC_GW_CONFIG, TOPIC_GW_ENV, TOPIC_GW_HEALTH, TOPIC_GW_STATE, TOPIC_GW_VISION,
                             TOPIC_NODE1_CMD, TOPIC_NODE1_EVENT, TOPIC_NODE1_TELEMETRY, TOPIC_NODE2_CMD,
                             TOPIC_NODE2_TELEMETRY, SeqCounter, now_ts)

log = logging.getLogger("decision")

TOPIC_GW_UI_CMD = "ab/gw/ui_cmd"

DEFAULTS = {
    "pm_alert": 35,
    "pm_alert_min": 10,
    "humidity_k": 0.5,
    "fan_table": [[15, 25], [35, 45], [55, 70], [100000, 100]],
    "night_start": "22:00",
    "night_end": "06:00",
    "night_fan_max": 35,
    "idle_after_s": 600,
    "aim_deadband_deg": 3,
    "aim_max_step_deg": 10,
    "aim_gain": 0.8,
    "charge_person_window_s": 900,
    "charge_max_temp_c": 45,
    "charge_refresh_s": 30,
    "fan_resend_s": 60,
    "stale_s": 30,               # sensor data older than this is ignored
    "vision_stale_s": 3,
    "clogged_ratio": 0.7,
    "clogged_for_s": 600,
    "battery_low_pct": 25,
}
PAN_LIMIT = 178.0                # firmware soft limit; the switch is the hard stop at 180
TILT_LIMIT = 30.0

TITLES = {
    "pm_high": "PM2.5 สูง", "charge_cut_temp": "หยุดชาร์จเพราะร้อนเกิน", "charge_fault": "ที่ชาร์จขัดข้อง",
    "battery_low": "แบตเตอรี่ต่ำ", "charge_full": "ชาร์จเต็มแล้ว", "filter_clogged": "ไส้กรองอุดตัน",
    "stop_pressed": "กดปุ่มหยุด",
}
LEVELS = {"pm_high": "warn", "charge_cut_temp": "crit", "charge_fault": "crit", "battery_low": "warn",
          "charge_full": "info", "filter_clogged": "warn", "stop_pressed": "info"}


def correct_pm(pm25, rh, k):
    if rh is None or rh <= 60:
        return float(pm25)
    return float(pm25) / (1.0 + k * (rh - 60.0) / 40.0)


def fan_from_table(pm, table):
    for upper, pct in table:
        if pm <= upper:
            return int(pct)
    return int(table[-1][1])


def minutes_of_day(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(now_min, start, end):
    """True if now_min is inside start..end (minutes of the day); the window may cross midnight."""
    if start == end:
        return False
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Brain(object):
    def __init__(self, cfg, mqtt, clock=time.time, local_minutes=None):
        c = dict(DEFAULTS)
        c.update(cfg.get("decision") or {})
        self.c = c
        self.mqtt = mqtt
        self.clock = clock
        self.local_minutes = local_minutes or (lambda t: time.localtime(t).tm_hour * 60 + time.localtime(t).tm_min)
        self._lock = threading.Lock()
        self._seq1 = SeqCounter()
        self._seq2 = SeqCounter()
        self._stop = threading.Event()
        self._thread = None
        # latest inputs: (time received, message)
        self.node1 = None
        self.node2 = None
        self.env = None
        self.vision = None
        self.health = None
        self.settings = {}
        self.person_seen_at = None       # camera
        self.present_confirmed_at = None  # "I am here" from the dashboard / voice
        # decisions
        self.mode = "idle"
        self.fan_pct = None
        self.target = {"pan_deg": 0.0, "tilt_deg": 0.0}
        self.pm25_corrected = None
        self.charge_allowed = None
        self.charge_reason = None
        self.charge_block = None         # why charging is refused: docked / nobody / temp_unknown / hot
        self._fan_sent = (None, 0.0)
        self._charge_sent_at = 0.0
        self._idle_centred = False
        self._pm_high_since = None
        self._clogged_since = None
        self.active = {}                 # alert type -> StateAlert
        self.stats = {"ticks": 0, "fan_cmds": 0, "aim_cmds": 0, "charge_cmds": 0}

    # --- lifecycle ---

    def start(self):
        subs = ((TOPIC_NODE1_TELEMETRY, self.on_node1), (TOPIC_NODE1_EVENT, self.on_event),
                (TOPIC_NODE2_TELEMETRY, self.on_node2), (TOPIC_GW_ENV, self.on_env),
                (TOPIC_GW_VISION, self.on_vision), (TOPIC_GW_HEALTH, self.on_health),
                (TOPIC_GW_CONFIG, self.on_config), (TOPIC_GW_UI_CMD, self.on_ui_cmd))
        for topic, handler in subs:
            self.mqtt.subscribe(topic, handler)
        self._thread = threading.Thread(target=self._loop, name="decision", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3.0)

    def _loop(self):
        while not self._stop.wait(1.0):
            try:
                self.step()
            except Exception:
                log.exception("decision step failed")

    # --- inputs ---

    def on_node1(self, topic, msg):
        with self._lock:
            self.node1 = (self.clock(), msg)

    def on_event(self, topic, msg):
        if msg.get("type") == "stop_pressed":
            log.info("stop button pressed")

    def on_node2(self, topic, msg):
        with self._lock:
            self.node2 = (self.clock(), msg)

    def on_env(self, topic, msg):
        with self._lock:
            self.env = (self.clock(), msg)

    def on_vision(self, topic, msg):
        with self._lock:
            self.vision = (self.clock(), msg)
            if msg.get("person_present"):
                self.person_seen_at = self.clock()

    def on_health(self, topic, msg):
        with self._lock:
            self.health = (self.clock(), msg)

    def on_config(self, topic, msg):
        keep = ("pm_alert", "night_start", "night_end", "fan_mode", "manual_fan_pct")
        with self._lock:
            self.settings = dict((k, msg[k]) for k in keep if k in msg)

    def on_ui_cmd(self, topic, msg):
        if msg.get("cmd") == "confirm_present":
            with self._lock:
                self.present_confirmed_at = self.clock()
            log.info("presence confirmed from the %s", msg.get("source", "app"))

    # --- helpers ---

    def _fresh(self, item, now, limit=None):
        if item is None or now - item[0] > (limit if limit is not None else self.c["stale_s"]):
            return None
        return item[1]

    def _setting(self, name):
        v = self.settings.get(name)
        return self.c[name] if v is None else v

    # --- one decision step ---

    def step(self):
        now = self.clock()
        with self._lock:
            node1 = self._fresh(self.node1, now)
            node2 = self._fresh(self.node2, now)
            env = self._fresh(self.env, now, 120)
            vision = self._fresh(self.vision, now, self.c["vision_stale_s"])
            health = self._fresh(self.health, now, 60)
            times = [t for t in (self.person_seen_at, self.present_confirmed_at) if t is not None]
            seen = max(times) if times else None
            camera_seen = self.person_seen_at
        self.stats["ticks"] += 1
        button = bool(node1 and node1.get("button"))

        # PM and humidity
        if node1 and isinstance(node1.get("pm25"), (int, float)):
            rh = env.get("rh") if env and isinstance(env.get("rh"), (int, float)) else None
            self.pm25_corrected = round(correct_pm(node1["pm25"], rh, float(self.c["humidity_k"])), 1)
        elif node1 is None:
            self.pm25_corrected = None

        # mode
        night = in_window(self.local_minutes(now), minutes_of_day(self._setting("night_start")),
                          minutes_of_day(self._setting("night_end")))
        surveying = bool(vision and vision.get("surveying"))
        recent_person = camera_seen is not None and now - camera_seen < self.c["idle_after_s"]
        mode = "night" if night else "survey" if surveying else "aim_person" if recent_person else "idle"
        if mode != self.mode:
            log.info("mode %s -> %s", self.mode, mode)
            self.mode = mode

        if not button:
            self._decide_fan(now, night)
            self._decide_aim(now, mode, vision, node1)
        self._decide_charge(now, node1, node2, seen)
        self._update_alerts(now, node1, node2, health, button)

        state = {"ts": int(now), "mode": self.mode, "fan_pct": self.fan_pct,
                 "target": {"pan_deg": round(self.target["pan_deg"], 1), "tilt_deg": round(self.target["tilt_deg"], 1)},
                 "pm25_corrected": self.pm25_corrected, "alerts": sorted(self.active.values(), key=lambda a: a["since"])}
        self.mqtt.publish(TOPIC_GW_STATE, state, qos=0)
        return state

    def _cmd1(self, body):
        body = dict(body)
        body["seq"] = self._seq1.next()
        body["ts"] = now_ts()
        self.mqtt.publish(TOPIC_NODE1_CMD, body)

    def _decide_fan(self, now, night):
        if self.settings.get("fan_mode") == "manual" and self.settings.get("manual_fan_pct") is not None:
            self.fan_pct = int(self.settings["manual_fan_pct"])  # config_listener sends it
            self._fan_sent = (None, 0.0)
            return
        if self.pm25_corrected is None:
            return  # no fresh PM: leave the fan as it is
        pct = fan_from_table(self.pm25_corrected, self.c["fan_table"])
        if night:
            pct = min(pct, int(self.c["night_fan_max"]))
        self.fan_pct = pct
        last, at = self._fan_sent
        if pct != last or now - at >= self.c["fan_resend_s"]:
            if pct != last:
                log.info("fan %s%% (PM2.5 %.1f%s)", pct, self.pm25_corrected, ", night cap" if night else "")
            self._cmd1({"cmd": "fan", "pct": pct})
            self._fan_sent = (pct, now)
            self.stats["fan_cmds"] += 1

    def _decide_aim(self, now, mode, vision, node1):
        if mode == "idle":
            if not self._idle_centred:
                self.target = {"pan_deg": 0.0, "tilt_deg": 0.0}
                self._cmd1({"cmd": "aim", "pan_deg": 0.0, "tilt_deg": 0.0})
                self.stats["aim_cmds"] += 1
                self._idle_centred = True
                log.info("nobody for %d min: head back to the room centre", self.c["idle_after_s"] // 60)
            return
        self._idle_centred = False
        if mode != "aim_person" or not vision or not node1:
            return  # night keeps the last aim; survey belongs to vision.py
        persons = vision.get("persons") or []
        if not persons:
            return
        p = persons[0]
        ep, et = float(p.get("pan_err_deg") or 0.0), float(p.get("tilt_err_deg") or 0.0)
        db, step = float(self.c["aim_deadband_deg"]), float(self.c["aim_max_step_deg"])
        gain = float(self.c["aim_gain"])
        dp = 0.0 if abs(ep) < db else clamp(gain * ep, -step, step)
        dt = 0.0 if abs(et) < db else clamp(gain * et, -step, step)
        if not dp and not dt:
            return
        pan = clamp(float(node1.get("pan_deg") or 0.0) + dp, -PAN_LIMIT, PAN_LIMIT)
        tilt = clamp(float(node1.get("tilt_deg") or 0.0) + dt, -TILT_LIMIT, TILT_LIMIT)
        self.target = {"pan_deg": round(pan, 1), "tilt_deg": round(tilt, 1)}
        self._cmd1({"cmd": "aim", "pan_deg": self.target["pan_deg"], "tilt_deg": self.target["tilt_deg"]})
        self.stats["aim_cmds"] += 1

    def _decide_charge(self, now, node1, node2, seen):
        docked = bool(node2 and (node2.get("docked") or node2.get("plugged")))
        temp = node1.get("batt_temp_c") if node1 and isinstance(node1.get("batt_temp_c"), (int, float)) else None
        attended = seen is not None and now - seen < self.c["charge_person_window_s"]
        if not docked:
            block, reason = "docked", "not docked"
        elif not attended:
            block, reason = "nobody", "nobody seen for %d min" % (self.c["charge_person_window_s"] // 60)
        elif temp is None:
            block, reason = "temp_unknown", "battery temperature unknown"
        elif temp >= self.c["charge_max_temp_c"]:
            block, reason = "hot", "battery %.1f C" % temp
        else:
            block, reason = None, "person seen, battery %.1f C" % temp
        allowed = block is None
        self.charge_block = block
        changed = allowed != self.charge_allowed
        if reason != self.charge_reason:
            log.info("charge_allowed %s (%s)", allowed, reason)
        self.charge_allowed, self.charge_reason = allowed, reason
        if changed or now - self._charge_sent_at >= self.c["charge_refresh_s"]:
            self.mqtt.publish(TOPIC_NODE2_CMD, {"seq": self._seq2.next(), "ts": now_ts(),
                                                "charge_allowed": allowed, "reason": reason})
            self._charge_sent_at = now
            self.stats["charge_cmds"] += 1

    # --- alerts ---

    def _set_alert(self, kind, on, now, text=""):
        if on and kind not in self.active:
            self.active[kind] = {"type": kind, "level": LEVELS[kind], "title": TITLES[kind], "text": text,
                                 "since": int(now)}
            log.info("alert %s: %s", kind, text)
        elif on:
            self.active[kind]["text"] = text
        elif kind in self.active:
            del self.active[kind]
            log.info("alert %s cleared", kind)

    def _update_alerts(self, now, node1, node2, health, button):
        pm, limit = self.pm25_corrected, float(self._setting("pm_alert"))
        if pm is not None and pm > limit:
            if self._pm_high_since is None:
                self._pm_high_since = now
        elif pm is None or pm < 0.9 * limit:
            self._pm_high_since = None
        high_for = now - self._pm_high_since if self._pm_high_since is not None else 0
        # starts after pm_alert_min above the limit, ends only below 90 % of it (no flapping around the limit)
        on = self._pm_high_since is not None and (high_for >= self.c["pm_alert_min"] * 60 or "pm_high" in self.active)
        self._set_alert("pm_high", on, now, "PM2.5 %.0f µg/m³ เกิน %d นาน %d นาที" % (pm or 0, limit, high_for // 60))

        mode2 = (node2 or {}).get("mode")
        fault = (node2 or {}).get("fault")
        temp_refused = self.charge_block == "hot"
        hot_fault = mode2 == "FAULT" and fault == "over_temp"
        self._set_alert("charge_cut_temp", bool(temp_refused or hot_fault), now,
                        "หยุดชาร์จ: %s" % ("กล่องชาร์จร้อนเกิน" if hot_fault else self.charge_reason))
        self._set_alert("charge_fault", mode2 == "FAULT" and not hot_fault, now, "กล่องชาร์จหยุดเพราะ %s" % fault)
        pct = (node2 or {}).get("est_percent")
        self._set_alert("battery_low", isinstance(pct, (int, float)) and pct < self.c["battery_low_pct"], now,
                        "แบตเหลือ %s%%" % pct)
        self._set_alert("charge_full", mode2 == "FULL", now, "ชาร์จเต็มแล้ว ถอดสายได้")

        ratio = (health or {}).get("fan_rpm_ratio")
        if isinstance(ratio, (int, float)) and ratio < self.c["clogged_ratio"]:
            if self._clogged_since is None:
                self._clogged_since = now
        else:
            self._clogged_since = None
        clogged = self._clogged_since is not None and now - self._clogged_since >= self.c["clogged_for_s"]
        self._set_alert("filter_clogged", clogged, now, "รอบพัดลมเหลือ %d%% ของไส้กรองใหม่ ควรเปลี่ยนไส้กรอง"
                        % int((ratio or 0) * 100))
        self._set_alert("stop_pressed", button, now, "ปุ่มหยุดถูกกดค้างไว้ เครื่องหยุดพัดลมและมอเตอร์")


def main():
    from common.config import load_config
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    cfg = load_config()
    setup_logging("decision", cfg)
    client = MqttClient("decision", cfg)
    client.start(wait=False)
    brain = Brain(cfg, client)
    brain.start()
    log.info("decision running")
    try:
        run_until_signal(lambda: log.info("mode %s fan %s charge %s alerts %s stats %s", brain.mode, brain.fan_pct,
                                          brain.charge_allowed, sorted(brain.active), brain.stats), tick_s=60)
    finally:
        brain.stop()
        client.stop()


if __name__ == "__main__":
    main()
