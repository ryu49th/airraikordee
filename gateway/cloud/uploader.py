"""Store-and-forward upload of MQTT data to Firebase Realtime Database (E4).

Every message from the routed topics is written to a SQLite `outbox` on disk first. A worker sends the oldest
rows in one multi-path PATCH over HTTPS and deletes them only after HTTP 200; on failure it waits with
exponential backoff (1 s -> 5 min). Nothing is lost while the internet is down or the service restarts.

Firebase layout per route (config.yaml `uploader.routes`):
  <path>/latest                  newest message
  <path>/history/<ms>_<rowid>    history rows (keys sort by time)
  health/uploader                backlog stats for the dashboard

Run on the Jetson from gateway/:  python3 -m cloud.uploader
"""
import json
import logging
import os
import random
import sqlite3
import threading
import time

from common.protocol import TOPIC_GW_UPLOADER, clean_json, encode, now_ts

log = logging.getLogger("uploader")


class Outbox(object):
    """SQLite queue of records waiting for upload. Safe to use from the MQTT and worker threads."""

    def __init__(self, path, max_rows=500000):
        self.max_rows = max_rows
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "path TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL, history INTEGER NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS dead (id INTEGER PRIMARY KEY, path TEXT, ts REAL, "
                         "payload TEXT, history INTEGER, error TEXT, failed_at REAL)")
        self._db.commit()
        self._adds = 0

    def add(self, path, ts, payload, history):
        with self._lock:
            self._db.execute("INSERT INTO outbox (path, ts, payload, history) VALUES (?, ?, ?, ?)",
                             (path, ts, payload, 1 if history else 0))
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
            return self._db.execute("SELECT id, path, ts, payload, history FROM outbox ORDER BY id LIMIT ?",
                                    (limit,)).fetchall()

    def delete(self, ids):
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])
            self._db.commit()

    def move_to_dead(self, row, error):
        with self._lock:
            self._db.execute("INSERT INTO dead (id, path, ts, payload, history, error, failed_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(row) + (error[:500], time.time()))
            self._db.execute("DELETE FROM outbox WHERE id = ?", (row[0],))
            self._db.commit()

    def pending(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def close(self):
        with self._lock:
            self._db.close()


class FirebaseRest(object):
    """Minimal Realtime Database REST client: one multi-path PATCH at the root."""

    def __init__(self, db_url, token="", timeout=10.0):
        import requests
        self._requests = requests
        self._session = requests.Session()
        self.url = db_url.rstrip("/") + "/.json"
        self.token = token
        self.timeout = timeout

    def patch(self, updates):
        """Returns (status_code, body). Network errors return (None, error text)."""
        params = {"auth": self.token} if self.token else None
        data = json.dumps(updates, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            r = self._session.patch(self.url, params=params, data=data, timeout=self.timeout,
                                    headers={"Content-Type": "application/json"})
        except self._requests.RequestException as e:
            return None, str(e)
        return r.status_code, r.text[:300]


def history_key(ts, row_id):
    return "%013d_%d" % (int(ts * 1000), row_id)


def build_updates(rows):
    """Rows (oldest first) -> one Firebase multi-path update. The newest row of each path wins `latest`."""
    updates = {}
    for row_id, path, ts, payload, history in rows:
        doc = json.loads(payload)
        if history:
            updates["%s/history/%s" % (path, history_key(ts, row_id))] = doc
        updates[path + "/latest"] = doc
    return updates


class Uploader(object):
    def __init__(self, cfg, mqtt_client, outbox, firebase, clock=time.time):
        u = cfg.get("uploader", {})
        self.routes = u.get("routes", [])
        self.batch_size = int(u.get("batch_size", 200))
        self.poll_s = float(u.get("poll_s", 1.0))
        self.backoff_min = float(u.get("backoff_min_s", 1))
        self.backoff_max = float(u.get("backoff_max_s", 300))
        self.stats_every_s = float(u.get("stats_every_s", 10))

        self.mqtt = mqtt_client
        self.outbox = outbox
        self.firebase = firebase
        self.clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._last_seen = {}
        self._last_history = {}
        self._backoff = self.backoff_min
        self._batch_cap = self.batch_size
        self._last_stats = 0.0
        self.stats = {"queued": 0, "sent": 0, "dead": 0, "failures": 0, "last_ok_ts": None, "last_error": None}

    def start(self):
        for route in self.routes:
            self.mqtt.subscribe(route["topic"], self._handler_for(route))
        self._thread = threading.Thread(target=self._worker, name="uploader", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(5.0)

    # --- MQTT -> outbox ----------------------------------------------------------------------------

    def _handler_for(self, route):
        def handler(topic, msg):
            self.ingest(route, msg)
        return handler

    def ingest(self, route, msg):
        """Queue one message for upload. Returns True if it was kept."""
        path = route["path"]
        now = self.clock()
        every = float(route.get("every_s", 0))
        if every and now - self._last_seen.get(path, -1e12) < every:
            return False
        self._last_seen[path] = now

        history = bool(route.get("history", False))
        if history:
            hist_every = float(route.get("history_every_s", 0))
            if hist_every and now - self._last_history.get(path, -1e12) < hist_every:
                history = False
            else:
                self._last_history[path] = now

        ts = msg.get("ts")
        if not isinstance(ts, (int, float)):
            ts = now
        self.outbox.add(path, float(ts), encode(clean_json(msg)), history)
        self.stats["queued"] += 1
        return True

    # --- outbox -> Firebase ------------------------------------------------------------------------

    def _worker(self):
        while not self._stop.is_set():
            try:
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

        updates = build_updates(rows)
        updates["health/uploader"] = self._health(len(rows))
        status, body = self.firebase.patch(updates)

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

    def _health(self, in_flight):
        return {"ts": now_ts(), "pending": self.outbox.pending(), "in_flight": in_flight,
                "sent_total": self.stats["sent"], "last_ok_ts": self.stats["last_ok_ts"],
                "last_error": self.stats["last_error"]}

    def _maybe_publish_stats(self):
        now = self.clock()
        if now - self._last_stats < self.stats_every_s:
            return
        self._last_stats = now
        self.mqtt.publish(TOPIC_GW_UPLOADER, self._health(0))


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
