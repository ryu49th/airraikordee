"""Store-and-forward upload of MQTT data to Firebase Realtime Database (E4).

Every record is written to a SQLite `outbox` on disk first. A worker sends the oldest rows in one multi-path
PATCH over HTTPS and deletes them only after HTTP 200; on failure it waits with exponential backoff
(1 s -> 5 min). Nothing is lost while the internet is down or the service restarts.

Firebase paths and records follow docs/protocol.md ("Firebase Realtime Database"):
  telemetry/node1, telemetry/node2, env, state   overwrite, at most every rates_s (5 s)
  health                                          overwrite, every 30 s
  cause/latest, cause/history/{id}                every result
  alerts/{key}                                    each new item in state.alerts, and each ab/gw/alert
  history/m1/{ts}, history/m15/{ts}               1-min / 15-min averages, old keys deleted once a day
Payloads are the MQTT payloads without node_id and seq.

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
                             TOPIC_GW_UPLOADER, TOPIC_NODE1_TELEMETRY, TOPIC_NODE2_TELEMETRY, clean_json, encode,
                             now_ts)

log = logging.getLogger("uploader")

# MQTT topic -> Firebase path for the records that are simply overwritten.
OVERWRITE = {
    TOPIC_NODE1_TELEMETRY: "telemetry/node1",
    TOPIC_NODE2_TELEMETRY: "telemetry/node2",
    TOPIC_GW_ENV: "env",
    TOPIC_GW_STATE: "state",
    TOPIC_GW_HEALTH: "health",
}
DEFAULT_RATES = {"telemetry/node1": 5, "telemetry/node2": 5, "env": 5, "state": 5, "health": 30}
# Which field of which topic feeds the history charts.
HISTORY_FIELDS = {
    TOPIC_NODE1_TELEMETRY: ("pm25",),
    TOPIC_GW_STATE: ("pm25_corrected", "fan_pct"),
    TOPIC_GW_ENV: ("temp_c", "rh"),
}
DEFAULT_HISTORY = {"m1": {"bucket_s": 60, "keep_s": 48 * 3600}, "m15": {"bucket_s": 900, "keep_s": 30 * 86400}}
ALERT_FIELDS = ("ts", "type", "severity", "msg", "discord_sent")


class Outbox(object):
    """SQLite queue of (Firebase path, JSON) waiting for upload. Safe to use from the MQTT and worker threads."""

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


class FirebaseRest(object):
    """Minimal Realtime Database REST client."""

    def __init__(self, db_url, token="", timeout=10.0):
        import requests
        self._requests = requests
        self._session = requests.Session()
        self.base = db_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method, path, params=None, data=None):
        """Returns (status_code, body). Network errors return (None, error text)."""
        params = dict(params or {})
        if self.token:
            params["auth"] = self.token
        try:
            r = self._session.request(method, "%s/%s.json" % (self.base, path), params=params or None, data=data,
                                      timeout=self.timeout, headers={"Content-Type": "application/json"})
        except self._requests.RequestException as e:
            return None, str(e)
        return r.status_code, r.text

    def patch(self, updates):
        """One multi-path update at the root. A None value deletes that path."""
        data = json.dumps(updates, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        status, body = self._call("PATCH", "", data=data)
        return status, body[:300]

    def shallow_keys(self, path):
        """Child keys of `path` (without their data), or None on error."""
        status, body = self._call("GET", path, params={"shallow": "true"})
        if status != 200:
            return None
        return list((json.loads(body) or {}).keys())


def build_updates(rows):
    """Rows (oldest first) -> one Firebase multi-path update. A later row for the same path wins."""
    updates = {}
    for _, path, _, payload in rows:
        updates[path] = json.loads(payload)
    return updates


def strip_ids(msg):
    return dict((k, v) for k, v in msg.items() if k not in ("node_id", "seq"))


class Buckets(object):
    """Averages of a few fields over fixed time buckets (history/m1, history/m15)."""

    def __init__(self, bucket_s):
        self.bucket_s = int(bucket_s)
        self.start = None
        self._closed_before = 0
        self._sums = {}
        self._counts = {}

    def add(self, ts, values):
        """Add values seen at ts. Returns a finished (key, record) when ts opens a new bucket, else None."""
        start = int(ts) // self.bucket_s * self.bucket_s
        done = None
        if start < self._closed_before or (self.start is not None and start < self.start):
            return None  # late record for a bucket that is already closed
        if self.start is not None and start > self.start:
            done = self._finish()
        self.start = start
        for name, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._sums[name] = self._sums.get(name, 0.0) + value
                self._counts[name] = self._counts.get(name, 0) + 1
        return done

    def flush(self, now):
        """Close the open bucket once its time is over (so the chart does not wait for the next message)."""
        if self.start is not None and now >= self.start + self.bucket_s:
            done = self._finish()
            self.start = None
            return done
        return None

    def _finish(self):
        record = dict((name, round(self._sums[name] / self._counts[name], 2)) for name in self._sums)
        key = "%010d" % self.start
        self._closed_before = self.start + self.bucket_s
        self._sums, self._counts = {}, {}
        return (key, record) if record else None


class Uploader(object):
    def __init__(self, cfg, mqtt_client, outbox, firebase, clock=time.time):
        u = cfg.get("uploader", {})
        self.batch_size = int(u.get("batch_size", 200))
        self.poll_s = float(u.get("poll_s", 1.0))
        self.backoff_min = float(u.get("backoff_min_s", 1))
        self.backoff_max = float(u.get("backoff_max_s", 300))
        self.stats_every_s = float(u.get("stats_every_s", 10))
        self.retention_every_s = float(u.get("retention_every_s", 86400))
        self.rates = dict(DEFAULT_RATES)
        self.rates.update(u.get("rates_s") or {})
        history = u.get("history") or DEFAULT_HISTORY
        self.history = dict((name, Buckets(h["bucket_s"])) for name, h in history.items())
        self.keep_s = dict((name, float(h["keep_s"])) for name, h in history.items())

        self.mqtt = mqtt_client
        self.outbox = outbox
        self.firebase = firebase
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_sent = {}
        self._active_alerts = set()
        self._alert_n = 0
        self._backoff = self.backoff_min
        self._batch_cap = self.batch_size
        self._last_stats = 0.0
        self._last_retention = None
        self.stats = {"queued": 0, "sent": 0, "dead": 0, "failures": 0, "last_ok_ts": None, "last_error": None}

    def start(self):
        for topic in list(OVERWRITE) + [TOPIC_GW_CAUSE, TOPIC_GW_ALERT]:
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
            if topic in HISTORY_FIELDS:
                self._add_history(msg, HISTORY_FIELDS[topic])
            if topic == TOPIC_GW_STATE:
                self._state_alerts(msg)
            if topic in OVERWRITE:
                self._overwrite(OVERWRITE[topic], msg)
            elif topic == TOPIC_GW_CAUSE:
                self._cause(msg)
            elif topic == TOPIC_GW_ALERT:
                self._alert(msg)

    def _ts(self, msg):
        ts = msg.get("ts")
        return float(ts) if isinstance(ts, (int, float)) else self.clock()

    def _queue(self, path, ts, record):
        self.outbox.add(path, ts, encode(record))
        self.stats["queued"] += 1

    def _overwrite(self, path, msg):
        now = self.clock()
        rate = float(self.rates.get(path, 0))
        if rate and now - self._last_sent.get(path, -1e12) < rate:
            return
        self._last_sent[path] = now
        record = strip_ids(msg)
        if path == "state":
            record["outbox_pending"] = self.outbox.pending()
            record["last_upload_ts"] = self.stats["last_ok_ts"]
        self._queue(path, self._ts(msg), record)

    def _cause(self, msg):
        record = strip_ids(msg)
        record.pop("snapshot_path", None)  # local Jetson path; snapshots never go to Firebase
        ts = self._ts(msg)
        record.setdefault("id", "c%d" % int(ts))
        self._queue("cause/latest", ts, record)
        self._queue("cause/history/" + str(record["id"]), ts, record)

    def _alert(self, msg):
        record = dict((k, msg[k]) for k in ALERT_FIELDS if k in msg)
        ts = self._ts(msg)
        record.setdefault("ts", ts)
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
                record = dict(item)
                record["ts"] = item.get("since", msg.get("ts"))
                record.pop("since", None)
                self._alert(record)
        self._active_alerts = active

    def _add_history(self, msg, fields):
        values = dict((f, msg[f]) for f in fields if f in msg)
        if not values:
            return
        ts = self._ts(msg)
        for name, buckets in self.history.items():
            self._queue_bucket(name, buckets.add(ts, values))

    def _queue_bucket(self, name, done):
        if done:
            key, record = done
            self._queue("history/%s/%s" % (name, key), float(key), record)

    def flush_history(self):
        with self._lock:
            now = self.clock()
            for name, buckets in self.history.items():
                self._queue_bucket(name, buckets.flush(now))

    # --- outbox -> Firebase ------------------------------------------------------------------------

    def _worker(self):
        while not self._stop.is_set():
            try:
                self.flush_history()
                delay = self.step()
                self._maybe_clean_history()
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
            log.error("firebase refused auth (%s); check FIREBASE_TOKEN and database rules", status)
        else:
            log.warning("upload failed (%s %s), %d rows waiting, retry in %.0f s",
                        status, body, len(rows), delay)
        return delay * random.uniform(0.8, 1.2)

    def _maybe_clean_history(self):
        now = self.clock()
        if self._last_retention is not None and now - self._last_retention < self.retention_every_s:
            return
        if self.stats["last_ok_ts"] is None:
            return  # wait until Firebase is reachable
        self._last_retention = now
        self.clean_history(now)

    def clean_history(self, now):
        """Delete history keys older than keep_s. Returns how many were deleted (None if Firebase failed)."""
        deleted = 0
        for name, keep in self.keep_s.items():
            keys = self.firebase.shallow_keys("history/" + name)
            if keys is None:
                return None
            old = [k for k in keys if k.isdigit() and int(k) < now - keep]
            if old:
                status, body = self.firebase.patch(dict(("history/%s/%s" % (name, k), None) for k in old))
                if status != 200:
                    log.warning("history cleanup of %s failed: %s %s", name, status, body)
                    return None
                deleted += len(old)
        if deleted:
            log.info("deleted %d old history keys", deleted)
        return deleted

    def _maybe_publish_stats(self):
        now = self.clock()
        if now - self._last_stats < self.stats_every_s:
            return
        self._last_stats = now
        self.mqtt.publish(TOPIC_GW_UPLOADER, {"ts": now_ts(), "outbox_pending": self.outbox.pending(),
                                              "last_upload_ts": self.stats["last_ok_ts"],
                                              "last_error": self.stats["last_error"]})


def main():
    from common.config import load_config, secret, writable_dir
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    cfg = load_config()
    setup_logging("uploader", cfg)
    u = cfg.get("uploader", {})
    db_path = os.path.join(writable_dir(cfg.get("data_dir", "/var/lib/airraikordee"), "data"),
                           u.get("db_file", "outbox.db"))
    db_url = secret("FIREBASE_DB_URL")
    if not db_url:
        log.error("FIREBASE_DB_URL is not set in .env; data is only stored locally until it is")
    outbox = Outbox(db_path, int(u.get("max_rows", 500000)))
    firebase = FirebaseRest(db_url or "http://127.0.0.1:9", secret("FIREBASE_TOKEN"), float(u.get("timeout_s", 10)))

    client = MqttClient("uploader", cfg)
    client.start(wait=False)
    uploader = Uploader(cfg, client, outbox, firebase)
    uploader.start()
    log.info("uploader running, outbox %s (%d pending)", db_path, outbox.pending())

    try:
        run_until_signal(lambda: log.info("stats %s pending=%d", uploader.stats, outbox.pending()), tick_s=60)
    finally:
        uploader.stop()
        client.stop()
        outbox.close()


if __name__ == "__main__":
    main()
