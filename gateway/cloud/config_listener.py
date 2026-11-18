"""Dashboard settings -> the unit (AIR-97, GW-17).

Polls Firebase devices/<deviceId>/config every few seconds with the device account (same sign-in as the
uploader), checks each field, and:
- publishes the settings in use on ab/gw/config (retained), for decision.py / health.py;
- while fan_mode is "manual", sends ab/node1/cmd {"cmd": "fan", "pct": manual_fan_pct} when the value changes
  and again every manual_resend_s (Node 1 forgets it after a reset).

Fields (Farn's data contract): pm_alert, night_start, night_end, manual_fan_pct, fan_mode, filter_reset_ts.
A missing or invalid field falls back to config.yaml. If Firebase cannot be reached the last good settings stay
in use (and survive a restart through a small cache file), so the unit never stops because the internet is down.

Run on the Jetson from gateway/:  python3 -m cloud.config_listener
"""
import json
import logging
import os
import re
import threading
import time

from common.protocol import TOPIC_GW_CONFIG, TOPIC_NODE1_CMD, SeqCounter, now_ts

log = logging.getLogger("config_listener")

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
FIELDS = ("pm_alert", "night_start", "night_end", "fan_mode", "manual_fan_pct", "filter_reset_ts")


def defaults_from(cfg):
    d = cfg.get("decision", {})
    return {
        "pm_alert": int(d.get("pm_alert", 35)),
        "night_start": str(d.get("night_start", "22:00")),
        "night_end": str(d.get("night_end", "06:00")),
        "fan_mode": "auto",
        "manual_fan_pct": None,
        "filter_reset_ts": None,
    }


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate(raw, defaults):
    """Firebase config -> (settings in use, list of field names that were rejected)."""
    out = dict(defaults)
    bad = []
    if not isinstance(raw, dict):
        return out, ([] if raw is None else ["config"])

    def take(name, ok):
        if name not in raw:
            return
        if raw[name] is None and name != "manual_fan_pct":
            return  # null = not set: keep the config.yaml value
        if ok(raw[name]):
            out[name] = raw[name]
        else:
            bad.append(name)

    take("pm_alert", lambda v: _is_int(v) and 5 <= v <= 500)
    take("night_start", lambda v: isinstance(v, str) and bool(HHMM.match(v)))
    take("night_end", lambda v: isinstance(v, str) and bool(HHMM.match(v)))
    take("fan_mode", lambda v: v in ("auto", "manual"))
    take("manual_fan_pct", lambda v: v is None or (_is_int(v) and 0 <= v <= 100))
    take("filter_reset_ts", lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0)
    if out["fan_mode"] == "manual" and out["manual_fan_pct"] is None:
        out["fan_mode"] = "auto"  # the contract: manual without a value means auto
    return out, bad


