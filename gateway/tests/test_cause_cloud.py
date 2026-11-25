import json
import threading

import pytest

from ai.cause_cloud import SCHEMA, CauseCloud, CauseLog, build_request, parse_response
from common.protocol import TOPIC_GW_CAUSE
from conftest import FakeMqtt

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

RULES = {"ts": 1794384000, "id": "c1794384000", "cause": "incense_candle", "confidence": 0.55,
         "source": "rules", "features": {"pm_now": 60, "pm_rise_fast": 48, "rh_rise_fast": 0.5}}


def reply(cause="cooking", confidence=0.83, advice="ปิดประตูครัว", stop="end_turn"):
    return {"stop_reason": stop, "content": [{"type": "thinking", "thinking": ""},
                                             {"type": "text", "text": json.dumps({"cause": cause,
                                                                                  "confidence": confidence,
                                                                                  "advice": advice})}],
            "usage": {"input_tokens": 1200, "output_tokens": 40}}


def test_request_has_images_schema_and_fallback():
    body = build_request(["AAAA", "BBBB"], RULES)
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    content = body["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "image", "text"]
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert "incense_candle" in content[-1]["text"] and "pm_rise_fast" in content[-1]["text"]
    assert "temperature" not in body and "thinking" not in body


def test_parse_response_accepts_valid_and_rejects_the_rest():
    assert parse_response(reply()) == {"cause": "cooking", "confidence": 0.83, "advice": "ปิดประตูครัว"}
    assert parse_response(reply(confidence=1.7))["confidence"] == 1.0
    assert parse_response(reply(stop="refusal")) is None
    assert parse_response(reply(cause="aliens")) is None
    assert parse_response({"stop_reason": "end_turn", "content": [{"type": "text", "text": "not json"}]}) is None


class FakeBlur(object):
    def __init__(self):
        self.calls = 0

    def blur(self, image):
        self.calls += 1
        return image, 1


class Resp(object):
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession(object):
    def __init__(self, resp=None, error=None):
        self.resp, self.error, self.calls = resp, error, []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append((url, json, headers))
        if self.error:
            raise self.error
        return self.resp


def make(tmp_path, session, api_key="sk-test", clock=None):
    clock = clock or [1000.0]
    mqtt = FakeMqtt()
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    cloud = CauseCloud({}, mqtt, api_key, CauseLog(str(tmp_path / "log.db")), session=session,
                       blurrer=FakeBlur(), read_image=lambda p: image.copy(), clock=lambda: clock[0])
    cloud.start()
    return cloud, mqtt, clock


def test_low_confidence_with_survey_asks_cloud_and_publishes(tmp_path):
    session = FakeSession(Resp(200, reply()))
    cloud, mqtt, clock = make(tmp_path, session)
    mqtt.deliver("ab/gw/vision/survey", {"snapshots": ["a.jpg", "b.jpg", "c.jpg"]})
    msg = cloud.ask(RULES)
    assert msg["source"] == "cloud" and msg["cause"] == "cooking" and msg["advice_th"] == "ปิดประตูครัว"
    assert msg["rules_cause"] == "incense_candle"
    url, body, headers = session.calls[0]
    assert headers["anthropic-version"] == "2026-10-21" and headers["x-api-key"] == "sk-test"
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert len([b for b in body["messages"][0]["content"] if b["type"] == "image"]) == 3
    assert cloud._blurrer.calls == 3                  # every image blurred before upload
    assert mqtt.on(TOPIC_GW_CAUSE)[0]["source"] == "cloud"
    assert ("cloud", "cooking", 0.83, "incense_candle") in cloud.log.rows()


def test_when_not_to_ask(tmp_path):
    cloud, mqtt, clock = make(tmp_path, FakeSession(Resp(200, reply())))
    assert cloud.should_ask(RULES) == "no recent survey snapshots"
    mqtt.deliver("ab/gw/vision/survey", {"snapshots": ["a.jpg"]})
    assert cloud.should_ask(dict(RULES, confidence=0.9)) == "rules are confident"
    assert cloud.should_ask(RULES) is None
    cloud.ask(RULES)
    clock[0] += 60
    assert "less than" in cloud.should_ask(RULES)    # max once per 10 min
    clock[0] += 600
    assert cloud.should_ask(RULES) is None
    clock[0] += 1000
    assert cloud.should_ask(RULES) == "no recent survey snapshots"
    cloud.api_key = ""
    assert cloud.should_ask(RULES) == "AI_API_KEY not set"


def test_failures_keep_the_rules_answer(tmp_path):
    for session in (FakeSession(error=IOError("offline")), FakeSession(Resp(529, {"error": "overloaded"})),
                    FakeSession(Resp(200, reply(stop="refusal")))):
        cloud, mqtt, clock = make(tmp_path, session)
        mqtt.deliver("ab/gw/vision/survey", {"snapshots": ["a.jpg"]})
        assert cloud.ask(RULES) is None
        assert not mqtt.on(TOPIC_GW_CAUSE)


def test_no_upload_when_faces_cannot_be_blurred(tmp_path):
    session = FakeSession(Resp(200, reply()))
    cloud, mqtt, clock = make(tmp_path, session)
    cloud._blurrer = None
    import ai.cause_cloud as cc
    original = cc.FaceBlurrer

    def broken(cv2=None):
        raise RuntimeError("no Haar face cascade found")
    cc.FaceBlurrer = broken
    try:
        mqtt.deliver("ab/gw/vision/survey", {"snapshots": ["a.jpg"]})
        assert cloud.ask(RULES) is None
    finally:
        cc.FaceBlurrer = original
    assert session.calls == []


def test_real_haar_blur_changes_face_pixels_only():
    from ai.cause_cloud import FaceBlurrer
    try:
        blurrer = FaceBlurrer(cv2)
    except RuntimeError:
        pytest.skip("no Haar cascades on this machine")
    image = np.full((200, 200, 3), 128, dtype=np.uint8)
    out, faces = blurrer.blur(image.copy())
    assert faces == 0 and (out == image).all()      # nothing to blur in a plain image
