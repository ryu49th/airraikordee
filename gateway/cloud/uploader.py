"""Store-and-forward upload of MQTT data to Firebase Realtime Database (E4).

Every record is written to a SQLite `outbox` on disk first. A worker sends the oldest rows in one multi-path
PATCH to devices/<deviceId> over HTTPS and deletes them only after HTTP 200; on failure it waits with
exponential backoff (1 s -> 5 min). Nothing is lost while the internet is down or the service restarts.

Paths and fields follow the Firebase data contract (docs/protocol.md, "Firebase Realtime Database"):
  telemetry/node1/latest   node1 telemetry + pm25_corrected/fan_pct/mode from state + person from vision
  telemetry/node2/latest   node2 telemetry + batt_temp_c from node1
  env/latest               BME280
  history/node1/<minute>   one row per minute (averages), key = unix seconds at the start of the minute
  state                    mode, fan_pct, target, alerts
  cause/latest             latest dust cause
  alerts/<key>             each new item in state.alerts, and each ab/gw/alert
  health                   every 30 s, with pending_rows and last_upload_ts from this uploader
The Jetson signs in with its device account (device-<deviceId>@devices.airraikordee.example) and refreshes
the 1-hour ID token before it expires, or after an HTTP 401.

Run on the Jetson from gateway/:  python3 -m cloud.uploader
"""
import json
import logging
import os
import random
import sqlite3
import threading
import time

from common.protocol import (TOPIC_GW_ALERT, TOPIC_GW_CAUSE, TOPIC_GW_ENV, TOPIC_GW_HEALTH, TOPIC_GW_STATE,
                             TOPIC_GW_UPLOADER, TOPIC_GW_VISION, TOPIC_NODE1_TELEMETRY, TOPIC_NODE2_TELEMETRY,
                             clean_json, encode, now_ts)

log = logging.getLogger("uploader")

# Field lists from the contract. Anything else in an MQTT message stays on the Jetson.
NODE1_FIELDS = ("ts", "pm25", "pm25_corrected", "pm10", "fan_pct", "fan_rpm", "pan_deg", "tilt_deg", "person",
                "mode")
NODE2_FIELDS = ("ts", "mode", "voltage", "current_a", "temp_c", "batt_temp_c", "plugged", "est_percent")
ENV_FIELDS = ("ts", "temp_c", "rh", "pressure_hpa")
HISTORY_FIELDS = ("pm25", "temp_c", "rh", "fan_pct")
STATE_FIELDS = ("mode", "fan_pct", "target", "alerts")
CAUSE_FIELDS = ("ts", "cause", "confidence", "source", "advice_th")
ALERT_FIELDS = ("ts", "level", "title", "text", "sent_to")
HEALTH_FIELDS = ("node1_online", "node2_online", "cpu_temp_c", "pending_rows", "last_upload_ts", "filter_hours",
                 "pms_hours")

DEFAULT_RATES = {"telemetry/node1/latest": 1, "telemetry/node2/latest": 1, "env/latest": 1, "state": 1}
ALERT_TITLES = {
    "pm25_high": "PM2.5 สูง", "pm_high": "PM2.5 สูง", "node_offline": "อุปกรณ์ออฟไลน์",
    "uart_fail": "Node 1 ไม่ตอบคำสั่ง", "charge_cut_temp": "หยุดชาร์จเพราะร้อนเกิน", "charge_fault": "ที่ชาร์จขัดข้อง",
    "battery_low": "แบตเตอรี่ต่ำ", "charge_full": "ชาร์จเต็มแล้ว", "filter_clogged": "ไส้กรองอุดตัน",
    "stop_pressed": "กดปุ่มหยุด",
}


def pick(msg, fields):
    return dict((k, msg[k]) for k in fields if k in msg)


