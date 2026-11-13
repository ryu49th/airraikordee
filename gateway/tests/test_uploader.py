import json

import pytest

from cloud.uploader import DeviceAuth, FirebaseRest, MinuteHistory, Outbox, Uploader, build_updates
from common.protocol import (TOPIC_GW_ALERT, TOPIC_GW_CAUSE, TOPIC_GW_ENV, TOPIC_GW_HEALTH, TOPIC_GW_STATE,
                             TOPIC_GW_VISION, TOPIC_NODE1_TELEMETRY, TOPIC_NODE2_TELEMETRY)
from conftest import FakeMqtt

CFG = {"uploader": {"batch_size": 50, "backoff_min_s": 1, "backoff_max_s": 8}}


class FakeFirebase(object):
    """Fails with the given statuses first, then returns 200. Applies updates to a flat dict of paths."""

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

    def under(self, prefix):
        return dict((k, v) for k, v in self.stored.items() if k.startswith(prefix))


class Clock(object):
    def __init__(self):
        self.t = 1794384000.0  # start of a minute

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
    up._last_health = clock.t  # tests opt in to health writes
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
    box.add("env/latest", 1.0, '{"rh":50}')
    box.add("env/latest", 2.0, '{"rh":51}')
    box.close()
    box = Outbox(path)
    rows = box.fetch(10)
    assert [r[3] for r in rows] == ['{"rh":50}', '{"rh":51}']
    box.delete([rows[0][0]])
    assert box.pending() == 1
    box.close()


def test_build_updates_later_row_wins():
    rows = [(1, "env/latest", 10.0, '{"rh":1}'), (2, "env/latest", 11.0, '{"rh":2}'),
            (3, "state", 11.0, '{"mode":"idle"}')]
    assert build_updates(rows) == {"env/latest": {"rh": 2}, "state": {"mode": "idle"}}


