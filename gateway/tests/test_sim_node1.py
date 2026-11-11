import random

from tools.sim_node1 import FAN_MAX_RPM, PAN_LIMIT, PAN_SPEED, Node1Model


def model():
    return Node1Model(random.Random(1))


def test_fan_command_changes_rpm():
    m = model()
    m.apply_cmd({"seq": 1, "cmd": "fan", "pct": 100})
    m.step(10)
    assert m.fan_rpm > 0.95 * FAN_MAX_RPM


def test_pan_moves_at_limited_speed_and_is_clamped():
    m = model()
    m.apply_cmd({"seq": 1, "cmd": "aim", "pan_deg": 300, "tilt_deg": -90})
    assert m.pan_target == PAN_LIMIT
    assert m.tilt_target == -30
    m.step(1)
    assert abs(m.pan - PAN_SPEED) < 1e-6
    m.step(30)
    assert m.pan == PAN_LIMIT
    assert m.tilt == -30


def test_duplicate_seq_is_applied_once():
    m = model()
    cmd = {"seq": 7, "cmd": "fan", "pct": 80}
    m.apply_cmd(cmd)
    m.fan_pct = 10
    note = m.apply_cmd(cmd)
    assert m.fan_pct == 10
    assert "duplicate" in note


def test_stop_button_blocks_motion_but_not_stop():
    m = model()
    m.press_stop(True)
    assert m.fan_pct == 0
    m.apply_cmd({"seq": 1, "cmd": "aim", "pan_deg": 90})
    m.apply_cmd({"seq": 2, "cmd": "fan", "pct": 100})
    m.step(10)
    assert m.pan == 0 and m.fan_pct == 0
    assert "stopped" in m.apply_cmd({"seq": 3, "cmd": "stop"})


def test_fan_cleans_the_air():
    slow, fast = model(), model()
    for m, pct in ((slow, 0), (fast, 100)):
        m.apply_cmd({"seq": 1, "cmd": "fan", "pct": pct})
        m.start_dust("cooking")
        m.step(600)
    assert fast.pm < slow.pm / 2


def test_clogged_filter_lowers_rpm():
    clean, clogged = model(), model()
    clogged.clog = 0.3
    for m in (clean, clogged):
        m.apply_cmd({"seq": 1, "cmd": "fan", "pct": 60})
        m.step(10)
    assert clogged.fan_rpm < 0.75 * clean.fan_rpm


def test_telemetry_has_protocol_fields():
    t = model().telemetry()
    for key in ("pm1", "pm25", "pm10", "fan_rpm", "pan_deg", "tilt_deg", "button"):
        assert key in t
    assert t["pm1"] <= t["pm25"] + 3 and t["pm10"] >= t["pm25"]
