import base64
import io
import json
import struct
import wave

from bridge.uart_bridge import UartBridge
from common.protocol import (TOPIC_GW_CAUSE, TOPIC_GW_VISION, TOPIC_NODE1_AUDIO, TOPIC_NODE1_CMD,
                             TOPIC_NODE1_TELEMETRY)
from conftest import FakeMqtt
from voice.voice import (DEFAULTS, REPLIES, TOPIC_CAUSE_REQUEST, TOPIC_GW_VOICE, TOPIC_VISION_CMD, AudioAssembler,
                         CloudError, GoogleTts, Voice, build_llm_request, chime_wav, keyword_intent,
                         parse_llm_response, prepare, scale_wav, wav_bytes)

FRAME = 1600  # 50 ms of 16 kHz 16-bit mono


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def frames(sid, n, skip=()):
    out = []
    for i in range(n):
        if i not in skip:
            pcm = bytes([i % 256]) * FRAME
            out.append({"audio": sid, "i": i, "pcm": base64.b64encode(pcm).decode("ascii")})
    return out


def end(sid, n):
    return {"audio": sid, "end": n, "rate": 16000, "reason": "silence"}


def assembler():
    clock = Clock()
    asks, done = [], []
    a = AudioAssembler(DEFAULTS, lambda sid, m: asks.append((sid, list(m))), lambda c, pcm: done.append((c, pcm)),
                       clock)
    return a, asks, done, clock


# --- keywords and LLM ------------------------------------------------------------------------------------

def test_keywords_cover_the_four_commands():
    assert keyword_intent("หันมาทางนี้หน่อย") == "aim"
    assert keyword_intent("ฟอกแรงขึ้น") == "fan_up"
    assert keyword_intent("เบาลงหน่อย") == "fan_down"
    assert keyword_intent("ทำไมฝุ่นเยอะ") == "why"
    assert keyword_intent("หยุด") == "stop"
    assert keyword_intent("หยุดหันได้แล้ว") == "stop"          # stop wins over aim
    assert keyword_intent("วันนี้ อากาศ ดี ไหม") is None


def test_llm_request_and_answer():
    body = build_llm_request('เปิด "พัดลม" ให้หน่อย')
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["format"]["schema"]["properties"]["intent"]["enum"][-1] == "unknown"
    assert '"พัดลม"' not in body["messages"][0]["content"]  # quotes in the transcript cannot break the prompt
    ok = {"stop_reason": "end_turn",
          "content": [{"type": "text", "text": json.dumps({"intent": "fan_up", "reply": "เพิ่มแรงลมแล้วครับ"})}]}
    assert parse_llm_response(ok) == ("fan_up", "เพิ่มแรงลมแล้วครับ")
    assert parse_llm_response({"stop_reason": "refusal", "content": []}) is None
    bad = {"content": [{"type": "text", "text": json.dumps({"intent": "dance", "reply": ""})}]}
    assert parse_llm_response(bad) is None


# --- audio frames ------------------------------------------------------------------------------------------

def test_complete_clip_is_joined_in_order_and_acked():
    a, asks, done, _ = assembler()
    for f in reversed(frames(7, 10)):
        a.on_frame(f)
    a.on_frame(end(7, 10))
    assert asks == [(7, [])]
    clip, pcm = done[0]
    assert clip.sid == 7 and len(pcm) == 10 * FRAME and pcm[FRAME * 3] == 3


def test_missing_frames_are_asked_for_and_resent():
    a, asks, done, clock = assembler()
    for f in frames(1, 10, skip=(2, 5)):
        a.on_frame(f)
    a.on_frame(end(1, 10))
    assert asks == [(1, [2, 5])] and not done
    a.tick()
    assert len(asks) == 1  # waits audio_retry_s before asking again
    for f in frames(1, 10):
        if f["i"] in (2, 5):
            a.on_frame(f)
    a.on_frame(end(1, 10))
    assert asks[-1] == (1, []) and len(done[0][1]) == 10 * FRAME


def test_gives_up_after_retries_then_patches_or_drops():
    a, asks, done, clock = assembler()
    for f in frames(2, 20, skip=(4,)):
        a.on_frame(f)
    a.on_frame(end(2, 20))
    for _ in range(DEFAULTS["audio_retries"] - 1):  # the end marker itself started round 1
        clock.t += 1.1
        a.tick()
    assert len([x for x in asks if x[1]]) == 3 and not done
    clock.t += 1.1
    a.tick()
    assert asks[-1] == (2, []) and len(done[0][1]) == 20 * FRAME  # 95 % came in: gap filled with silence

    a, asks, done, clock = assembler()
    for f in frames(3, 10, skip=(1, 2, 3)):
        a.on_frame(f)
    a.on_frame(end(3, 10))
    for _ in range(4):
        clock.t += 1.1
        a.tick()
    assert done[0][1] is None  # only 70 %: dropped


def test_clip_without_end_marker_times_out():
    a, asks, done, clock = assembler()
    for f in frames(4, 5):
        a.on_frame(f)
    clock.t += 9
    a.tick()
    assert a.clips == {} and not done


