import json

from cloud.config_listener import ConfigListener, validate
from cloud.uploader import FirebaseRest
from common.protocol import TOPIC_GW_CONFIG, TOPIC_GW_UI_CMD, TOPIC_NODE1_CMD
from conftest import FakeMqtt

CFG = {"decision": {"pm_alert": 35, "night_start": "22:00", "night_end": "06:00"},
       "config_listener": {"poll_s": 5, "manual_resend_s": 60, "backoff_max_s": 60}}
DEFAULTS = {"pm_alert": 35, "night_start": "22:00", "night_end": "06:00", "fan_mode": "auto",
            "manual_fan_pct": None, "filter_reset_ts": None}


class FakeFirebase(object):
    """GET devices/<id>/config: returns `value`, or fails with `status` while it is set."""

    def __init__(self, value=None):
        self.value = value
        self.ui_cmd = None
        self.status = None
        self.patch_status = 200
        self.paths = []
        self.patches = []

    def get(self, path):
        self.paths.append(path)
        if self.status is not None:
            return self.status, "error"
        return 200, (self.ui_cmd if path == "ui_cmd" else self.value)

    def patch(self, updates):
        self.patches.append(updates)
        if self.patch_status == 200 and self.ui_cmd:
            for k in updates:
                self.ui_cmd.pop(k.split("/", 1)[1], None)
        return self.patch_status, "{}"


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(value=None, cache=None):
    clock = Clock()
    fb = FakeFirebase(value)
    listener = ConfigListener(CFG, FakeMqtt(), fb, cache, clock=clock)
    return listener, fb, clock


def test_validate_keeps_good_fields_and_falls_back_on_bad_ones():
    s, bad = validate({"pm_alert": 50, "night_start": "23:00", "night_end": "7:00", "fan_mode": "turbo",
                       "manual_fan_pct": 150, "filter_reset_ts": 1794000000}, DEFAULTS)
    assert s["pm_alert"] == 50 and s["night_start"] == "23:00" and s["filter_reset_ts"] == 1794000000
    assert s["night_end"] == "06:00" and s["fan_mode"] == "auto" and s["manual_fan_pct"] is None
    assert sorted(bad) == ["fan_mode", "manual_fan_pct", "night_end"]


def test_validate_rejects_booleans_strings_and_nulls_as_numbers():
    s, bad = validate({"pm_alert": True, "manual_fan_pct": "70", "night_start": None}, DEFAULTS)
    assert s["pm_alert"] == 35 and s["night_start"] == "22:00"
    assert sorted(bad) == ["manual_fan_pct", "pm_alert"]


def test_manual_without_a_value_means_auto():
    s, _ = validate({"fan_mode": "manual", "manual_fan_pct": None}, DEFAULTS)
    assert s["fan_mode"] == "auto"


def test_never_saved_config_uses_defaults_and_publishes_once():
    listener, fb, _ = make(None)
    assert listener.step() == 5
    assert fb.paths == ["config", "ui_cmd"]
    listener.step()
    msgs = listener.mqtt.on(TOPIC_GW_CONFIG)
    assert len(msgs) == 1
    assert msgs[0]["pm_alert"] == 35 and msgs[0]["source"] == "firebase"


def test_change_on_the_web_is_published():
    listener, fb, _ = make({"pm_alert": 35})
    listener.step()
    fb.value = {"pm_alert": 50, "night_start": "21:30", "night_end": "06:00"}
    listener.step()
    msgs = listener.mqtt.on(TOPIC_GW_CONFIG)
    assert [m["pm_alert"] for m in msgs] == [35, 50]
    assert msgs[-1]["night_start"] == "21:30"


def test_manual_fan_sends_node1_command_on_change_and_resends():
    listener, fb, clock = make({"fan_mode": "manual", "manual_fan_pct": 70})
    listener.step()
    listener.step()
    cmds = listener.mqtt.on(TOPIC_NODE1_CMD)
    assert len(cmds) == 1 and cmds[0]["cmd"] == "fan" and cmds[0]["pct"] == 70
    fb.value = {"fan_mode": "manual", "manual_fan_pct": 40}
    listener.step()
    clock.t += 61
    listener.step()
    pcts = [c["pct"] for c in listener.mqtt.on(TOPIC_NODE1_CMD)]
    assert pcts == [70, 40, 40]
    seqs = [c["seq"] for c in listener.mqtt.on(TOPIC_NODE1_CMD)]
    assert seqs == sorted(set(seqs))


def test_auto_mode_sends_no_fan_command():
    listener, _, _ = make({"fan_mode": "auto", "manual_fan_pct": 70})
    listener.step()
    assert listener.mqtt.on(TOPIC_NODE1_CMD) == []


