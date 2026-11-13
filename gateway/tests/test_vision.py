import os
import threading
import time

import pytest

from common.protocol import TOPIC_GW_VISION, TOPIC_NODE1_ACK, TOPIC_NODE1_CMD
from conftest import FakeMqtt
from vision.vision import (DEFAULTS, SURVEY_ANGLES, TOPIC_CAUSE_REQUEST, TOPIC_VISION_SNAPSHOT,
                           TOPIC_VISION_SURVEY, Vision, box_to_angles, estimate_distance, persons_from_boxes)

C = dict(DEFAULTS)


def test_centre_is_zero_and_edges_are_half_the_fov():
    assert box_to_angles(320, 240, 640, 480, 60, 45) == (0.0, 0.0)
    assert box_to_angles(640, 240, 640, 480, 60, 45) == (30.0, 0.0)   # right edge
    assert box_to_angles(0, 0, 640, 480, 60, 45) == (-30.0, 22.5)     # top-left: left and up


def test_pinhole_is_not_linear():
    pan, _ = box_to_angles(480, 240, 640, 480, 60, 45)                # halfway to the right edge
    assert 16.0 < pan < 16.2                                           # atan(0.5 * tan 30°), not 15°


def test_distance_from_box_height():
    full = estimate_distance(480, 480, 45, 1.7)                       # person fills the frame height
    assert full == pytest.approx(1.7 / (2 * 0.41421), abs=0.01)  # rounded to cm
    assert estimate_distance(240, 480, 45, 1.7) == pytest.approx(2 * full, rel=1e-2)
    assert estimate_distance(0, 480, 45, 1.7) is None


def test_persons_nearest_first():
    persons = persons_from_boxes([(100, 240, 50, 100, 0.9), (500, 240, 120, 400, 0.7)], C)
    assert [p["conf"] for p in persons] == [0.7, 0.9]
    assert persons[0]["pan_err_deg"] > 0 and persons[1]["pan_err_deg"] < 0


class FakeCamera(object):
    def __init__(self, boxes=()):
        self.boxes = list(boxes)
        self.saved = []
        self.closed = False
        self.opens = 0

    def open(self):
        self.opens += 1

    def read(self):
        time.sleep(0.005)
        return "frame", self.boxes

    def save(self, frame, path):
        with open(path, "w") as f:
            f.write("jpg")
        self.saved.append(path)

    def close(self):
        self.closed = True


def make(tmp_path, camera, cfg=None):
    mqtt = FakeMqtt()
    v = Vision({"vision": dict(cfg or {}, survey_step_timeout_s=0.5, survey_settle_s=0)}, mqtt,
               open_camera=lambda: camera, snapshot_dir=str(tmp_path), sleep=lambda s: None)
    return v, mqtt


def test_publishes_persons_and_serves_snapshots(tmp_path):
    camera = FakeCamera([(320, 240, 100, 300, 0.8)])
    v, mqtt = make(tmp_path, camera, {"publish_hz": 50})
    v.start()
    try:
        snap = v.snapshot("test")
        assert snap and os.path.exists(snap["path"]) and snap["person_present"] is True
        assert mqtt.on(TOPIC_VISION_SNAPSHOT)[0]["tag"] == "test"
        deadline = time.time() + 2
        while not mqtt.on(TOPIC_GW_VISION) and time.time() < deadline:
            time.sleep(0.01)
        msg = mqtt.on(TOPIC_GW_VISION)[-1]
        assert msg["person_present"] is True and msg["persons"][0]["pan_err_deg"] == 0.0
        assert msg["surveying"] is False
    finally:
        v.stop()
    assert camera.closed


def test_only_last_200_snapshots_are_kept(tmp_path):
    for i in range(205):
        (tmp_path / ("%013d_old.jpg" % i)).write_text("x")
    v, mqtt = make(tmp_path, FakeCamera())
    v._save_snapshot(FakeCamera(), "frame", "new", False)
    files = sorted(os.listdir(str(tmp_path)))
    assert len(files) == 200 and "0000000000000_old.jpg" not in files


def test_camera_missing_is_retried_without_reloading_the_model(tmp_path):
    loads = []

    class NoCamera(FakeCamera):
        def open(self):
            self.opens += 1
            raise IOError("failed to create videoSource device")

    camera = NoCamera()

    def load():
        loads.append(1)
        return camera

    mqtt = FakeMqtt()
    v = Vision({"vision": {"camera_retry_s": 0.01}}, mqtt, open_camera=load, snapshot_dir=str(tmp_path))
    v.start()
    time.sleep(0.1)
    v.stop()
    assert len(loads) == 1 and camera.opens > 2      # network loaded once, video source retried
    assert not mqtt.on(TOPIC_GW_VISION)


def ack_node1(mqtt):
    """Pretend uart_bridge + Node 1 ACK every survey_step right away."""
    original = mqtt.publish

    def publish(topic, msg, qos=1, retain=False):
        original(topic, msg, qos, retain)
        if topic == TOPIC_NODE1_CMD:
            threading.Thread(target=mqtt.deliver, args=(TOPIC_NODE1_ACK, {"ack_seq": msg["seq"]})).start()
        return True
    mqtt.publish = publish


def test_survey_turns_through_8_angles_takes_8_snapshots_and_turns_back(tmp_path):
    camera = FakeCamera()
    v, mqtt = make(tmp_path, camera)
    ack_node1(mqtt)
    v.start()
    try:
        mqtt.deliver("ab/node1/telemetry", {"ts": 1, "pan_deg": 30.0})
        assert v.start_survey("request")
        assert not v.start_survey("request")          # one at a time
        deadline = time.time() + 5
        while not mqtt.on(TOPIC_VISION_SURVEY) and time.time() < deadline:
            time.sleep(0.01)
    finally:
        v.stop()
    steps = [m["pan_deg"] for m in mqtt.on(TOPIC_NODE1_CMD)]
    assert steps == list(SURVEY_ANGLES) + [30.0]
    survey = mqtt.on(TOPIC_VISION_SURVEY)[0]
    assert len(survey["snapshots"]) == 8 and survey["reason"] == "request"
    assert mqtt.on(TOPIC_CAUSE_REQUEST)[0]["reason"] == "survey"
    assert not v.surveying.is_set()


def test_spike_triggers_survey_but_not_at_night(tmp_path):
    v, mqtt = make(tmp_path, FakeCamera())
    started = []
    v.run_survey = lambda reason: (started.append(reason), v.surveying.clear())
    mqtt.handlers = []
    v.mqtt.subscribe("ab/gw/cause", v.on_cause)
    v.mqtt.subscribe("ab/gw/state", v.on_state)
    mqtt.deliver("ab/gw/state", {"mode": "night"})
    mqtt.deliver("ab/gw/cause", {"source": "rules", "reason": "spike", "cause": "cooking"})
    mqtt.deliver("ab/gw/state", {"mode": "idle"})
    mqtt.deliver("ab/gw/cause", {"source": "rules", "reason": "periodic"})
    mqtt.deliver("ab/gw/cause", {"source": "rules", "reason": "spike"})
    time.sleep(0.05)
    assert started == ["spike"]
