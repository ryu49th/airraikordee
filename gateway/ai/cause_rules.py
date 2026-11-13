"""Local dust-cause rules (AIR-18). Works without internet, answers in well under 50 ms.

Inputs: the last 30 min of PM2.5 (ab/node1/telemetry), humidity (ab/gw/env) and, if configured, outdoor PM2.5
from a public API. Rules (thresholds in config.yaml `cause`):
  fast spike (> spike_ugm3 within spike_window_s) with humidity rising   -> cooking
  fast spike with humidity flat                                          -> incense_candle (or smoking: the
                                                                            rules cannot tell them apart, so the
                                                                            confidence stays low and the cloud AI
                                                                            is asked)
  slow rise that tracks a high outdoor PM                                -> outdoor_infiltration / window_open
  anything else                                                          -> unknown
Publishes ab/gw/cause {ts, id, cause, confidence, source: "rules", advice_th, features}.

It runs when PM spikes, every `check_every_s` while PM is above `min_pm`, and on request
(ab/gw/cause/request, e.g. from decision.py's survey or the voice command "ทำไมฝุ่นเยอะ").

Run on the Jetson from gateway/:  python3 -m ai.cause_rules
"""
import logging
import threading
import time
from collections import deque

from common.protocol import TOPIC_GW_CAUSE, TOPIC_GW_ENV, TOPIC_NODE1_TELEMETRY, now_ts

log = logging.getLogger("cause_rules")

TOPIC_CAUSE_REQUEST = "ab/gw/cause/request"
CAUSES = ("window_open", "cooking", "incense_candle", "smoking", "outdoor_infiltration", "unknown")
ADVICE_TH = {
    "cooking": "น่าจะมาจากการทำอาหาร เปิดพัดลมดูดควันหรือปิดประตูครัว",
    "incense_candle": "น่าจะมาจากธูปหรือเทียน ดับแล้วเปิดระบายอากาศสักครู่",
    "smoking": "น่าจะมาจากควันบุหรี่ ควรสูบนอกบ้าน",
    "window_open": "ฝุ่นข้างนอกเข้ามาทางหน้าต่าง ลองปิดหน้าต่าง",
    "outdoor_infiltration": "ฝุ่นข้างนอกสูงและรั่วเข้ามาในห้อง ปิดประตูหน้าต่างให้สนิท",
    "unknown": "ยังไม่แน่ใจว่าฝุ่นมาจากไหน เครื่องจะฟอกอากาศต่อไป",
}
DEFAULTS = {
    "spike_ugm3": 15, "spike_window_s": 300, "humidity_rise_pct": 5, "slow_rise_ugm3": 10,
    "slow_window_s": 1800, "outdoor_ratio": 0.8, "min_pm": 25, "check_every_s": 300, "history_s": 1800,
    "outdoor_pm_url": "", "outdoor_pm_key": "pm25", "outdoor_every_s": 900,
}


def _value_at_or_after(series, t):
    """First value in a (ts, value) series with ts >= t (the oldest point inside the window)."""
    for ts, value in series:
        if ts >= t:
            return value
    return None


def features(pm, rh, now, cfg, outdoor_pm=None):
    """Numbers the rules look at. pm and rh are lists of (ts, value), oldest first."""
    if not pm:
        return None
    spike_start = now - cfg["spike_window_s"]
    recent = [v for ts, v in pm if ts >= spike_start]
    pm_now = pm[-1][1]
    f = {
        "pm_now": round(pm_now, 1),
        "pm_rise_fast": round(pm_now - min(recent), 1) if recent else 0.0,
        "pm_rise_slow": None,
        "rh_rise_fast": None,
        "outdoor_pm": outdoor_pm,
    }
    slow_start = _value_at_or_after(pm, now - cfg["slow_window_s"])
    if slow_start is not None:
        f["pm_rise_slow"] = round(pm_now - slow_start, 1)
    rh_recent = [v for ts, v in rh if ts >= spike_start]
    if rh_recent:
        f["rh_rise_fast"] = round(rh_recent[-1] - min(rh_recent), 1)
    return f


def classify(f, cfg):
    """features -> (cause, confidence 0..1). Pure function, no I/O."""
    if f is None:
        return "unknown", 0.0
    spike = f["pm_rise_fast"]
    if spike > cfg["spike_ugm3"]:
        strength = min(1.0, (spike - cfg["spike_ugm3"]) / (2.0 * cfg["spike_ugm3"]))
        rh_rise = f["rh_rise_fast"]
        if rh_rise is not None and rh_rise >= cfg["humidity_rise_pct"]:
            return "cooking", round(0.6 + 0.25 * strength, 2)
        if rh_rise is None:
            return "incense_candle", round(0.35 + 0.15 * strength, 2)
        return "incense_candle", round(0.5 + 0.2 * strength, 2)

    slow = f["pm_rise_slow"]
    outdoor = f["outdoor_pm"]
    if slow is not None and slow > cfg["slow_rise_ugm3"]:
        if outdoor is not None and outdoor >= cfg["outdoor_ratio"] * f["pm_now"]:
            if outdoor >= f["pm_now"]:
                return "outdoor_infiltration", 0.7
            return "window_open", 0.6
        return "unknown", 0.3
    return "unknown", 0.2