class Outbox(object):
    """SQLite queue of (path under devices/<id>, JSON) waiting for upload. Thread-safe."""

    def __init__(self, path, max_rows=500000):
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "path TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS dead (id INTEGER PRIMARY KEY, path TEXT, ts REAL, "
                         "payload TEXT, error TEXT, failed_at REAL)")
        self._db.commit()
        self._adds = 0

    def add(self, path, ts, payload):
        with self._lock:
            self._db.execute("INSERT INTO outbox (path, ts, payload) VALUES (?, ?, ?)", (path, ts, payload))
            self._db.commit()
            self._adds += 1
            if self._adds % 1000 == 0:
                self._trim()

    def _trim(self):
        (count,) = self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()
        extra = count - self.max_rows
        if extra > 0:
            self._db.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox ORDER BY id LIMIT ?)", (extra,))
            self._db.commit()
            log.warning("outbox full: dropped %d oldest rows", extra)

    def fetch(self, limit):
        with self._lock:
            return self._db.execute("SELECT id, path, ts, payload FROM outbox ORDER BY id LIMIT ?",
                                    (limit,)).fetchall()

    def delete(self, ids):
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])
            self._db.commit()

    def move_to_dead(self, row, error):
        with self._lock:
            self._db.execute("INSERT INTO dead (id, path, ts, payload, error, failed_at) VALUES (?, ?, ?, ?, ?, ?)",
                             tuple(row) + (error[:500], time.time()))
            self._db.execute("DELETE FROM outbox WHERE id = ?", (row[0],))
            self._db.commit()

    def pending(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def close(self):
        with self._lock:
            self._db.close()


class DeviceAuth(object):
    """Firebase Auth (email/password) for the device account; keeps a fresh ID token."""

    SIGN_IN = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
    REFRESH = "https://securetoken.googleapis.com/v1/token"

    def __init__(self, api_key, email, password, session, timeout=10.0, clock=time.time):
        self.api_key = api_key
        self.email = email
        self.password = password
        self.session = session
        self.timeout = timeout
        self.clock = clock
        self._id_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self):
        """A valid ID token, or None if signing in failed (the caller backs off and tries again)."""
        with self._lock:
            if self._id_token and self.clock() < self._expires_at - 300:
                return self._id_token
            if self._refresh_token and self._refresh():
                return self._id_token
            self._sign_in()
            return self._id_token

    def invalidate(self):
        """Called after an HTTP 401: the next token() refreshes."""
        with self._lock:
            self._expires_at = 0.0

    def _sign_in(self):
        self._id_token = None
        try:
            r = self.session.post(self.SIGN_IN, params={"key": self.api_key}, timeout=self.timeout,
                                  json={"email": self.email, "password": self.password, "returnSecureToken": True})
            body = r.json()
        except Exception as e:
            log.warning("device sign-in failed: %s", e)
            return False
        if r.status_code != 200:
            log.error("device sign-in refused for %s: %s", self.email, body.get("error", {}).get("message", body))
            return False
        self._store(body["idToken"], body["refreshToken"], body.get("expiresIn", 3600))
        log.info("signed in as %s", self.email)
        return True

    def _refresh(self):
        try:
            r = self.session.post(self.REFRESH, params={"key": self.api_key}, timeout=self.timeout,
                                  data={"grant_type": "refresh_token", "refresh_token": self._refresh_token})
            body = r.json()
        except Exception as e:
            log.warning("token refresh failed: %s", e)
            return False
        if r.status_code != 200:
            log.warning("token refresh refused (%s), signing in again", body.get("error", {}).get("message", body))
            self._refresh_token = None
            return False
        self._store(body["id_token"], body["refresh_token"], body.get("expires_in", 3600))
        return True

    def _store(self, id_token, refresh_token, expires_in):
        self._id_token = id_token
        self._refresh_token = refresh_token
        self._expires_at = self.clock() + float(expires_in)


