import pytest

from cloud.uploader import Buckets, Outbox, Uploader, build_updates
from common.protocol import (TOPIC_GW_ALERT, TOPIC_GW_CAUSE, TOPIC_GW_ENV, TOPIC_GW_STATE, TOPIC_NODE1_TELEMETRY,
                             TOPIC_NODE2_TELEMETRY)
from conftest import FakeMqtt

CFG = {"uploader": {"batch_size": 50, "backoff_min_s": 1, "backoff_max_s": 8}}


class FakeFirebase(object):
    """Fails with the given statuses first, then returns 200. Applies updates to a flat dict of paths."""

    def __init__(self, failures=(), keys=None):
        self.failures = list(failures)
        self.stored = {}
        self.keys = keys or {}
        self.calls = 0

    def patch(self, updates):
        self.calls += 1
        if self.failures:
            status = self.failures.pop(0)
            return status, "error" if status else "connection refused"
        for path, value in updates.items():
            if value is None:
                self.stored.pop(path, None)
            else:
                self.stored[path] = value
        return 200, "{}"

    def shallow_keys(self, path):
        return list(self.keys.get(path, []))

    def under(self, prefix):
        return dict((k, v) for k, v in self.stored.items() if k.startswith(prefix))


class Clock(object):
    def __init__(self):
        self.t = 1794384000.0  # start of a 15-min bucket

    def __call__(self):
        return self.t


@pytest.fixture
def outbox(tmp_path):
    box = Outbox(str(tmp_path / "outbox.db"))
    yield box
    box.close()


def make(outbox, firebase, cfg=CFG):
    clock = Clock()
    up = Uploader(cfg, FakeMqtt(), outbox, firebase, clock=clock)
    return up, clock


def drain(up, max_steps=50):
    delays = []
    for _ in range(max_steps):
        if not up.outbox.pending():
            break
        delays.append(up.step())
    return delays


def test_outbox_survives_restart(tmp_path):
    path = str(tmp_path / "outbox.db")
    box = Outbox(path)
    box.add("env", 1.0, '{"rh":50}')
    box.add("env", 2.0, '{"rh":51}')
    box.close()
    box = Outbox(path)
    rows = box.fetch(10)
    assert [r[3] for r in rows] == ['{"rh":50}', '{"rh":51}']
    box.delete([rows[0][0]])
    assert box.pending() == 1
    box.close()


def test_build_updates_later_row_wins():
    rows = [(1, "env", 10.0, '{"rh":1}'), (2, "env", 11.0, '{"rh":2}'), (3, "state", 11.0, '{"mode":"idle"}')]
    assert build_updates(rows) == {"env": {"rh": 2}, "state": {"mode": "idle"}}


