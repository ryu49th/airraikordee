import random

from brain.decision import Brain, correct_pm, fan_from_table, in_window, minutes_of_day
from common.protocol import (TOPIC_GW_CONFIG, TOPIC_GW_ENV, TOPIC_GW_HEALTH, TOPIC_GW_STATE, TOPIC_GW_VISION,
                             TOPIC_NODE1_CMD, TOPIC_NODE1_TELEMETRY, TOPIC_NODE2_CMD, TOPIC_NODE2_TELEMETRY)
from conftest import FakeMqtt

TABLE = [[15, 25], [35, 45], [55, 70], [100000, 100]]


class Clock(object):
    def __init__(self):
        self.t = 1794384000.0
        self.minute = 12 * 60  # noon, local time

    def __call__(self):
        return self.t


def make(cfg=None):
    clock = Clock()
    mqtt = FakeMqtt()
    b = Brain({"decision": cfg or {}}, mqtt, clock=clock, local_minutes=lambda t: clock.minute)
    return b, mqtt, clock


def cmds(mqtt, name):
    return [m for m in mqtt.on(TOPIC_NODE1_CMD) if m.get("cmd") == name]


def charge(mqtt):
    return [m["charge_allowed"] for m in mqtt.on(TOPIC_NODE2_CMD)]


def node1(b, pm25=10, **kw):
    msg = {"pm25": pm25, "pan_deg": 0.0, "tilt_deg": 0.0, "batt_temp_c": 30.0, "button": False}
    msg.update(kw)
    b.on_node1(TOPIC_NODE1_TELEMETRY, msg)


def person(b, pan_err=0.0, tilt_err=0.0, surveying=False):
    b.on_vision(TOPIC_GW_VISION, {"persons": [{"pan_err_deg": pan_err, "tilt_err_deg": tilt_err}],
                                  "person_present": True, "surveying": surveying})


def nobody(b, surveying=False):
    b.on_vision(TOPIC_GW_VISION, {"persons": [], "person_present": False, "surveying": surveying})


def alerts(state):
    return [a["type"] for a in state["alerts"]]


# --- pure helpers ----------------------------------------------------------------------------------------

def test_humidity_correction_and_fan_table():
    assert correct_pm(40, None, 0.5) == 40 and correct_pm(40, 55, 0.5) == 40
    assert correct_pm(48, 100, 0.5) == 32.0          # rh 100: divide by 1.5
    assert [fan_from_table(pm, TABLE) for pm in (5, 15, 15.1, 35, 50, 56, 900)] == [25, 25, 45, 45, 70, 100, 100]


def test_night_window_crosses_midnight():
    s, e = minutes_of_day("22:00"), minutes_of_day("06:00")
    assert in_window(23 * 60, s, e) and in_window(3 * 60, s, e) and not in_window(12 * 60, s, e)
    assert not in_window(6 * 60, s, e) and in_window(13 * 60, minutes_of_day("13:00"), minutes_of_day("14:00"))


# --- fan (AIR-13) ----------------------------------------------------------------------------------------

def test_fan_follows_corrected_pm_and_resends():
    b, mqtt, clock = make()
    node1(b, pm25=10)
    b.step()
    node1(b, pm25=48)
    b.on_env(TOPIC_GW_ENV, {"rh": 100})              # 48 / 1.5 = 32 -> 45 %
    b.step()
    b.step()
    assert [c["pct"] for c in cmds(mqtt, "fan")] == [25, 45]
    assert mqtt.on(TOPIC_GW_STATE)[-1]["pm25_corrected"] == 32.0
    clock.t += 61
    node1(b, pm25=48)
    b.step()
    assert [c["pct"] for c in cmds(mqtt, "fan")] == [25, 45, 45]  # resent: Node 1 may have reset


def test_night_caps_the_fan_and_manual_mode_sends_nothing():
    b, mqtt, clock = make()
    clock.minute = 23 * 60
    node1(b, pm25=80)
    state = b.step()
    assert state["mode"] == "night" and cmds(mqtt, "fan")[-1]["pct"] == 35

    b, mqtt, clock = make()
    b.on_config(TOPIC_GW_CONFIG, {"fan_mode": "manual", "manual_fan_pct": 60, "night_start": "01:00",
                                  "night_end": "02:00"})
    node1(b, pm25=80)
    state = b.step()
    assert cmds(mqtt, "fan") == [] and state["fan_pct"] == 60 and state["mode"] != "night"


