from tools.sim_node2 import CHARGE_TIMER_S, Node2Model


def docked_box(**kwargs):
    m = Node2Model(**kwargs)
    m.fixed_batt_temp = 30.0
    m.step(2)
    return m


def charging_box(**kwargs):
    m = docked_box(**kwargs)
    m.apply_cmd({"seq": 1, "charge_allowed": True, "reason": "test"})
    m.step(2)
    assert m.mode == "CHARGING"
    return m


def test_plugged_box_goes_docked_and_unplugged_goes_idle():
    m = docked_box()
    assert m.mode == "DOCKED"
    m.plugged = False
    m.step(2)
    assert m.mode == "IDLE"
    assert m.telemetry()["est_percent"] is None


def test_no_charging_without_permission():
    m = docked_box()
    m.step(60)
    assert m.mode == "DOCKED" and m.current == 0


def test_no_charging_without_battery_temperature():
    m = Node2Model()
    m.step(2)
    m.apply_cmd({"seq": 1, "charge_allowed": True})
    m.step(2)
    assert m.mode == "DOCKED"


def test_charging_stops_when_permission_is_not_refreshed():
    m = charging_box()  # permission sent 2 s ago
    m.step(57)
    assert m.mode == "CHARGING"
    m.step(3)
    assert m.mode == "DOCKED"


def test_charges_to_full_and_current_stays_under_the_limit():
    m = docked_box(soc=0.5)
    m.auto_allow = True
    peak = 0.0
    for _ in range(4 * 3600):
        m.step(1)
        peak = max(peak, m.current)
        if m.mode == "FULL":
            break
    assert m.mode == "FULL"
    assert m.soc > 0.99
    assert peak <= 3.0


def test_hot_battery_latches_fault_until_cool_and_reset():
    m = charging_box()
    m.fixed_batt_temp = 46.0
    m.step(1)
    assert (m.mode, m.fault) == ("FAULT", "over_temp")
    assert "still too hot" in m.reset_fault()
    m.fixed_batt_temp = 35.0
    m.step(1)
    assert m.mode == "FAULT"
    assert m.reset_fault() == "fault reset"
    assert m.mode == "DOCKED"


def test_over_current_faults_immediately():
    m = charging_box()
    m.over_current = True
    m.step(1)
    assert (m.mode, m.fault) == ("FAULT", "over_current")
    m.apply_cmd({"seq": 2, "reset_fault": True})
    assert m.mode == "DOCKED"


def test_charge_timer_fault():
    m = docked_box(soc=0.0, capacity_ah=40.0)
    m.auto_allow = True
    m.step(CHARGE_TIMER_S + 10)
    assert (m.mode, m.fault) == ("FAULT", "timer")


def test_field_test_powers_the_unit_without_a_battery():
    m = docked_box(battery=False)
    m.field_test = True
    m.step(2)
    assert m.mode == "FIELD_TEST"
    assert m.current > 1.0
    m.battery = True
    m.step(2)
    assert m.mode == "DOCKED"


def test_duplicate_command_is_applied_once():
    m = docked_box()
    m.apply_cmd({"seq": 5, "charge_allowed": True})
    m.allowed = False
    assert "duplicate" in m.apply_cmd({"seq": 5, "charge_allowed": True})
    assert m.allowed is False


def test_telemetry_has_protocol_fields():
    t = docked_box().telemetry()
    for key in ("docked", "charging", "voltage", "current_a", "temp_c", "est_percent"):
        assert key in t
    assert t["mode"] == "DOCKED"