def test_telemetry_overwrites_every_5s_without_ids(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    for i in range(12):
        up.on_message(TOPIC_NODE1_TELEMETRY, {"node_id": "node1", "seq": i, "ts": clock.t, "pm25": float(i)})
        clock.t += 1
    assert outbox.pending() == 3                      # t = 0, 5, 10
    drain(up)
    assert fb.stored["telemetry/node1"] == {"ts": clock.t - 2, "pm25": 10.0}


def test_nothing_lost_while_offline_then_backoff_resets(outbox):
    fb = FakeFirebase(failures=[None, 503, None, 401])
    up, clock = make(outbox, fb)
    for i in range(6):
        up.on_message(TOPIC_GW_ALERT, {"ts": clock.t + i, "type": "pm_high", "severity": "warn", "msg": str(i)})
    delays = drain(up)
    assert outbox.pending() == 0
    assert sorted(a["msg"] for a in fb.under("alerts/").values()) == ["0", "1", "2", "3", "4", "5"]
    assert sorted(fb.under("alerts/")) == list(fb.under("alerts/"))  # keys sort in time order
    for delay, base in zip(delays, [1, 2, 4, 8]):     # doubling backoff with +/- 20 % jitter
        assert 0.8 * base <= delay <= 1.2 * base
    assert delays[4] == 0
    assert up._backoff == 1


def test_bad_row_goes_to_dead_table_and_queue_keeps_moving(outbox):
    fb = FakeFirebase(failures=[400, 400, 400])
    up, clock = make(outbox, fb)
    for i in range(4):
        up.on_message(TOPIC_GW_ALERT, {"ts": clock.t + i, "type": "x%d" % i})
    drain(up)
    assert outbox.pending() == 0
    assert up.stats["dead"] == 1                      # 4 rows -> 2 -> 1 rejected, the rest went through
    assert up.stats["sent"] == 3


def test_cause_goes_to_latest_and_history_without_snapshot(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    up.on_message(TOPIC_GW_CAUSE, {"ts": 1794383760, "cause": "cooking", "confidence": 0.74, "source": "rules",
                                   "advice_th": "เปิดพัดลมดูดควัน", "snapshot_path": "/var/lib/x.jpg"})
    drain(up)
    record = fb.stored["cause/latest"]
    assert record["id"] == "c1794383760" and "snapshot_path" not in record
    assert fb.stored["cause/history/c1794383760"] == record


def test_state_gets_outbox_counts_and_new_alerts_once(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    alert = {"type": "pm_high", "severity": "warn", "msg": "PM2.5 68", "since": 1794383160}
    for i in range(3):                                # the same alert stays active for 3 state messages
        up.on_message(TOPIC_GW_STATE, {"ts": clock.t, "mode": "idle", "alerts": [alert]})
        clock.t += 5
    up.on_message(TOPIC_GW_STATE, {"ts": clock.t, "mode": "idle", "alerts": []})
    clock.t += 5
    up.on_message(TOPIC_GW_STATE, {"ts": clock.t, "mode": "idle", "alerts": [alert]})  # comes back
    drain(up)
    alerts = list(fb.under("alerts/").values())
    assert len(alerts) == 2
    assert alerts[0] == {"ts": 1794383160, "type": "pm_high", "severity": "warn", "msg": "PM2.5 68"}
    assert "outbox_pending" in fb.stored["state"] and "last_upload_ts" in fb.stored["state"]


def test_history_averages_per_minute_and_flushes(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    start = int(clock.t)
    for i in range(120):                              # 2 minutes of 1 Hz telemetry and env
        up.on_message(TOPIC_NODE1_TELEMETRY, {"ts": clock.t, "pm25": 10.0 if i < 60 else 20.0})
        up.on_message(TOPIC_GW_ENV, {"ts": clock.t, "temp_c": 29.0, "rh": 70})
        up.on_message(TOPIC_NODE2_TELEMETRY, {"ts": clock.t, "voltage": 12.1})  # not a chart field
        clock.t += 1
    clock.t += 1000
    up.flush_history()
    drain(up)
    m1 = fb.under("history/m1/")
    assert m1["history/m1/%010d" % start] == {"pm25": 10.0, "temp_c": 29.0, "rh": 70.0}
    assert m1["history/m1/%010d" % (start + 60)]["pm25"] == 20.0
    assert fb.under("history/m15/") == {"history/m15/%010d" % start: {"pm25": 15.0, "temp_c": 29.0, "rh": 70.0}}


def test_buckets_ignore_late_records():
    b = Buckets(60)
    assert b.add(120, {"pm25": 1}) is None
    key, rec = b.add(185, {"pm25": 2})
    assert (key, rec) == ("0000000120", {"pm25": 1.0})
    assert b.add(130, {"pm25": 99}) is None           # belongs to the closed bucket
    assert b.flush(240) == ("0000000180", {"pm25": 2.0})
    assert b.add(200, {"pm25": 5}) is None            # closed by the flush


def test_old_history_keys_are_deleted(outbox):
    now = 1794383700
    keys = {"history/m1": [str(now - 49 * 3600), str(now - 3600)],
            "history/m15": [str(now - 31 * 86400), str(now - 86400)]}
    fb = FakeFirebase(keys=keys)
    for path, ks in keys.items():
        for k in ks:
            fb.stored["%s/%s" % (path, k)] = {"pm25": 1}
    up, clock = make(outbox, fb)
    assert up.clean_history(now) == 2
    assert sorted(fb.under("history/")) == sorted(["history/m15/%d" % (now - 86400), "history/m1/%d" % (now - 3600)])


def test_nan_becomes_null(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    up.on_message(TOPIC_GW_ENV, {"ts": clock.t, "temp_c": float("nan"), "rh": 50})
    drain(up)
    assert fb.stored["env"]["temp_c"] is None