def test_stop_button_blocks_commands_and_raises_an_alert():
    b, mqtt, _ = make()
    node1(b, pm25=80, button=True)
    person(b, pan_err=30)
    state = b.step()
    assert mqtt.on(TOPIC_NODE1_CMD) == [] and alerts(state) == ["stop_pressed"]


def test_stale_pm_leaves_the_fan_alone():
    b, mqtt, clock = make()
    node1(b, pm25=80)
    b.step()
    clock.t += 31
    state = b.step()
    assert len(cmds(mqtt, "fan")) == 1 and state["pm25_corrected"] is None


# --- modes and aiming (AIR-14) ----------------------------------------------------------------------------

def test_aim_person_with_deadband_and_max_step():
    b, mqtt, _ = make()
    node1(b, pan_deg=40.0, tilt_deg=0.0)
    person(b, pan_err=2.0, tilt_err=1.0)             # inside the deadband
    assert b.step()["mode"] == "aim_person" and cmds(mqtt, "aim") == []
    person(b, pan_err=-30.0, tilt_err=5.0)
    b.step()
    aim = cmds(mqtt, "aim")[-1]
    assert (aim["pan_deg"], aim["tilt_deg"]) == (30.0, 4.0)   # max 10 deg step, gain 0.8 on tilt


def test_survey_mode_does_not_aim():
    b, mqtt, _ = make()
    node1(b)
    person(b, pan_err=30, surveying=True)
    assert b.step()["mode"] == "survey" and cmds(mqtt, "aim") == []


def test_idle_after_ten_minutes_centres_the_head_once():
    b, mqtt, clock = make()
    node1(b, pan_deg=90.0)
    person(b, pan_err=0.5)
    b.step()
    for _ in range(3):
        clock.t += 300
        node1(b, pan_deg=90.0)
        nobody(b)
        state = b.step()
    assert state["mode"] == "idle"
    assert [(a["pan_deg"], a["tilt_deg"]) for a in cmds(mqtt, "aim")] == [(0.0, 0.0)]
    assert state["target"] == {"pan_deg": 0.0, "tilt_deg": 0.0}


# --- charging (AIR-15) --------------------------------------------------------------------------------------

def test_charge_allowed_rules():
    b, mqtt, clock = make()
    b.on_node2(TOPIC_NODE2_TELEMETRY, {"docked": True, "mode": "DOCKED"})
    node1(b, batt_temp_c=None)
    person(b)
    b.step()
    assert b.charge_reason == "battery temperature unknown"
    node1(b, batt_temp_c=31.0)
    b.step()
    node1(b, batt_temp_c=46.0)
    state = b.step()
    assert charge(mqtt) == [False, True, False] and "charge_cut_temp" in alerts(state)
    node1(b, batt_temp_c=40.0)
    b.on_node2(TOPIC_NODE2_TELEMETRY, {"docked": False})
    state = b.step()
    assert charge(mqtt)[-1] is False and b.charge_reason == "not docked"
    assert "charge_cut_temp" not in alerts(state)


def test_refresh_every_30_s():
    b, mqtt, clock = make()
    b.on_node2(TOPIC_NODE2_TELEMETRY, {"docked": True})
    node1(b)
    person(b)
    for _ in range(61):
        b.step()
        clock.t += 1
        node1(b)
        b.on_node2(TOPIC_NODE2_TELEMETRY, {"docked": True})
    assert charge(mqtt) == [True, True, True]


def test_confirm_present_counts_as_attended():
    b, mqtt, clock = make()
    b.on_node2(TOPIC_NODE2_TELEMETRY, {"docked": True})
    node1(b)
    b.on_ui_cmd(None, {"cmd": "confirm_present"})
    b.step()
    assert charge(mqtt) == [True] and b.mode == "idle"  # an app confirm does not aim the head


