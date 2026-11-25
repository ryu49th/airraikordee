"""Dust-cause feedback from the dashboard -> the accuracy table on the Jetson (AIR-54 "done when").

The dashboard writes devices/<id>/feedback/<push id> = {cause_ts, correct, true_cause?} when someone presses
"ใช่" or "ไม่ใช่" on the latest dust cause. This keeps
  causes    every ab/gw/cause answer (ts, id, cause, confidence, source)   so we know what the user saw
  feedback  every feedback entry (key, cause_ts, correct, true_cause, shown cause and source)
in data_dir/cause_log.db, next to cause_cloud's answers table. Entries stay in Firebase (the dashboard reads them);
keys already stored are skipped. Runs inside the config_listener process, polling every `feedback_every_s`.

Report for the write-up:  python3 -m tools.accuracy_report
"""
import logging
import sqlite3
import threading
import time

from common.protocol import TOPIC_GW_CAUSE

log = logging.getLogger("feedback")


class FeedbackLog(object):
    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.execute("CREATE TABLE IF NOT EXISTS causes (ts INTEGER PRIMARY KEY, id TEXT, cause TEXT, "
                         "confidence REAL, source TEXT)")
        self._db.execute("CREATE TABLE IF NOT EXISTS feedback (key TEXT PRIMARY KEY, received REAL, cause_ts INTEGER, "
                         "correct INTEGER, true_cause TEXT, shown_cause TEXT, source TEXT)")
        self._db.commit()

    def add_cause(self, msg):
        if not isinstance(msg.get("ts"), (int, float)):
            return
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO causes VALUES (?, ?, ?, ?, ?)",
                             (int(msg["ts"]), msg.get("id"), msg.get("cause"), msg.get("confidence"),
                              msg.get("source")))
            self._db.commit()

    def known_keys(self):
        with self._lock:
            return set(r[0] for r in self._db.execute("SELECT key FROM feedback"))

    def add_feedback(self, key, entry, received):
        cause_ts = entry.get("cause_ts")
        with self._lock:
            shown = self._db.execute("SELECT cause, source FROM causes WHERE ts = ?", (cause_ts,)).fetchone()
            self._db.execute("INSERT OR IGNORE INTO feedback VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (key, received, cause_ts, 1 if entry.get("correct") else 0, entry.get("true_cause"),
                              shown[0] if shown else None, shown[1] if shown else None))
            self._db.commit()

    def accuracy(self):
        """{source: (answers rated, rated correct)} plus "all"."""
        with self._lock:
            rows = self._db.execute("SELECT COALESCE(source, 'unknown'), COUNT(*), SUM(correct) FROM feedback "
                                    "GROUP BY 1").fetchall()
        out = dict((r[0], (r[1], r[2] or 0)) for r in rows)
        out["all"] = (sum(v[0] for v in out.values()), sum(v[1] for v in out.values()))
        return out

    def mistakes(self):
        with self._lock:
            return self._db.execute("SELECT shown_cause, true_cause, COUNT(*) FROM feedback WHERE correct = 0 "
                                    "GROUP BY 1, 2 ORDER BY 3 DESC").fetchall()


class Feedback(object):
    def __init__(self, cfg, mqtt, firebase, log_db, clock=time.time):
        c = cfg.get("config_listener", {})
        self.every_s = float(c.get("feedback_every_s", 60))
        self.mqtt = mqtt
        self.firebase = firebase
        self.db = log_db
        self.clock = clock
        self._stop = threading.Event()
        self.stats = {"stored": 0, "polls": 0}

    def start(self):
        self.mqtt.subscribe(TOPIC_GW_CAUSE, self.on_cause)
        threading.Thread(target=self._loop, name="feedback", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.every_s):
            try:
                self.step()
            except Exception:
                log.exception("feedback poll failed")

    def on_cause(self, topic, msg):
        self.db.add_cause(msg)

    def step(self):
        """Store feedback entries not seen before. Returns how many were new."""
        self.stats["polls"] += 1
        status, body = self.firebase.get("feedback")
        if status != 200 or not isinstance(body, dict):
            return 0
        known = self.db.known_keys()
        new = 0
        for key in sorted(body):
            entry = body[key]
            if key in known or not isinstance(entry, dict) or not isinstance(entry.get("cause_ts"), (int, float)):
                continue
            self.db.add_feedback(key, entry, self.clock())
            new += 1
            log.info("feedback on cause %s: %s%s", entry.get("cause_ts"), "correct" if entry.get("correct") else "wrong",
                     (", really " + entry["true_cause"]) if entry.get("true_cause") else "")
        self.stats["stored"] += new
        return new