def test_firebase_down_keeps_last_settings_and_backs_off():
    listener, fb, clock = make({"pm_alert": 60, "fan_mode": "manual", "manual_fan_pct": 30})
    listener.step()
    fb.status = 503
    delays = [listener.step() for _ in range(6)]
    assert delays == [5, 10, 20, 40, 60, 60]
    assert listener.current["pm_alert"] == 60
    assert len(listener.mqtt.on(TOPIC_GW_CONFIG)) == 1
    clock.t += 61
    listener.step()
    assert [c["pct"] for c in listener.mqtt.on(TOPIC_NODE1_CMD)] == [30, 30]  # fan still held while offline
    fb.status = None
    assert listener.step() == 5


def test_offline_from_boot_uses_defaults():
    listener, fb, _ = make()
    fb.status = None
    fb.value = None
    fb.status = 401
    listener.step()
    assert listener.current == DEFAULTS
    assert listener.mqtt.on(TOPIC_GW_CONFIG)[0]["source"] == "defaults"


def test_cache_survives_a_restart_without_internet(tmp_path):
    cache = str(tmp_path / "config_cache.json")
    first, _, _ = make({"pm_alert": 80, "fan_mode": "manual", "manual_fan_pct": 55}, cache)
    first.step()
    assert json.load(open(cache))["pm_alert"] == 80

    second, fb, _ = make(None, cache)
    fb.status = 503
    second.load_cache()
    second.step()
    assert second.current["pm_alert"] == 80 and second.source == "cache"
    assert second.mqtt.on(TOPIC_GW_CONFIG)[0]["source"] == "cache"
    assert second.mqtt.on(TOPIC_NODE1_CMD)[0]["pct"] == 55


def test_firebase_rest_get_builds_the_device_path_and_retries_after_401():
    class Resp(object):
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    class Session(object):
        def __init__(self):
            self.calls = []
            self.replies = [Resp(401, {"error": "expired"}), Resp(200, {"pm_alert": 40})]

        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            return self.replies.pop(0)

    class Auth(object):
        def __init__(self):
            self.n = 0

        def token(self):
            self.n += 1
            return "t%d" % self.n

        def invalidate(self):
            pass

    session = Session()
    rest = FirebaseRest("https://db.example/", "airraikordee-01", Auth(), session)
    assert rest.get("config") == (200, {"pm_alert": 40})
    assert session.calls[0][0] == "https://db.example/devices/airraikordee-01/config.json"
    assert [c[1]["auth"] for c in session.calls] == ["t1", "t2"]


def test_sim_node1_follows_the_manual_fan_setting():
    """AIR-97 done-when: change Settings on the web, the (simulated) unit follows within seconds."""
    import random

    from tools.sim_node1 import FAN_MAX_RPM, Node1Model

    node = Node1Model(random.Random(1))
    listener, fb, _ = make({"fan_mode": "manual", "manual_fan_pct": 100})
    listener.step()
    for cmd in listener.mqtt.on(TOPIC_NODE1_CMD):
        node.apply_cmd(cmd)
    node.step(10)
    assert node.fan_pct == 100 and node.fan_rpm > 0.9 * FAN_MAX_RPM

    fb.value = {"fan_mode": "manual", "manual_fan_pct": 20}
    listener.step()
    node.apply_cmd(listener.mqtt.on(TOPIC_NODE1_CMD)[-1])
    node.step(10)
    assert node.fan_pct == 20 and node.fan_rpm < 0.3 * FAN_MAX_RPM


def test_ui_cmd_is_published_once_then_deleted():
    import time as _time
    listener, fb, _ = make({"pm_alert": 35})
    now = int(_time.time())
    fb.ui_cmd = {"k1": {"ts": now, "by": "uid1", "cmd": "confirm_present"},
                 "k2": {"ts": now, "by": "uid1", "cmd": "test_notify", "channel": "discord"},
                 "k3": {"ts": now, "by": "uid1", "cmd": "format_disk"},
                 "k4": {"ts": now - 3600, "by": "uid1", "cmd": "aim_person"}}
    fb.patch_status = 500                       # delete fails: next poll must not publish them again
    listener.step()
    listener.step()
    msgs = listener.mqtt.on(TOPIC_GW_UI_CMD)
    assert [m["cmd"] for m in msgs] == ["confirm_present", "test_notify"]
    assert msgs[1]["channel"] == "discord" and msgs[0]["source"] == "app" and "by" not in msgs[0]
    fb.patch_status = 200
    listener.step()
    assert fb.ui_cmd == {} and len(listener.mqtt.on(TOPIC_GW_UI_CMD)) == 2
    assert set(fb.patches[-1]) == {"ui_cmd/k1", "ui_cmd/k2", "ui_cmd/k3", "ui_cmd/k4"}
    assert all(v is None for v in fb.patches[-1].values())
