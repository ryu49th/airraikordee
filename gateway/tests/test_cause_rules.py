import time

from ai.cause_rules import ADVICE_TH, CAUSES, DEFAULTS, CauseRules, OutdoorPm, classify, features
from common.protocol import TOPIC_GW_CAUSE, TOPIC_GW_ENV, TOPIC_NODE1_TELEMETRY
from conftest import FakeMqtt

CFG = dict(DEFAULTS)
T0 = 1794384000.0


def series(values, step=10, start=T0):
    return [(start + i * step, float(v)) for i, v in enumerate(values)]


def run(pm_values, rh_values=None, outdoor=None, step=10):
    pm = series(pm_values, step)
    rh = series(rh_values, step) if rh_values else []
    f = features(pm, rh, pm[-1][0], CFG, outdoor)
    return classify(f, CFG), f


def test_spike_with_humidity_rise_is_cooking():
    (cause, conf), f = run([12] * 30 + [20, 30, 45, 55, 60], [60] * 30 + [62, 65, 68, 70, 72])
    assert cause == "cooking" and 0.6 <= conf <= 0.85
    assert f["pm_rise_fast"] == 48.0 and f["rh_rise_fast"] >= 10


def test_spike_with_flat_humidity_is_incense_with_low_confidence():
    (cause, conf), _ = run([12] * 30 + [20, 30, 45, 55, 60], [60] * 35)
    assert cause == "incense_candle" and conf < 0.8   # low enough that the cloud AI is asked


def test_spike_without_humidity_data_is_even_less_sure():
    (cause, conf), _ = run([12] * 30 + [20, 30, 45, 55, 60])
    assert cause == "incense_candle" and conf <= 0.5


def test_slow_rise_with_high_outdoor_pm_is_outdoor_infiltration():
    slow = [15 + i * 0.12 for i in range(181)]      # +21.6 over 30 min, never 15 within 5 min
    (cause, conf), _ = run(slow, outdoor=60)
    assert cause == "outdoor_infiltration" and conf == 0.7


def test_slow_rise_with_outdoor_slightly_lower_is_window_open():
    slow = [15 + i * 0.12 for i in range(181)]
    (cause, _), f = run(slow, outdoor=0.9 * (15 + 180 * 0.12))
    assert cause == "window_open"


def test_slow_rise_without_outdoor_data_or_flat_pm_is_unknown():
    slow = [15 + i * 0.12 for i in range(181)]
    assert run(slow)[0][0] == "unknown"
    assert run([20] * 100)[0] == ("unknown", 0.2)
    assert classify(None, CFG) == ("unknown", 0.0)


def test_every_cause_has_thai_advice():
    assert set(CAUSES) == set(ADVICE_TH)


def test_classification_is_fast():
    pm = series([12] * 175 + [20, 30, 45, 55, 60])
    rh = series([60] * 180)
    start = time.time()
    for _ in range(100):
        classify(features(pm, rh, pm[-1][0], CFG, 40), CFG)
    assert (time.time() - start) / 100 < 0.05


def test_service_publishes_once_per_spike_and_answers_requests():
    mqtt = FakeMqtt()
    rules = CauseRules({}, mqtt, outdoor=OutdoorPm("", "pm25", 900), clock=lambda: T0)
    rules.start()
    t = T0
    for v in [12] * 30 + [20, 30, 45, 55, 60, 62, 61]:
        mqtt.deliver(TOPIC_GW_ENV, {"ts": t, "rh": 60 if v < 20 else 70})
        mqtt.deliver(TOPIC_NODE1_TELEMETRY, {"node_id": "node1", "seq": 1, "ts": t, "pm25": v})
        t += 10
    causes = mqtt.on(TOPIC_GW_CAUSE)
    assert len(causes) == 1                          # one result for the whole spike
    msg = causes[0]
    assert msg["cause"] == "cooking" and msg["source"] == "rules" and msg["reason"] == "spike"
    assert msg["id"] == "c%d" % msg["ts"] and msg["advice_th"] == ADVICE_TH["cooking"]
    mqtt.deliver("ab/gw/cause/request", {"reason": "voice"})
    assert mqtt.on(TOPIC_GW_CAUSE)[-1]["reason"] == "voice"


class FakeSession(object):
    def __init__(self, payload=None, fail=False):
        self.payload, self.fail, self.calls = payload, fail, 0

    def get(self, url, timeout=None):
        self.calls += 1
        if self.fail:
            raise IOError("offline")
        session = self

        class R(object):
            def json(self):
                return session.payload
        return R()


def test_outdoor_pm_reads_nested_key_caches_and_survives_offline():
    clock = [0.0]
    s = FakeSession({"data": {"iaqi": {"pm25": {"v": 57}}}})
    o = OutdoorPm("https://example/api", "data.iaqi.pm25.v", 900, session=s, clock=lambda: clock[0])
    assert o.get() == 57.0 and o.get() == 57.0 and s.calls == 1
    s.fail = True
    clock[0] += 1000
    assert o.get() == 57.0                           # last good value while offline
    assert OutdoorPm("", "pm25", 900).get() is None
