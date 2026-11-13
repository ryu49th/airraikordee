import pytest

from cloud.uploader import Outbox, Uploader, build_updates, history_key
from conftest import FakeMqtt

ROUTES = [
    {"topic": "ab/node1/telemetry", "path": "telemetry/node1", "history": True, "every_s": 0, "history_every_s": 5},
    {"topic": "ab/gw/vision", "path": "vision", "history": False, "every_s": 1},
    {"topic": "ab/gw/alert", "path": "alerts", "history": True},
]
CFG = {"uploader": {"routes": ROUTES, "batch_size": 50, "backoff_min_s": 1, "backoff_max_s": 8}}


class FakeFirebase(object):
    """Fails with the given statuses first, then returns 200. Keeps everything it accepted."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.stored = {}
        self.calls = 0

    def patch(self, updates):
        self.calls += 1
        if self.failures:
            status = self.failures.pop(0)
            return status, "error" if status else "connection refused"
        self.stored.update(updates)
        return 200, "{}"


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def outbox(tmp_path):
    box = Outbox(str(tmp_path / "outbox.db"))
    yield box
    box.close()


def make(outbox, firebase):
    clock = Clock()
    up = Uploader(CFG, FakeMqtt(), outbox, firebase, clock=clock)
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
    box.add("env", 1.0, '{"rh":50}', True)
    box.add("env", 2.0, '{"rh":51}', True)
    box.close()
    box = Outbox(path)
    rows = box.fetch(10)
    assert [r[3] for r in rows] == ['{"rh":50}', '{"rh":51}']
    box.delete([rows[0][0]])
    assert box.pending() == 1
    box.close()


def test_build_updates_latest_is_newest_and_history_keys_sort():
    rows = [(1, "telemetry/node1", 10.0, '{"pm25":1}', 1),
            (2, "telemetry/node1", 11.0, '{"pm25":2}', 0),
            (3, "vision", 11.5, '{"person_present":true}', 0)]
    updates = build_updates(rows)
    assert updates["telemetry/node1/latest"] == {"pm25": 2}
    assert updates["telemetry/node1/history/" + history_key(10.0, 1)] == {"pm25": 1}
    assert updates["vision/latest"] == {"person_present": True}
    assert len(updates) == 3
    assert history_key(9.999, 99) < history_key(10.0, 1)


def test_nothing_lost_while_offline_then_backoff_resets(outbox):
    fb = FakeFirebase(failures=[None, 503, None, 401])
    up, clock = make(outbox, fb)
    route = ROUTES[0]
    for i in range(20):
        clock.t += 1
        up.ingest(route, {"node_id": "node1", "seq": i, "ts": clock.t, "pm25": i})
    delays = drain(up)
    assert outbox.pending() == 0
    assert fb.stored["telemetry/node1/latest"]["pm25"] == 19
    history = [k for k in fb.stored if k.startswith("telemetry/node1/history/")]
    assert len(history) == 4                          # one per 5 s of the 20 s
    for delay, base in zip(delays, [1, 2, 4, 8]):     # doubling backoff with +/- 20 % jitter
        assert 0.8 * base <= delay <= 1.2 * base
    assert delays[4] == 0
    assert up._backoff == 1
    assert fb.stored["health/uploader"]["sent_total"] == 0  # stats are written before the batch is counted


def test_bad_row_goes_to_dead_table_and_queue_keeps_moving(outbox):
    fb = FakeFirebase(failures=[400, 400, 400])
    up, clock = make(outbox, fb)
    for i in range(4):
        up.ingest(ROUTES[2], {"ts": i, "type": "x%d" % i})
    drain(up)
    assert outbox.pending() == 0
    assert up.stats["dead"] == 1                      # 4 rows -> 2 -> 1 rejected, the rest went through
    assert up.stats["sent"] == 3


def test_every_s_throttles_vision(outbox):
    up, clock = make(outbox, FakeFirebase())
    kept = []
    for i in range(10):                               # 5 Hz for 2 s
        clock.t += 0.2
        kept.append(up.ingest(ROUTES[1], {"ts": clock.t, "person_present": True}))
    assert sum(kept) == 2


def test_missing_ts_and_nan_are_handled(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    up.ingest(ROUTES[2], {"type": "x", "value": float("nan")})
    drain(up)
    assert fb.stored["alerts/latest"] == {"type": "x", "value": None}
