import json

from common.protocol import (TOPIC_GW_CONFIG, TOPIC_GW_HEALTH, TOPIC_GW_STATE, TOPIC_NODE1_TELEMETRY,
                             TOPIC_NODE2_TELEMETRY)
from conftest import FakeMqtt
from health.health import Health, SystemProbe

CFG = {"health": {"node_offline_s": 30, "publish_every_s": 10, "disk_warn_pct": 85, "baseline_hours": 1.0}}


class Clock(object):
    def __init__(self):
        self.t = 1794384000.0

    def __call__(self):
        return self.t


class FakeProbe(object):
    def __init__(self, disk=40):
        self.disk = disk

    def cpu_temp_c(self):
        return 52.0

    def ram_pct(self):
        return 61

    def disk_pct(self):
        return self.disk


def make(state_path=None, probe=None):
    clock = Clock()
    mqtt = FakeMqtt()
    h = Health(CFG, mqtt, probe or FakeProbe(), state_path, clock=clock)
    for topic, handler in ((TOPIC_NODE1_TELEMETRY, h.on_node1), (TOPIC_NODE2_TELEMETRY, h.on_node2),
                           (TOPIC_GW_STATE, h.on_state), (TOPIC_GW_CONFIG, h.on_config)):
        mqtt.subscribe(topic, handler)
    return h, mqtt, clock


def run(h, clock, seconds, every=None):
    """Advance time one second at a time, calling step(); `every` = callback per second (e.g. a node sending)."""
    for _ in range(int(seconds)):
        clock.t += 1
        if every:
            every()
        h.step()


def node1(mqtt, rpm=1000, pct=60, pm25=12):
    mqtt.deliver(TOPIC_NODE1_TELEMETRY, {"node_id": "node1", "seq": 1, "ts": 0, "pm25": pm25,
                                         "fan_rpm": rpm, "fan_pct": pct})


def test_first_step_publishes_a_full_heartbeat():
    h, mqtt, _ = make()
    msg = h.step()
    assert mqtt.on(TOPIC_GW_HEALTH) == [msg]
    for key in ("ts", "node1_online", "node1_last_seen", "node2_online", "node2_last_seen", "cpu_temp_c",
                "ram_pct", "disk_pct", "filter_hours", "pms_hours", "fan_rpm_ratio"):
        assert key in msg
    assert msg["node1_online"] is False and msg["node1_last_seen"] is None
    assert (msg["cpu_temp_c"], msg["ram_pct"], msg["disk_pct"]) == (52.0, 61, 40)


def test_heartbeat_every_10_s():
    h, mqtt, clock = make()
    h.step()
    run(h, clock, 30)
    assert len(mqtt.on(TOPIC_GW_HEALTH)) == 4


def test_node2_goes_offline_within_30_s_and_is_published_at_once():
    """AIR-21 done-when: switch Node 2 off and it shows offline within 30 s."""
    h, mqtt, clock = make()
    h.step()
    mqtt.deliver(TOPIC_NODE2_TELEMETRY, {"node_id": "node2"})
    h.step()
    assert mqtt.on(TOPIC_GW_HEALTH)[-1]["node2_online"] is True
    off_at = clock.t
    went_offline = None
    for _ in range(40):
        clock.t += 1
        msg = h.step()
        if msg and msg["node2_online"] is False and went_offline is None:
            went_offline = clock.t
    assert went_offline is not None and went_offline - off_at <= 30
    assert mqtt.on(TOPIC_GW_HEALTH)[-1]["node2_last_seen"] == int(off_at)


def test_node_that_keeps_talking_stays_online():
    h, mqtt, clock = make()
    run(h, clock, 120, every=lambda: mqtt.deliver(TOPIC_NODE2_TELEMETRY, {"node_id": "node2"}))
    assert all(m["node2_online"] for m in mqtt.on(TOPIC_GW_HEALTH)[1:])


def test_filter_and_pms_hours_count_only_while_running():
    h, mqtt, clock = make()
    h.step()
    run(h, clock, 3600, every=lambda: node1(mqtt))
    assert abs(h.filter_hours - 1.0) < 0.01 and abs(h.pms_hours - 1.0) < 0.01
    run(h, clock, 3600, every=lambda: node1(mqtt, rpm=0, pct=0, pm25=None))
    assert abs(h.filter_hours - 1.0) < 0.02 and abs(h.pms_hours - 1.0) < 0.02


def test_time_jump_is_not_counted():
    h, mqtt, clock = make()
    h.step()
    node1(mqtt)
    clock.t += 3 * 3600
    node1(mqtt)
    h.step()
    assert h.filter_hours == 0


def test_rpm_ratio_falls_when_the_filter_clogs():
    h, mqtt, clock = make()
    h.step()
    run(h, clock, 3700, every=lambda: node1(mqtt, rpm=1000, pct=60))   # first hour: learn the baseline
    assert h.fan_rpm_ratio == 1.0
    run(h, clock, 20, every=lambda: node1(mqtt, rpm=700, pct=60))
    assert h.fan_rpm_ratio == 0.7


def test_fan_pct_from_gw_state_when_node1_does_not_send_it():
    h, mqtt, clock = make()
    h.step()
    mqtt.deliver(TOPIC_GW_STATE, {"fan_pct": 45})
    run(h, clock, 60, every=lambda: mqtt.deliver(TOPIC_NODE1_TELEMETRY, {"fan_rpm": 800, "pm25": 10}))
    assert h.fan_rpm_ratio == 1.0 and "45" in h.baseline


def test_new_filter_from_the_dashboard_resets_hours_and_baseline():
    h, mqtt, clock = make()
    h.step()
    mqtt.deliver(TOPIC_GW_CONFIG, {"filter_reset_ts": 1000})
    run(h, clock, 600, every=lambda: node1(mqtt))
    assert h.filter_hours > 0.1 and h.baseline
    mqtt.deliver(TOPIC_GW_CONFIG, {"filter_reset_ts": 1000})          # same value again: nothing happens
    assert h.filter_hours > 0.1
    mqtt.deliver(TOPIC_GW_CONFIG, {"filter_reset_ts": 2000})
    assert h.filter_hours == 0 and h.baseline == {} and h.pms_hours > 0.1


def test_counters_survive_a_restart(tmp_path):
    path = str(tmp_path / "health_state.json")
    h, mqtt, clock = make(path)
    h.step()
    mqtt.deliver(TOPIC_GW_CONFIG, {"filter_reset_ts": 1000})
    run(h, clock, 1800, every=lambda: node1(mqtt))
    h.save_state()
    assert json.load(open(path))["filter_reset_ts"] == 1000

    h2, mqtt2, _ = make(path)
    assert h2.load_state()
    assert abs(h2.filter_hours - 0.5) < 0.01 and h2.baseline
    mqtt2.deliver(TOPIC_GW_CONFIG, {"filter_reset_ts": 1000})         # retained config after the restart
    assert abs(h2.filter_hours - 0.5) < 0.01


def test_disk_warning_is_logged_once(caplog):
    probe = FakeProbe(disk=90)
    h, mqtt, clock = make(probe=probe)
    with caplog.at_level("WARNING", logger="health"):
        h.step()
        run(h, clock, 30)
    assert sum("disk 90% full" in r.message for r in caplog.records) == 1


def test_system_probe_reads_this_machine_without_crashing():
    p = SystemProbe("/")
    for value in (p.cpu_temp_c(), p.ram_pct(), p.disk_pct()):
        assert value is None or isinstance(value, (int, float))
    assert isinstance(p.disk_pct(), int)