class OutdoorPm(object):
    """Outdoor PM2.5 from a JSON API (URL and key in config), cached; None when unknown or offline."""

    def __init__(self, url, key, every_s, session=None, clock=time.time):
        self.url = url
        self.key = key
        self.every_s = every_s
        self.session = session
        self.clock = clock
        self._value = None
        self._fetched = -1e12

    def get(self):
        if not self.url:
            return None
        if self.clock() - self._fetched < self.every_s:
            return self._value
        self._fetched = self.clock()
        try:
            if self.session is None:
                import requests
                self.session = requests.Session()
            data = self.session.get(self.url, timeout=5).json()
            for part in self.key.split("."):
                data = data[int(part)] if isinstance(data, list) else data[part]
            self._value = float(data)
        except Exception as e:
            log.warning("outdoor PM unavailable: %s", e)  # keep the last good value
        return self._value


class CauseRules(object):
    def __init__(self, cfg, mqtt_client, outdoor=None, clock=time.time):
        c = dict(DEFAULTS)
        c.update(cfg.get("cause") or {})
        self.cfg = c
        self.mqtt = mqtt_client
        self.clock = clock
        self.outdoor = outdoor or OutdoorPm(c["outdoor_pm_url"], c["outdoor_pm_key"], c["outdoor_every_s"],
                                            clock=clock)
        self._pm = deque()
        self._rh = deque()
        self._lock = threading.Lock()
        self._in_spike = False
        self._last_check = -1e12

    def start(self):
        self.mqtt.subscribe(TOPIC_NODE1_TELEMETRY, self.on_telemetry)
        self.mqtt.subscribe(TOPIC_GW_ENV, self.on_env)
        self.mqtt.subscribe(TOPIC_CAUSE_REQUEST, self.on_request)

    def _ts(self, msg):
        ts = msg.get("ts")
        return float(ts) if isinstance(ts, (int, float)) else self.clock()

    def _push(self, series, ts, value):
        series.append((ts, float(value)))
        while series and series[0][0] < ts - self.cfg["history_s"]:
            series.popleft()

    def on_env(self, topic, msg):
        if isinstance(msg.get("rh"), (int, float)):
            with self._lock:
                self._push(self._rh, self._ts(msg), msg["rh"])

    def on_telemetry(self, topic, msg):
        pm = msg.get("pm25")
        if not isinstance(pm, (int, float)):
            return
        now = self._ts(msg)
        with self._lock:
            self._push(self._pm, now, pm)
            f = features(list(self._pm), list(self._rh), now, self.cfg)
        spiking = f["pm_rise_fast"] > self.cfg["spike_ugm3"]
        if spiking and not self._in_spike:
            self._in_spike = True
            self.evaluate("spike")
        elif not spiking:
            self._in_spike = False
            if pm >= self.cfg["min_pm"] and now - self._last_check >= self.cfg["check_every_s"]:
                self.evaluate("periodic")

    def on_request(self, topic, msg):
        self.evaluate(msg.get("reason", "request"))

    def evaluate(self, reason):
        """Classify now and publish ab/gw/cause. Returns the published message."""
        now = self.clock()
        with self._lock:
            pm, rh = list(self._pm), list(self._rh)
        if pm:
            now = pm[-1][0]
        self._last_check = now
        f = features(pm, rh, now, self.cfg, self.outdoor.get())
        cause, confidence = classify(f, self.cfg)
        msg = {"ts": int(now), "id": "c%d" % int(now), "cause": cause, "confidence": confidence,
               "source": "rules", "advice_th": ADVICE_TH[cause], "features": f or {}, "reason": reason}
        log.info("cause %s (%.2f) from %s: %s", cause, confidence, reason, f)
        self.mqtt.publish(TOPIC_GW_CAUSE, msg)
        return msg


def main():
    from common.config import load_config
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    cfg = load_config()
    setup_logging("cause_rules", cfg)
    client = MqttClient("cause_rules", cfg)
    client.start(wait=False)
    rules = CauseRules(cfg, client)
    rules.start()
    log.info("cause_rules running")
    try:
        run_until_signal()
    finally:
        client.stop()


if __name__ == "__main__":
    main()