def test_node1_latest_merges_state_and_vision_and_keeps_contract_fields(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    up.on_message(TOPIC_GW_STATE, {"ts": clock.t, "mode": "track", "fan_pct": 70, "pm25_corrected": 36.5,
                                   "target": 35, "alerts": []})
    up.on_message(TOPIC_GW_VISION, {"ts": clock.t, "person_present": True, "persons": [{"conf": 0.9}]})
    clock.t += 1
    up.on_message(TOPIC_NODE1_TELEMETRY, {"node_id": "node1", "seq": 9, "ts": clock.t, "pm25": 41, "pm10": 58,
                                          "fan_rpm": 1820, "pan_deg": -35, "tilt_deg": 12, "button": False,
                                          "batt_temp_c": 29.8})
    drain(up)
    assert fb.stored["telemetry/node1/latest"] == {
        "ts": clock.t, "pm25": 41, "pm25_corrected": 36.5, "pm10": 58, "fan_pct": 70, "fan_rpm": 1820,
        "pan_deg": -35, "tilt_deg": 12, "person": True, "mode": "track"}
    assert fb.stored["state"] == {"mode": "track", "fan_pct": 70, "target": 35, "alerts": []}


def test_node2_maps_docked_to_plugged_and_takes_battery_temp_from_node1(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    up.on_message(TOPIC_NODE1_TELEMETRY, {"ts": clock.t, "pm25": 10, "batt_temp_c": 29.8})
    clock.t += 1
    up.on_message(TOPIC_NODE2_TELEMETRY, {"node_id": "node2", "seq": 1, "ts": clock.t, "mode": "CHARGING",
                                          "docked": True, "charging": True, "voltage": 12.1, "current_a": 1.8,
                                          "temp_c": 31.2, "est_percent": 64})
    drain(up)
    assert fb.stored["telemetry/node2/latest"] == {
        "ts": clock.t, "mode": "CHARGING", "voltage": 12.1, "current_a": 1.8, "temp_c": 31.2, "batt_temp_c": 29.8,
        "plugged": True, "est_percent": 64}


def test_latest_paths_are_throttled_to_1s(outbox):
    up, clock = make(outbox, FakeFirebase())
    for i in range(10):                               # 5 Hz for 2 s
        up.on_message(TOPIC_GW_ENV, {"ts": clock.t, "temp_c": 30, "rh": 60, "pressure_hpa": 1006})
        clock.t += 0.2
    assert sum(1 for r in outbox.fetch(100) if r[1] == "env/latest") == 2


def test_nothing_lost_while_offline_then_backoff_resets(outbox):
    fb = FakeFirebase(failures=[None, 503, None, 401])
    up, clock = make(outbox, fb)
    for i in range(6):
        up.on_message(TOPIC_GW_ALERT, {"ts": clock.t + i, "type": "pm_high", "severity": "warn", "msg": str(i)})
    delays = drain(up)
    assert outbox.pending() == 0
    alerts = fb.under("alerts/")
    assert sorted(a["text"] for a in alerts.values()) == ["0", "1", "2", "3", "4", "5"]
    assert sorted(alerts) == list(alerts)             # keys sort in time order
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


def test_alert_record_matches_contract(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    up.on_message(TOPIC_GW_ALERT, {"ts": 1794383760.5, "source": "uart_bridge", "type": "uart_fail",
                                   "severity": "warn", "msg": "Node 1 ไม่ตอบคำสั่ง fan"})
    drain(up)
    (record,) = fb.under("alerts/").values()
    assert record == {"ts": 1794383760, "level": "warn", "title": "Node 1 ไม่ตอบคำสั่ง",
                      "text": "Node 1 ไม่ตอบคำสั่ง fan"}


def test_cause_latest_drops_snapshot_and_extra_fields(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    up.on_message(TOPIC_GW_CAUSE, {"ts": 1794383760, "cause": "cooking", "confidence": 0.74, "source": "rules",
                                   "advice_th": "ปิดประตูครัว", "snapshot_path": "/var/lib/x.jpg", "id": "c1"})
    drain(up)
    assert fb.stored["cause/latest"] == {"ts": 1794383760, "cause": "cooking", "confidence": 0.74,
                                         "source": "rules", "advice_th": "ปิดประตูครัว"}


def test_state_alert_strings_create_one_alert_each_time_they_start(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    for alerts in (["pm25_high"], ["pm25_high"], [], ["pm25_high"]):
        up.on_message(TOPIC_GW_STATE, {"ts": clock.t, "mode": "idle", "alerts": alerts})
        clock.t += 5
    drain(up)
    alerts = list(fb.under("alerts/").values())
    assert len(alerts) == 2
    assert alerts[0]["title"] == "PM2.5 สูง" and alerts[0]["level"] == "warn"
    assert fb.stored["state"]["alerts"] == ["pm25_high"]


def test_history_is_one_row_per_minute_keyed_by_minute_start(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    start = int(clock.t)
    for i in range(120):                              # 2 minutes of 1 Hz data
        up.on_message(TOPIC_NODE1_TELEMETRY, {"ts": clock.t, "pm25": 10.0 if i < 60 else 20.0})
        up.on_message(TOPIC_GW_ENV, {"ts": clock.t, "temp_c": 29.0, "rh": 70})
        up.on_message(TOPIC_GW_STATE, {"ts": clock.t, "mode": "idle", "fan_pct": 40})
        clock.t += 1
    clock.t += 30
    up._last_health = clock.t
    up.tick()
    drain(up)
    history = fb.under("history/node1/")
    assert sorted(history) == ["history/node1/%d" % start, "history/node1/%d" % (start + 60)]
    assert history["history/node1/%d" % start] == {"ts": start + 59, "pm25": 10.0, "temp_c": 29.0, "rh": 70.0,
                                                   "fan_pct": 40.0}
    assert history["history/node1/%d" % (start + 60)]["pm25"] == 20.0


def test_minute_history_ignores_late_records():
    h = MinuteHistory()
    assert h.add(120, {"pm25": 1}) is None
    assert h.add(185, {"pm25": 2}) == (120, {"pm25": 1.0, "ts": 120})
    assert h.add(130, {"pm25": 99}) is None           # belongs to the written minute
    assert h.flush(240) == (180, {"pm25": 2.0, "ts": 185})
    assert h.add(200, {"pm25": 5}) is None


def test_health_every_30s_with_uploader_counts_and_node_online(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    up.on_message(TOPIC_GW_HEALTH, {"ts": clock.t, "cpu_temp_c": 52, "filter_hours": 312, "ram_pct": 61,
                                    "nodes": {"node2": {"online": False}}})
    up.on_message(TOPIC_NODE1_TELEMETRY, {"ts": clock.t, "pm25": 5})
    up.tick()
    assert "health" not in fb.stored and not [r for r in outbox.fetch(100) if r[1] == "health"]
    clock.t += 30
    up.on_message(TOPIC_NODE1_TELEMETRY, {"ts": clock.t, "pm25": 5})
    up.tick()
    drain(up)
    health = fb.stored["health"]
    assert health["node1_online"] is True and health["node2_online"] is False
    assert health["cpu_temp_c"] == 52 and health["filter_hours"] == 312 and "ram_pct" not in health
    assert "pending_rows" in health and "last_upload_ts" in health


def test_nan_becomes_null(outbox):
    fb = FakeFirebase()
    up, clock = make(outbox, fb)
    up.on_message(TOPIC_GW_ENV, {"ts": clock.t, "temp_c": float("nan"), "rh": 50})
    drain(up)
    assert fb.stored["env/latest"]["temp_c"] is None


# --- auth and REST against a fake HTTP session -------------------------------------------------------

class Resp(object):
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession(object):
    def __init__(self):
        self.calls = []
        self.valid_tokens = set()
        self.n = 0

    def post(self, url, params=None, json=None, data=None, timeout=None):
        self.calls.append(("POST", url.split("/")[-1]))
        if "signInWithPassword" in url:
            if json["password"] != "right":
                return Resp(400, {"error": {"message": "INVALID_LOGIN_CREDENTIALS"}})
            return self._token("idToken", "refreshToken", "expiresIn")
        return self._token("id_token", "refresh_token", "expires_in")

    def _token(self, id_key, refresh_key, exp_key):
        self.n += 1
        token = "tok%d" % self.n
        self.valid_tokens.add(token)
        return Resp(200, {id_key: token, refresh_key: "r%d" % self.n, exp_key: "3600"})

    def patch(self, url, params=None, data=None, timeout=None, headers=None):
        self.calls.append(("PATCH", url))
        if params["auth"] not in self.valid_tokens:
            return Resp(401, {"error": "Auth token is expired"})
        return Resp(200, {})


def test_device_signs_in_writes_under_devices_and_refreshes_after_401():
    session, clock = FakeSession(), Clock()
    auth = DeviceAuth("key", "device-airraikordee-01@devices.airraikordee.example", "right", session, clock=clock)
    fb = FirebaseRest("https://db.example/", "airraikordee-01", auth, session)
    assert fb.patch({"env/latest": {"rh": 1}})[0] == 200
    assert session.calls[-1] == ("PATCH", "https://db.example/devices/airraikordee-01.json")
    session.valid_tokens.clear()                      # token revoked/expired early
    assert fb.patch({"env/latest": {"rh": 2}})[0] == 200
    assert [c[1] for c in session.calls if c[0] == "POST"] == ["accounts:signInWithPassword", "token"]
    clock.t += 3600 - 200                             # within 5 min of expiry: refresh before writing
    fb.patch({"env/latest": {"rh": 3}})
    assert [c[1] for c in session.calls if c[0] == "POST"][-1] == "token"


def test_wrong_device_password_is_reported_not_raised():
    session = FakeSession()
    auth = DeviceAuth("key", "device-x@devices.airraikordee.example", "wrong", session)
    fb = FirebaseRest("https://db.example", "x", auth, session)
    assert fb.patch({"env/latest": {}}) == (None, "device sign-in failed")
    assert not [c for c in session.calls if c[0] == "PATCH"]