# --- alerts ---------------------------------------------------------------------------------------------------

def test_pm_high_after_ten_minutes_and_hysteresis():
    b, mqtt, clock = make()
    for _ in range(9):
        node1(b, pm25=60)
        assert "pm_high" not in alerts(b.step())
        clock.t += 60
    clock.t += 60
    node1(b, pm25=60)
    state = b.step()
    assert "pm_high" in alerts(state) and state["alerts"][0]["level"] == "warn"
    node1(b, pm25=33)                                 # below 35 but above 90 %: stays on
    assert "pm_high" in alerts(b.step())
    node1(b, pm25=30)
    assert "pm_high" not in alerts(b.step())


def test_pm_alert_follows_the_dashboard_setting():
    b, mqtt, clock = make()
    b.on_config(TOPIC_GW_CONFIG, {"pm_alert": 80})
    node1(b, pm25=60)
    b.step()
    clock.t += 700
    node1(b, pm25=60)
    assert "pm_high" not in alerts(b.step())


def test_charger_and_filter_alerts():
    b, mqtt, clock = make()
    node1(b)
    b.on_node2(TOPIC_NODE2_TELEMETRY, {"docked": True, "mode": "FAULT", "fault": "timer", "est_percent": 20})
    b.on_health(TOPIC_GW_HEALTH, {"fan_rpm_ratio": 0.5})
    assert sorted(alerts(b.step())) == ["battery_low", "charge_fault"]
    clock.t += 601
    node1(b)
    b.on_node2(TOPIC_NODE2_TELEMETRY, {"docked": True, "mode": "FAULT", "fault": "over_temp", "est_percent": 30})
    b.on_health(TOPIC_GW_HEALTH, {"fan_rpm_ratio": 0.5})
    assert sorted(alerts(b.step())) == ["charge_cut_temp", "filter_clogged"]
    b.on_node2(TOPIC_NODE2_TELEMETRY, {"docked": True, "mode": "FULL", "est_percent": 100})
    assert "charge_full" in alerts(b.step())


# --- closed loop with the simulators -----------------------------------------------------------------------

def test_closed_loop_dust_raises_the_fan_and_cleans_the_room():
    """AIR-13 done-when: dust goes up and the fan speeds up by itself."""
    from tools.sim_node1 import Node1Model
    node = Node1Model(random.Random(3))
    b, mqtt, clock = make()
    fans = []
    for second in range(1800):
        if second == 60:
            node.start_dust("cooking")
        node.step(1.0)
        b.on_node1(TOPIC_NODE1_TELEMETRY, node.telemetry())
        b.step()
        for c in mqtt.published[-3:]:
            if c[0] == TOPIC_NODE1_CMD and c[1]["cmd"] == "fan":
                node.apply_cmd(c[1])
        fans.append(node.fan_pct)
        clock.t += 1
    assert fans[30] == 25 and max(fans) >= 70 and fans[-1] <= 45


def test_closed_loop_charging_stops_when_nobody_is_there():
    """AIR-15 done-when: nobody seen for 15 min and the box stops charging by itself."""
    from tools.sim_node2 import Node2Model
    box = Node2Model(soc=0.4, rng=random.Random(1))
    box.fixed_batt_temp = 30.0
    b, mqtt, clock = make()
    charging = []
    sent = 0
    for second in range(1300):
        node1(b, batt_temp_c=30.0)
        if second < 60:
            person(b)
        else:
            nobody(b)
        b.on_node2(TOPIC_NODE2_TELEMETRY, dict(box.telemetry(), docked=box.plugged))
        b.step()
        for msg in mqtt.on(TOPIC_NODE2_CMD)[sent:]:
            box.apply_cmd(msg)
        sent = len(mqtt.on(TOPIC_NODE2_CMD))
        box.set_batt_temp(30.0)
        box.step(1.0)
        charging.append(box.mode == "CHARGING")
        clock.t += 1
    assert any(charging[:900]) and charging[900] is True
    assert not any(charging[965:])  # 15 min after the person left (+ one refresh), charging is off
    assert b.charge_reason.startswith("nobody seen")