def test_bridge_forwards_audio_lines_without_touching_seq():
    mqtt = FakeMqtt()
    bridge = UartBridge({"node1": {"device": "/dev/fake"}}, mqtt)
    line = json.dumps(frames(9, 1)[0]).encode() + b"\n"
    bridge.handle_line(line)
    assert mqtt.on(TOPIC_NODE1_AUDIO)[0]["audio"] == 9
    assert mqtt.on(TOPIC_NODE1_TELEMETRY) == []
    assert bridge.stats["audio_lines"] == 1 and bridge.rx_seq.received == {}
    assert len(line) < 4096


# --- wav helpers ------------------------------------------------------------------------------------------

def test_wav_helpers():
    w = wave.open(io.BytesIO(chime_wav()), "rb")
    assert w.getframerate() == 16000 and w.getnframes() > 3000
    loud = wav_bytes(struct.pack("<2h", 10000, -10000), 16000)
    quiet = wave.open(io.BytesIO(scale_wav(loud, 0.5)), "rb").readframes(2)
    assert struct.unpack("<2h", quiet) == (5000, -5000)


# --- the whole service with fakes ---------------------------------------------------------------------------

class FakeStt(object):
    def __init__(self, text="", fail=False):
        self.text = text
        self.fail = fail

    def transcribe(self, pcm, rate):
        if self.fail:
            raise CloudError("offline")
        return self.text


class FakeTts(object):
    def __init__(self, fail=False):
        self.said = []
        self.fail = fail

    def synthesize(self, text):
        if self.fail:
            raise CloudError("offline")
        self.said.append(text)
        return "/tmp/x.wav"


class FakeLlm(object):
    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def classify(self, text):
        self.calls.append(text)
        if self.answer is None:
            raise CloudError("no key")
        return self.answer


class FakePlayer(object):
    def __init__(self):
        self.played = []

    def play(self, path):
        self.played.append(path)

    def play_bytes(self, data, name):
        self.played.append(name)


def make(text="", llm=None, stt_fail=False):
    clock = Clock()
    mqtt = FakeMqtt()
    tts = FakeTts()
    v = Voice({"voice": {"cause_wait_s": 0.05}}, mqtt, FakeStt(text, stt_fail), tts, FakeLlm(llm), FakePlayer(),
              clock)
    return v, mqtt, tts, clock


def cmds(mqtt, name):
    return [m for m in mqtt.on(TOPIC_NODE1_CMD) if m["cmd"] == name]


def test_aim_turns_to_the_nearest_person():
    v, mqtt, tts, clock = make("หันมาทางนี้")
    v.on_telemetry(TOPIC_NODE1_TELEMETRY, {"pan_deg": 40, "tilt_deg": 0, "fan_pct": 50})
    v.on_vision(TOPIC_GW_VISION, {"persons": [{"pan_err_deg": -12.5, "tilt_err_deg": 4}]})
    r = v.handle_clip(1, b"\0" * 3200, 16000, wake_at=clock.t - 2)
    aim = cmds(mqtt, "aim")[0]
    assert (aim["pan_deg"], aim["tilt_deg"]) == (27.5, 4.0)
    assert tts.said == [REPLIES["aim_ok"]] and r["ok"] and r["latency_s"] == 2.0
    assert mqtt.on(TOPIC_GW_VOICE)[0]["intent"] == "aim"


def test_aim_with_nobody_in_view_says_so():
    v, mqtt, tts, clock = make("มาทางนี้")
    v.on_vision(TOPIC_GW_VISION, {"persons": [{"pan_err_deg": 5, "tilt_err_deg": 0}]})
    clock.t += 5  # stale vision
    v.handle_clip(1, b"", 16000)
    assert cmds(mqtt, "aim") == [] and tts.said == [REPLIES["aim_nobody"]]


def test_fan_steps_and_limits():
    v, mqtt, tts, _ = make("ฟอกแรงขึ้น")
    v.on_telemetry(TOPIC_NODE1_TELEMETRY, {"fan_pct": 60})
    v.handle_clip(1, b"", 16000)
    v.handle_clip(2, b"", 16000)
    assert [c["pct"] for c in cmds(mqtt, "fan")] == [85, 100]
    v.handle_clip(3, b"", 16000)
    assert len(cmds(mqtt, "fan")) == 2 and tts.said[-1] == REPLIES["fan_max"]
    v.stt.text = "เบาลง"
    v.handle_clip(4, b"", 16000)
    assert cmds(mqtt, "fan")[-1]["pct"] == 75 and tts.said[-1] == REPLIES["fan_down"]


def test_stop():
    v, mqtt, tts, _ = make("หยุดเลย")
    v.handle_clip(1, b"", 16000)
    assert len(cmds(mqtt, "stop")) == 1 and tts.said == [REPLIES["stop"]]