class FirebaseRest(object):
    """Realtime Database REST client rooted at devices/<deviceId>."""

    def __init__(self, db_url, device_id, auth=None, session=None, timeout=10.0):
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.url = "%s/devices/%s.json" % (db_url.rstrip("/"), device_id)
        self.auth = auth
        self.timeout = timeout

    def patch(self, updates):
        """One multi-path update under devices/<id>. Returns (status_code, body); (None, text) on errors."""
        data = json.dumps(updates, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        for attempt in (1, 2):
            params = None
            if self.auth is not None:
                token = self.auth.token()
                if token is None:
                    return None, "device sign-in failed"
                params = {"auth": token}
            try:
                r = self.session.patch(self.url, params=params, data=data, timeout=self.timeout,
                                       headers={"Content-Type": "application/json"})
            except Exception as e:
                return None, str(e)
            if r.status_code == 401 and self.auth is not None and attempt == 1:
                self.auth.invalidate()  # token expired early or was revoked: refresh once and retry
                continue
            return r.status_code, r.text[:300]


def build_updates(rows):
    """Rows (oldest first) -> one multi-path update. A later row for the same path wins."""
    updates = {}
    for _, path, _, payload in rows:
        updates[path] = json.loads(payload)
    return updates


class MinuteHistory(object):
    """Averages over one minute -> history/node1/<minute start>."""

    def __init__(self):
        self.minute = None
        self._closed_before = 0
        self._sums = {}
        self._counts = {}
        self._last_ts = None

    def add(self, ts, values):
        """Returns a finished (minute, record) when ts starts a new minute, else None."""
        minute = int(ts) - int(ts) % 60
        if minute < self._closed_before or (self.minute is not None and minute < self.minute):
            return None  # late record for a minute that is already written
        done = self._finish() if self.minute is not None and minute > self.minute else None
        self.minute = minute
        self._last_ts = ts
        for name, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._sums[name] = self._sums.get(name, 0.0) + value
                self._counts[name] = self._counts.get(name, 0) + 1
        return done

    def flush(self, now):
        """Write the open minute once it is over, so the chart does not wait for the next message."""
        if self.minute is not None and now >= self.minute + 60:
            done = self._finish()
            self.minute = None
            return done
        return None

    def _finish(self):
        record = dict((name, round(self._sums[name] / self._counts[name], 2)) for name in self._sums)
        minute = self.minute
        self._closed_before = minute + 60
        self._sums, self._counts = {}, {}
        if not record:
            return None
        record["ts"] = int(self._last_ts)
        return minute, record


class Uploader(object):
    def __init__(self, cfg, mqtt_client, outbox, firebase, clock=time.time):
        u = cfg.get("uploader", {})
        self.batch_size = int(u.get("batch_size", 200))
        self.poll_s = float(u.get("poll_s", 1.0))
        self.backoff_min = float(u.get("backoff_min_s", 1))
        self.backoff_max = float(u.get("backoff_max_s", 300))
        self.stats_every_s = float(u.get("stats_every_s", 10))
        self.health_every_s = float(u.get("health_every_s", 30))
        self.node_offline_s = float(u.get("node_offline_s", 30))
        self.rates = dict(DEFAULT_RATES)
        self.rates.update(u.get("rates_s") or {})

        self.mqtt = mqtt_client
        self.outbox = outbox
        self.firebase = firebase
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_sent = {}
        self._node1 = {}       # merged telemetry/node1/latest
        self._node2 = {}
        self._health = {}      # last ab/gw/health from health.py
        self._last_seen = {}   # node -> clock() of last telemetry
        self._active_alerts = set()
        self._alert_n = 0
        self._history = MinuteHistory()
        self._history_values = {}
        self._backoff = self.backoff_min
        self._batch_cap = self.batch_size
        self._last_stats = 0.0
        self._last_health = 0.0
        self.stats = {"queued": 0, "sent": 0, "dead": 0, "failures": 0, "last_ok_ts": None, "last_error": None}

    def start(self):
        for topic in (TOPIC_NODE1_TELEMETRY, TOPIC_NODE2_TELEMETRY, TOPIC_GW_ENV, TOPIC_GW_STATE, TOPIC_GW_VISION,
                      TOPIC_GW_CAUSE, TOPIC_GW_ALERT, TOPIC_GW_HEALTH):
            self.mqtt.subscribe(topic, self.on_message)
        self._thread = threading.Thread(target=self._worker, name="uploader", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5.0)

    # --- MQTT -> outbox ----------------------------------------------------------------------------

    def on_message(self, topic, msg):
        msg = clean_json(msg)
        with self._lock:
            handler = {
                TOPIC_NODE1_TELEMETRY: self._on_node1, TOPIC_NODE2_TELEMETRY: self._on_node2,
                TOPIC_GW_ENV: self._on_env, TOPIC_GW_STATE: self._on_state, TOPIC_GW_VISION: self._on_vision,
                TOPIC_GW_CAUSE: self._on_cause, TOPIC_GW_ALERT: self._on_alert, TOPIC_GW_HEALTH: self._on_health,
            }.get(topic)
            if handler:
                handler(msg)

    def _ts(self, msg):
        ts = msg.get("ts")
        return float(ts) if isinstance(ts, (int, float)) else self.clock()

    def _queue(self, path, ts, record):
        self.outbox.add(path, ts, encode(record))
        self.stats["queued"] += 1

    def _throttled(self, path, record, ts):
        now = self.clock()
        rate = float(self.rates.get(path, 0))
        if rate and now - self._last_sent.get(path, -1e12) < rate:
            return
        self._last_sent[path] = now
        self._queue(path, ts, record)

    def _on_node1(self, msg):
        self._last_seen["node1"] = self.clock()
        self._node1.update(pick(msg, NODE1_FIELDS))
        if "batt_temp_c" in msg:
            self._node2["batt_temp_c"] = msg["batt_temp_c"]
        self._add_history(msg)
        self._throttled("telemetry/node1/latest", dict(self._node1), self._ts(msg))

    def _on_node2(self, msg):
        self._last_seen["node2"] = self.clock()
        self._node2.update(pick(msg, NODE2_FIELDS))
        if "plugged" not in msg and "docked" in msg:
            self._node2["plugged"] = msg["docked"]
        self._throttled("telemetry/node2/latest", dict(self._node2), self._ts(msg))

    def _on_env(self, msg):
        self._add_history(msg)
        self._throttled("env/latest", pick(msg, ENV_FIELDS), self._ts(msg))

    def _on_state(self, msg):
        for name in ("pm25_corrected", "fan_pct", "mode"):
            if name in msg:
                self._node1[name] = msg[name]
        self._add_history(msg)
        self._state_alerts(msg)
        record = pick(msg, STATE_FIELDS)
        record["alerts"] = [a.get("type") if isinstance(a, dict) else a for a in record.get("alerts") or []]
        self._throttled("state", record, self._ts(msg))

    def _on_vision(self, msg):
        if "person_present" in msg:
            self._node1["person"] = bool(msg["person_present"])

    def _on_cause(self, msg):
        record = pick(msg, CAUSE_FIELDS)  # snapshot_path stays local: snapshots never go to Firebase
        record.setdefault("ts", int(self._ts(msg)))
        self._queue("cause/latest", self._ts(msg), record)

    def _on_alert(self, msg):
        """ab/gw/alert {ts, type, severity, msg} or a contract record {ts, level, title, text, sent_to}."""
        record = pick(msg, ALERT_FIELDS)
        record.setdefault("level", msg.get("severity", "warn"))
        record.setdefault("title", ALERT_TITLES.get(msg.get("type"), msg.get("type", "แจ้งเตือน")))
        if "text" not in record and "msg" in msg:
            record["text"] = msg["msg"]
        ts = self._ts(msg)
        record["ts"] = int(ts)
        self._alert_n = (self._alert_n + 1) % 1000
        self._queue("alerts/%013d%03d" % (int(ts * 1000), self._alert_n), ts, record)

    def _state_alerts(self, msg):
        """One alerts/ entry when an item first appears in state.alerts."""
        active = set()
        for item in msg.get("alerts") or []:
            if isinstance(item, str):
                item = {"type": item}
            if not isinstance(item, dict) or "type" not in item:
                continue
            active.add(item["type"])
            if item["type"] not in self._active_alerts:
                alert = dict(item)
                alert["ts"] = item.get("since", msg.get("ts"))
                self._on_alert(alert)
        self._active_alerts = active

    def _on_health(self, msg):
        self._health = dict(msg)
        nodes = msg.get("nodes") or {}
        for node in ("node1", "node2"):
            if isinstance(nodes.get(node), dict) and "online" in nodes[node]:
                self._health[node + "_online"] = nodes[node]["online"]

    def _add_history(self, msg):
        for name in HISTORY_FIELDS:
            if name in msg:
                self._history_values[name] = msg[name]
        values = pick(msg, HISTORY_FIELDS)
        if values:
            self._queue_minute(self._history.add(self._ts(msg), values))

    def _queue_minute(self, done):
        if done:
            minute, record = done
            self._queue("history/node1/%d" % minute, float(minute), record)

    def tick(self):
        """Periodic work from the worker: close the finished minute and write health."""
        with self._lock:
            now = self.clock()
            self._queue_minute(self._history.flush(now))
            if now - self._last_health >= self.health_every_s:
                self._last_health = now
                record = pick(self._health, HEALTH_FIELDS)
                for node in ("node1", "node2"):
                    if node + "_online" not in record:
                        record[node + "_online"] = now - self._last_seen.get(node, -1e12) < self.node_offline_s
                record["pending_rows"] = self.outbox.pending()
                record["last_upload_ts"] = int(self.stats["last_ok_ts"]) if self.stats["last_ok_ts"] else None
                self._queue("health", now, record)

    # --- outbox -> Firebase ------------------------------------------------------------------------

    def _worker(self):
        while not self._stop.is_set():
            try:
                self.tick()
                delay = self.step()
            except Exception:
                log.exception("upload step failed")
                delay = self.backoff_max
            self._maybe_publish_stats()
            if delay:
                self._stop.wait(delay)

    def step(self):
        """Try to upload one batch. Returns how long to wait before the next try (0 = go again now)."""
        rows = self.outbox.fetch(self._batch_cap)
        if not rows:
            return self.poll_s

        status, body = self.firebase.patch(build_updates(rows))

        if status == 200:
            self.outbox.delete([r[0] for r in rows])
            self.stats["sent"] += len(rows)
            self.stats["last_ok_ts"] = now_ts()
            self.stats["last_error"] = None
            self._backoff = self.backoff_min
            self._batch_cap = min(self.batch_size, self._batch_cap * 2)
            return 0

        self.stats["failures"] += 1
        self.stats["last_error"] = "%s %s" % (status, body)
        if status == 400:
            # Firebase rejected the content itself; retrying the same batch would block the queue forever.
            if len(rows) > 1:
                self._batch_cap = max(1, len(rows) // 2)
                log.warning("firebase 400 on %d rows, retrying in smaller batches: %s", len(rows), body)
                return 0
            log.error("firebase rejected row %d (%s), moved to dead table: %s", rows[0][0], rows[0][1], body)
            self.outbox.move_to_dead(rows[0], body or "400")
            self.stats["dead"] += 1
            self._batch_cap = self.batch_size
            return 0

        delay = self._backoff
        self._backoff = min(self.backoff_max, self._backoff * 2)
        if status in (401, 403):
            log.error("firebase refused the write (%s %s); check the device account and the database rules",
                      status, body)
        else:
            log.warning("upload failed (%s %s), %d rows waiting, retry in %.0f s",
                        status, body, len(rows), delay)
        return delay * random.uniform(0.8, 1.2)

    def _maybe_publish_stats(self):
        now = self.clock()
        if now - self._last_stats < self.stats_every_s:
            return
        self._last_stats = now
        self.mqtt.publish(TOPIC_GW_UPLOADER, {"ts": now_ts(), "outbox_pending": self.outbox.pending(),
                                              "last_upload_ts": self.stats["last_ok_ts"],
                                              "last_error": self.stats["last_error"]})


def device_email(device_id):
    return "device-%s@devices.airraikordee.example" % device_id


def main():
    import requests

    from common.config import load_config, secret, writable_dir
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    cfg = load_config()
    setup_logging("uploader", cfg)
    u = cfg.get("uploader", {})
    db_path = os.path.join(writable_dir(cfg.get("data_dir", "/var/lib/airraikordee"), "data"),
                           u.get("db_file", "outbox.db"))
    outbox = Outbox(db_path, int(u.get("max_rows", 500000)))

    db_url = secret("FIREBASE_DB_URL")
    device_id = secret("DEVICE_ID", "airraikordee-01")
    missing = [k for k in ("FIREBASE_DB_URL", "FIREBASE_API_KEY", "DEVICE_PASSWORD") if not secret(k)]
    if missing:
        log.error("%s not set in .env; data is only stored locally until they are", ", ".join(missing))
    session = requests.Session()
    timeout = float(u.get("timeout_s", 10))
    auth = DeviceAuth(secret("FIREBASE_API_KEY"), device_email(device_id), secret("DEVICE_PASSWORD"), session,
                      timeout)
    firebase = FirebaseRest(db_url or "http://127.0.0.1:9", device_id, auth, session, timeout)

    client = MqttClient("uploader", cfg)
    client.start(wait=False)
    uploader = Uploader(cfg, client, outbox, firebase)
    uploader.start()
    log.info("uploader running for devices/%s, outbox %s (%d pending)", device_id, db_path, outbox.pending())

    try:
        run_until_signal(lambda: log.info("stats %s pending=%d", uploader.stats, outbox.pending()), tick_s=60)
    finally:
        uploader.stop()
        client.stop()
        outbox.close()


if __name__ == "__main__":
    main()