class ConfigListener(object):
    def __init__(self, cfg, mqtt, firebase, cache_path=None, clock=time.time):
        c = cfg.get("config_listener", {})
        self.poll_s = float(c.get("poll_s", 5))
        self.manual_resend_s = float(c.get("manual_resend_s", 60))
        self.backoff_max_s = float(c.get("backoff_max_s", 60))
        self.defaults = defaults_from(cfg)
        self.mqtt = mqtt
        self.firebase = firebase
        self.cache_path = cache_path
        self.clock = clock

        self.current = None        # settings in use (after validation)
        self.source = None         # "firebase", "cache" or "defaults"
        self._last_raw = object()  # last raw value seen from Firebase, to log changes once
        self._last_fan_sent = None
        self._last_fan_at = 0.0
        self._fails = 0
        self._seq = SeqCounter()
        self._stop = threading.Event()
        self._thread = None
        self.stats = {"polls": 0, "poll_errors": 0, "changes": 0, "fan_cmds": 0, "last_ok_ts": None,
                      "last_error": None}

    # --- lifecycle ---------------------------------------------------------------------------------

    def start(self):
        self.load_cache()
        self._thread = threading.Thread(target=self._loop, name="config_listener", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5.0)

    def _loop(self):
        while not self._stop.is_set():
            delay = self.step()
            self._stop.wait(delay)

    # --- one poll ----------------------------------------------------------------------------------

    def step(self):
        """Poll once and act. Returns the seconds to wait before the next poll."""
        self.stats["polls"] += 1
        status, body = self.firebase.get("config")
        if status == 200:
            if self._fails:
                log.info("Firebase reachable again after %d failed polls", self._fails)
            self._fails = 0
            self.stats["last_ok_ts"] = now_ts()
            self.apply(body, "firebase")
            return self.poll_s

        self._fails += 1
        self.stats["poll_errors"] += 1
        self.stats["last_error"] = "%s %s" % (status, body)
        if self._fails == 1 or self._fails % 20 == 0:
            log.warning("cannot read config (%s %s); keeping the %s settings", status, body,
                        self.source or "default")
        if self.current is None:
            self.apply(None, "defaults")
        else:
            self._keep_fan()
        return min(self.backoff_max_s, self.poll_s * (2 ** min(self._fails - 1, 4)))

    def apply(self, raw, source):
        settings, bad = validate(raw, self.defaults)
        if raw != self._last_raw:
            self._last_raw = raw
            if bad:
                log.warning("ignored invalid config fields %s, using config.yaml for them", ", ".join(bad))
            if source == "firebase" and isinstance(raw, dict):
                self._save_cache(raw)

        changed = settings != self.current
        if changed or source == "firebase":
            self.source = source
        if changed:
            old = self.current or {}
            self.current = settings
            self.stats["changes"] += 1
            diff = dict((k, settings[k]) for k in FIELDS if old.get(k) != settings[k])
            log.info("settings from %s: %s", source, json.dumps(diff, ensure_ascii=False))
            if old and old.get("filter_reset_ts") != settings["filter_reset_ts"]:
                log.info("filter replaced (filter_reset_ts=%s)", settings["filter_reset_ts"])
            self._publish_config()
        self._keep_fan()

    def _publish_config(self):
        msg = dict(self.current)
        msg["ts"] = now_ts()
        msg["source"] = self.source
        self.mqtt.publish(TOPIC_GW_CONFIG, msg, qos=1, retain=True)

    def _keep_fan(self):
        """In manual mode, make Node 1 run the fan at manual_fan_pct (on change and every manual_resend_s)."""
        s = self.current
        if not s or s["fan_mode"] != "manual":
            self._last_fan_sent = None
            return
        pct = s["manual_fan_pct"]
        now = self.clock()
        if pct == self._last_fan_sent and now - self._last_fan_at < self.manual_resend_s:
            return
        self.mqtt.publish(TOPIC_NODE1_CMD, {"seq": self._seq.next(), "ts": now_ts(), "cmd": "fan", "pct": pct})
        if pct != self._last_fan_sent:
            log.info("manual fan %d%%", pct)
        self._last_fan_sent = pct
        self._last_fan_at = now
        self.stats["fan_cmds"] += 1

    # --- cache (last good Firebase config, used at boot without internet) ---------------------------

    def load_cache(self):
        if not self.cache_path or not os.path.isfile(self.cache_path):
            return False
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("ignoring config cache %s: %s", self.cache_path, e)
            return False
        self.apply(raw, "cache")
        return True

    def _save_cache(self, raw):
        if not self.cache_path:
            return
        tmp = self.cache_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        except OSError as e:
            log.warning("cannot write config cache %s: %s", self.cache_path, e)


def main():
    import requests

    from cloud.uploader import DeviceAuth, FirebaseRest, device_email
    from common.config import load_config, secret, writable_dir
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    cfg = load_config()
    setup_logging("config_listener", cfg)
    c = cfg.get("config_listener", {})
    device_id = secret("DEVICE_ID", "airraikordee-01")
    missing = [k for k in ("FIREBASE_DB_URL", "FIREBASE_API_KEY", "DEVICE_PASSWORD") if not secret(k)]
    if missing:
        log.error("%s not set in .env; running on config.yaml settings only", ", ".join(missing))
    session = requests.Session()
    timeout = float(c.get("timeout_s", 10))
    auth = DeviceAuth(secret("FIREBASE_API_KEY"), device_email(device_id), secret("DEVICE_PASSWORD"), session,
                      timeout)
    firebase = FirebaseRest(secret("FIREBASE_DB_URL") or "http://127.0.0.1:9", device_id, auth, session, timeout)
    cache = os.path.join(writable_dir(cfg.get("data_dir", "/var/lib/airraikordee"), "data"),
                         c.get("cache_file", "config_cache.json"))

    client = MqttClient("config_listener", cfg)
    client.start(wait=False)
    listener = ConfigListener(cfg, client, firebase, cache)
    listener.start()
    log.info("config_listener polling devices/%s/config every %.0f s", device_id, listener.poll_s)

    try:
        run_until_signal(lambda: log.info("stats %s", listener.stats), tick_s=60)
    finally:
        listener.stop()
        client.stop()


if __name__ == "__main__":
    main()