def test_why_uses_a_fresh_cause_or_asks_for_one():
    v, mqtt, tts, clock = make("ทำไมฝุ่นเยอะ")
    v.on_cause(TOPIC_GW_CAUSE, {"cause": "cooking", "advice_th": "น่าจะมาจากการทำอาหาร"})
    v.handle_clip(1, b"", 16000)
    assert tts.said == ["น่าจะมาจากการทำอาหาร"] and mqtt.on(TOPIC_CAUSE_REQUEST) == []

    clock.t += 3600
    v.handle_clip(2, b"", 16000)  # nobody answers within cause_wait_s
    assert mqtt.on(TOPIC_CAUSE_REQUEST)[0]["reason"] == "voice"
    assert mqtt.on(TOPIC_VISION_CMD)[0] == {"cmd": "survey", "reason": "voice"}
    assert tts.said[-2:] == [REPLIES["why_wait"], REPLIES["why_none"]]


def test_llm_fallback_and_its_reply():
    v, mqtt, tts, _ = make("ช่วยเปิดลมให้แรงกว่านี้", llm=("fan_up", "ได้เลยครับ"))
    v.on_telemetry(TOPIC_NODE1_TELEMETRY, {"fan_pct": 25})
    r = v.handle_clip(1, b"", 16000)
    assert r["source"] == "llm" and cmds(mqtt, "fan")[0]["pct"] == 50
    assert tts.said == [REPLIES["fan_up"]]  # commands use the fixed (cached) reply

    v, mqtt, tts, _ = make("วันนี้อากาศเป็นไง", llm=("unknown", "วันนี้ฝุ่นน้อยครับ"))
    r = v.handle_clip(1, b"", 16000)
    assert tts.said == ["วันนี้ฝุ่นน้อยครับ"] and not r["ok"]

    v, mqtt, tts, _ = make("อะไรก็ไม่รู้")  # no AI key: unknown
    v.handle_clip(1, b"", 16000)
    assert tts.said == [REPLIES["unknown"]]


def test_offline_and_silence():
    v, mqtt, tts, _ = make(stt_fail=True)
    r = v.handle_clip(1, b"", 16000)
    assert tts.said == [REPLIES["offline"]] and r["source"] == "offline" and mqtt.on(TOPIC_NODE1_CMD) == []
    v, mqtt, tts, _ = make("")
    v.handle_clip(1, b"", 16000)
    assert tts.said == [REPLIES["no_speech"]]


def test_frames_to_command_end_to_end():
    v, mqtt, tts, clock = make("หยุด")
    v.on_event(None, {"type": "wake"})
    for f in frames(5, 6):
        v.on_audio(TOPIC_NODE1_AUDIO, f)
    v.on_audio(TOPIC_NODE1_AUDIO, end(5, 6))
    v._jobs.get()()  # run the queued job on this thread
    assert cmds(mqtt, "audio_ack")[0]["missing"] == [] and len(cmds(mqtt, "stop")) == 1
    assert not v.busy


def test_tts_cache_and_prepare(tmp_path):
    class Resp(object):
        status_code = 200

        def json(self):
            return {"audioContent": base64.b64encode(wav_bytes(b"\0\0" * 10, 24000)).decode("ascii")}

    class Session(object):
        def __init__(self):
            self.calls = 0

        def post(self, url, params=None, json=None, timeout=None):
            self.calls += 1
            assert params == {"key": "k"} and json["voice"]["languageCode"] == "th-TH"
            return Resp()

    s = Session()
    tts = GoogleTts("k", s, DEFAULTS, str(tmp_path / "cache"))
    p = tts.synthesize("หยุดแล้วครับ")
    assert open(p, "rb").read(4) == b"RIFF" and tts.synthesize("หยุดแล้วครับ") == p and s.calls == 1
    made, total = prepare(tts)
    assert made == total - 1 and s.calls == total
    offline = GoogleTts("", s, DEFAULTS, str(tmp_path / "cache"))
    assert offline.synthesize(REPLIES["offline"])  # cached: works without a key or internet


def test_night_mode_runs_commands_without_speaking():
    from common.protocol import TOPIC_GW_STATE
    v, mqtt, tts, _ = make("หยุด")
    v.on_state(TOPIC_GW_STATE, {"mode": "night"})
    v.on_event(None, {"type": "wake"})
    r = v.handle_clip(1, b"", 16000)
    assert len(cmds(mqtt, "stop")) == 1 and tts.said == [] and v.player.played == [] and r["reply"] == REPLIES["stop"]
    v.on_state(TOPIC_GW_STATE, {"mode": "idle"})
    v.handle_clip(2, b"", 16000)
    assert tts.said == [REPLIES["stop"]]


def test_chat_mode_types_commands_and_prints_replies(capsys):
    from voice.voice import ClaudeIntent, chat
    lines = iter(["ฟอกแรงขึ้น", "หยุด", "วันนี้เป็นไง", "exit"])
    out = []
    chat({}, ClaudeIntent("", None, DEFAULTS), read=lambda prompt: next(lines), write=out.append)
    printed = capsys.readouterr().out
    assert "เครื่อง: " + REPLIES["fan_up"] in out and "เครื่อง: " + REPLIES["stop"] in out
    assert "เครื่อง: " + REPLIES["unknown"] in out              # no AI key: keywords only
    assert '"cmd": "fan", "pct": 75' in printed and '"cmd": "stop"' in printed   # shown, not sent
