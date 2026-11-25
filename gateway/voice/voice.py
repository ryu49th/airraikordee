"""Voice commands (AIR-20, GW-15): wake word on Node 1 -> spoken command -> action -> Thai reply.

Flow
1. Node 1 hears the wake word ("hellofoon", Edge Impulse on the ESP32-S3) and sends ab/node1/event type=wake.
   The Jetson plays a short chime.
2. Node 1 keeps recording the INMP441 until ~0.8 s of silence or 5 s, and streams the clip over the same USB serial
   link as audio frames (format below). uart_bridge forwards them on ab/node1/audio.
3. This module puts the frames back together. Missing frames are asked for again with
   ab/node1/cmd {"cmd": "audio_ack", "sid", "missing": [...]} (up to `audio_retries` rounds); an empty `missing`
   tells Node 1 it may free the clip.
4. Google Cloud Speech-to-Text (th-TH) turns the clip into text.
5. Keyword rules pick the command; if none match, Claude reads the text and picks one (or answers briefly).
6. The action goes to Node 1 / vision / cause_rules over MQTT, and the reply is spoken with Google Cloud
   Text-to-Speech on the USB speaker. Replies are cached as WAV files, so the fixed ones still play offline
   (`python3 -m voice.voice --prepare` makes them all while online).

Commands (Linear AIR-20 "done when"):
  "หันมาทางนี้"     aim   -> turn to the nearest person the camera sees
  "ฟอกแรงขึ้น/เบาลง" fan   -> fan +/- fan_step_pct
  "ทำไมฝุ่นเยอะ"    why   -> latest dust cause, or ask cause_rules (and a survey) for a fresh one, then say its advice
  "หยุด"           stop  -> stop fan and motors

Audio frames, Node 1 -> Jetson, one JSON object per line on the serial link:
  {"audio": <sid>, "i": <frame index from 0>, "pcm": "<base64 of 16-bit little-endian mono PCM>"}
  {"audio": <sid>, "end": <number of frames>, "rate": 16000, "reason": "silence" | "max"}
  sid = a number Node 1 picks per wake (e.g. a counter). 50 ms per frame (1600 bytes -> 2136 base64 characters)
  keeps every line under uart_bridge's 4096-byte limit. 16 kHz x 16 bit = 32 kB/s, which needs the ESP32-S3's
  native USB (USB CDC); the 115200-baud UART port is too slow.

Night mode (ab/gw/state mode "night", from decision.py): no chime and no spoken reply; commands still run.

Publishes ab/gw/voice {ts, sid, text, intent, source, reply, ok, latency_s} after each command (metrics).

Keys in .env: STT_API_KEY and TTS_API_KEY (one Google Cloud API key may serve both, with the Speech-to-Text and
Text-to-Speech APIs enabled), AI_API_KEY for the Claude fallback.

Run on the Jetson from gateway/:  python3 -m voice.voice
"""
import base64
import hashlib
import io
import json
import logging
import math
import os
import queue
import struct
import subprocess
import threading
import time
import wave

from common.protocol import (TOPIC_GW_CAUSE, TOPIC_GW_STATE, TOPIC_GW_VISION, TOPIC_NODE1_AUDIO, TOPIC_NODE1_CMD,
                             TOPIC_NODE1_EVENT, TOPIC_NODE1_TELEMETRY, SeqCounter, now_ts)

log = logging.getLogger("voice")

TOPIC_GW_VOICE = "ab/gw/voice"
TOPIC_VISION_CMD = "ab/gw/vision/cmd"
TOPIC_CAUSE_REQUEST = "ab/gw/cause/request"

STT_URL = "https://speech.googleapis.com/v1/speech:recognize"
TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-opus-5-5"

INTENTS = ("aim", "fan_up", "fan_down", "why", "stop", "unknown")
PAN_LIMIT = 180.0
TILT_LIMIT = 30.0

DEFAULTS = {
    "audio_timeout_s": 8.0,      # wake -> last frame; Node 1 stops at 5 s, the rest is slack
    "audio_retry_s": 1.0,        # wait this long for resent frames before asking again
    "audio_retries": 3,
    "audio_min_ratio": 0.9,      # after the last retry, use the clip if at least this share of frames came in
    "language": "th-TH",
    "tts_voice": "th-TH-Standard-A",
    "tts_rate_hz": 24000,
    "http_timeout_s": 8.0,
    "llm_timeout_s": 15.0,
    "fan_step_pct": 25,
    "person_max_age_s": 2.0,
    "cause_fresh_s": 600,        # a cause younger than this is answered at once
    "cause_wait_s": 20,          # otherwise wait this long for cause_rules
    "player": "aplay -q",        # + " -D plughw:<card>" for the USB speaker, see README
    "volume": 1.0,
}

# Every fixed sentence the unit can say; `--prepare` turns each into a cached WAV so they work offline.
REPLIES = {
    "aim_ok": "หันมาแล้วครับ",
    "aim_nobody": "ไม่เห็นใครเลยครับ",
    "fan_up": "เพิ่มแรงลมแล้วครับ",
    "fan_down": "ลดแรงลมแล้วครับ",
    "fan_max": "แรงสุดแล้วครับ",
    "fan_min": "เบาสุดแล้วครับ",
    "why_wait": "กำลังดูให้ครับ",
    "why_none": "ยังไม่รู้ว่าฝุ่นมาจากไหนครับ",
    "stop": "หยุดแล้วครับ",
    "unknown": "ขอโทษครับ ไม่เข้าใจคำสั่ง",
    "no_speech": "ไม่ได้ยินครับ ลองพูดใหม่อีกครั้ง",
    "offline": "ตอนนี้ไม่มีอินเทอร์เน็ต สั่งด้วยเสียงไม่ได้ ใช้ปุ่มหรือแอปแทนนะครับ",
    "busy": "รอสักครู่ครับ",
}

# Keyword rules, checked in this order (Thai has no spaces, so plain substring tests; STT output is lower-cased).
KEYWORDS = (
    ("stop", ("หยุด", "ปิดเครื่อง", "พอแล้ว", "stop")),
    ("why", ("ทำไมฝุ่น", "ฝุ่นมาจาก", "ฝุ่นเยอะ", "ฝุ่นเยอะจัง", "ทำไมอากาศ")),
    ("fan_up", ("แรงขึ้น", "แรงหน่อย", "เร่ง", "เพิ่มแรง", "เปิดแรง", "ฟอกแรง")),
    ("fan_down", ("เบาลง", "เบาหน่อย", "ลดแรง", "ช้าลง", "ค่อยลง", "เสียงดัง")),
    ("aim", ("หันมา", "มาทางนี้", "ทางนี้", "หันหน้า", "มองมา", "ตามมา")),
)
# Phrases that help Speech-to-Text hear the commands right.
PHRASES = ["หันมาทางนี้", "ฟอกแรงขึ้น", "เบาลง", "ทำไมฝุ่นเยอะ", "หยุด"]


def keyword_intent(text):
    t = (text or "").replace(" ", "").lower()
    for intent, words in KEYWORDS:
        if any(w in t for w in words):
            return intent
    return None


# --- audio ---------------------------------------------------------------------------------------------

def wav_bytes(pcm, rate, channels=1, width=2):
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(channels)
    w.setsampwidth(width)
    w.setframerate(rate)
    w.writeframes(pcm)
    w.close()
    return buf.getvalue()


def chime_wav(rate=16000):
    """Two short rising beeps, made here so no sound file has to ship with the code."""
    samples = []
    for freq, dur in ((880, 0.09), (0, 0.04), (1320, 0.12)):
        n = int(rate * dur)
        for k in range(n):
            fade = min(1.0, k / 200.0, (n - k) / 200.0)
            samples.append(int(9000 * fade * math.sin(2 * math.pi * freq * k / rate)) if freq else 0)
    return wav_bytes(struct.pack("<%dh" % len(samples), *samples), rate)


def scale_wav(data, volume):
    if volume >= 0.999:
        return data
    r = wave.open(io.BytesIO(data), "rb")
    params = r.getparams()
    frames = r.readframes(r.getnframes())
    r.close()
    if params.sampwidth != 2:
        return data
    n = len(frames) // 2
    pcm = struct.unpack("<%dh" % n, frames[:n * 2])
    out = struct.pack("<%dh" % n, *[int(max(-32768, min(32767, s * volume))) for s in pcm])
    return wav_bytes(out, params.framerate, params.nchannels, 2)


class Clip(object):
    def __init__(self, sid, started):
        self.sid = sid
        self.started = started
        self.frames = {}
        self.total = None
        self.rate = 16000
        self.reason = None
        self.rounds = 0
        self.asked_at = None


class AudioAssembler(object):
    """Puts audio frames back together and decides when to ask Node 1 for missing ones.

    `ask(sid, missing)` is called to send an audio_ack; `done(clip, pcm)` when a clip is usable (pcm is None when
    it had to be dropped)."""

    def __init__(self, cfg, ask, done, clock=time.time):
        self.c = cfg
        self.ask = ask
        self.done = done
        self.clock = clock
        self.clips = {}
        self._lock = threading.Lock()

    def on_frame(self, msg):
        sid = msg.get("audio")
        if not isinstance(sid, int) or isinstance(sid, bool):
            return
        with self._lock:
            clip = self.clips.get(sid)
            if clip is None:
                clip = self.clips[sid] = Clip(sid, self.clock())
            if "pcm" in msg and isinstance(msg.get("i"), int):
                try:
                    clip.frames[msg["i"]] = base64.b64decode(msg["pcm"])
                except (TypeError, ValueError):
                    log.warning("clip %d: frame %s is not base64", sid, msg.get("i"))
                return
            if isinstance(msg.get("end"), int):
                clip.total = msg["end"]
                clip.rate = int(msg.get("rate") or 16000)
                clip.reason = msg.get("reason")
                clip.asked_at = None
        self.check(sid)

    def missing(self, clip):
        return [i for i in range(clip.total or 0) if i not in clip.frames]

    def check(self, sid):
        """Finish the clip, or ask again for what is missing. Also called from tick()."""
        with self._lock:
            clip = self.clips.get(sid)
            if clip is None or clip.total is None:
                return
            now = self.clock()
            lost = self.missing(clip)
            if not lost:
                result = "ok"
            elif clip.asked_at is not None and now - clip.asked_at < self.c["audio_retry_s"]:
                return  # still waiting for the resend
            elif clip.rounds < self.c["audio_retries"]:
                clip.rounds += 1
                clip.asked_at = now
                result = "ask"
            elif clip.total and 1.0 - len(lost) / float(clip.total) >= self.c["audio_min_ratio"]:
                result = "patch"
            else:
                result = "drop"
            if result != "ask":
                del self.clips[sid]
        if result == "ask":
            log.info("clip %d: %d of %d frames missing, asking again (%d/%d)", sid, len(lost), clip.total,
                     clip.rounds, self.c["audio_retries"])
            self.ask(sid, lost)
            return
        self.ask(sid, [])  # Node 1 may free the clip
        if result == "drop":
            log.warning("clip %d: %d of %d frames never arrived, dropped", sid, len(lost), clip.total)
            self.done(clip, None)
            return
        if lost:
            log.info("clip %d: %d frames still missing, filled with silence", sid, len(lost))
        size = max([len(f) for f in clip.frames.values()] or [0])
        pcm = b"".join(clip.frames.get(i, b"\0" * size) for i in range(clip.total))
        self.done(clip, pcm)

    def tick(self):
        now = self.clock()
        with self._lock:
            sids = list(self.clips)
            stale = [s for s in sids if self.clips[s].total is None
                     and now - self.clips[s].started > self.c["audio_timeout_s"]]
            for s in stale:
                del self.clips[s]
        for s in stale:
            log.warning("clip %d: no end marker after %.0f s, dropped", s, self.c["audio_timeout_s"])
        for s in sids:
            if s not in stale:
                self.check(s)


# --- cloud services ------------------------------------------------------------------------------------

class CloudError(Exception):
    pass


class GoogleStt(object):
    def __init__(self, api_key, session, cfg):
        self.api_key = api_key
        self.session = session
        self.c = cfg

    def transcribe(self, pcm, rate):
        """16-bit mono PCM -> text ("" when nothing was said). Raises CloudError when offline or refused."""
        if not self.api_key:
            raise CloudError("STT_API_KEY not set")
        body = {"config": {"encoding": "LINEAR16", "sampleRateHertz": rate, "languageCode": self.c["language"],
                           "speechContexts": [{"phrases": PHRASES}]},
                "audio": {"content": base64.b64encode(pcm).decode("ascii")}}
        try:
            r = self.session.post(STT_URL, params={"key": self.api_key}, json=body, timeout=self.c["http_timeout_s"])
            data = r.json()
        except Exception as e:
            raise CloudError("speech-to-text unreachable: %s" % e)
        if r.status_code != 200:
            raise CloudError("speech-to-text error %s: %s" % (r.status_code, str(data)[:200]))
        for result in data.get("results") or []:
            for alt in result.get("alternatives") or []:
                if alt.get("transcript"):
                    return alt["transcript"].strip()
        return ""


class GoogleTts(object):
    """Text -> WAV bytes, cached on disk by sentence so repeated and offline replies need no request."""

    def __init__(self, api_key, session, cfg, cache_dir):
        self.api_key = api_key
        self.session = session
        self.c = cfg
        self.cache_dir = cache_dir

    def path(self, text):
        key = "%s|%s|%s" % (self.c["tts_voice"], self.c["tts_rate_hz"], text)
        return os.path.join(self.cache_dir, hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".wav")

    def cached(self, text):
        p = self.path(text)
        return p if os.path.isfile(p) else None

    def synthesize(self, text):
        """Returns the path of a WAV file. Raises CloudError if it is not cached and cannot be made."""
        p = self.path(text)
        if os.path.isfile(p):
            return p
        if not self.api_key:
            raise CloudError("TTS_API_KEY not set")
        body = {"input": {"text": text},
                "voice": {"languageCode": self.c["language"], "name": self.c["tts_voice"]},
                "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": self.c["tts_rate_hz"]}}
        try:
            r = self.session.post(TTS_URL, params={"key": self.api_key}, json=body, timeout=self.c["http_timeout_s"])
            data = r.json()
        except Exception as e:
            raise CloudError("text-to-speech unreachable: %s" % e)
        if r.status_code != 200 or not data.get("audioContent"):
            raise CloudError("text-to-speech error %s: %s" % (r.status_code, str(data)[:200]))
        audio = base64.b64decode(data["audioContent"])  # LINEAR16 comes back as a WAV file with its header
        if not audio.startswith(b"RIFF"):
            audio = wav_bytes(audio, self.c["tts_rate_hz"])
        if not os.path.isdir(self.cache_dir):
            os.makedirs(self.cache_dir)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(audio)
        os.replace(tmp, p)
        return p


LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "reply": {"type": "string"},
    },
    "required": ["intent", "reply"],
    "additionalProperties": False,
}
LLM_PROMPT = """You are the voice of a small home air purifier in Thailand. It has a camera head that can turn to \
face people, a fan, and a PM2.5 sensor. Someone said this to it (speech-to-text, so words may be misheard):

"%s"

Pick the command they meant:
- aim: turn to face the person speaking
- fan_up: clean faster / stronger airflow
- fan_down: clean slower / quieter
- why: asks why there is dust, where the dust comes from, or how the air is
- stop: stop / turn off
- unknown: none of these

reply: one short, friendly spoken sentence in Thai, at most 15 words, polite with ครับ. For a command, confirm it \
(the unit acts on it). For unknown, answer briefly if it is a simple question about air or the purifier, \
otherwise say you can only turn, change the fan, explain the dust, or stop."""


def build_llm_request(text):
    return {
        "model": CLAUDE_MODEL,
        "max_tokens": 1024,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": LLM_SCHEMA}},
        "fallbacks": "default",
        "messages": [{"role": "user", "content": LLM_PROMPT % text.replace('"', "'")}],
    }


def parse_llm_response(body):
    """Messages API JSON -> (intent, reply) or None."""
    if body.get("stop_reason") == "refusal":
        log.warning("LLM declined: %s", body.get("stop_details"))
        return None
    text = next((b.get("text") for b in body.get("content", []) if b.get("type") == "text"), None)
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if data.get("intent") not in INTENTS:
        return None
    reply = (data.get("reply") or "").strip()[:200]
    return data["intent"], reply


class ClaudeIntent(object):
    """LLM fallback when no keyword matched. The Jetson's Python 3.6 cannot run the Anthropic SDK, so this
    posts to the Messages API with requests, like ai/cause_cloud.py."""

    def __init__(self, api_key, session, cfg):
        self.api_key = api_key
        self.session = session
        self.c = cfg

    def classify(self, text):
        if not self.api_key:
            raise CloudError("AI_API_KEY not set")
        try:
            r = self.session.post(CLAUDE_URL, json=build_llm_request(text), timeout=self.c["llm_timeout_s"],
                                  headers={"x-api-key": self.api_key, "anthropic-version": "2026-10-21",
                                           "anthropic-beta": "server-side-fallback-2026-07-01",
                                           "content-type": "application/json"})
            body = r.json()
        except Exception as e:
            raise CloudError("LLM unreachable: %s" % e)
        if r.status_code != 200:
            raise CloudError("LLM error %s: %s" % (r.status_code, str(body)[:200]))
        answer = parse_llm_response(body)
        if answer is None:
            raise CloudError("LLM answer unusable")
        return answer


class Player(object):
    """Plays WAV files on the USB speaker one after another (aplay)."""

    def __init__(self, cfg, cache_dir):
        self.cmd = cfg["player"].split()
        self.volume = float(cfg["volume"])
        self.cache_dir = cache_dir
        self._lock = threading.Lock()

    def play_bytes(self, data, name):
        if not os.path.isdir(self.cache_dir):
            os.makedirs(self.cache_dir)
        p = os.path.join(self.cache_dir, name)
        with open(p, "wb") as f:
            f.write(scale_wav(data, self.volume))
        self.play(p, scaled=True)

    def play(self, path, scaled=False):
        if not scaled and self.volume < 0.999:
            with open(path, "rb") as f:
                data = scale_wav(f.read(), self.volume)
            path = path + ".vol.wav"
            with open(path, "wb") as f:
                f.write(data)
        with self._lock:
            try:
                subprocess.call(self.cmd + [path], timeout=30)
            except Exception as e:
                log.warning("cannot play %s: %s", path, e)


# --- the voice service ---------------------------------------------------------------------------------

class Voice(object):
    def __init__(self, cfg, mqtt, stt, tts, llm, player, clock=time.time, text_out=None):
        self.text_out = text_out   # chat mode: replies are printed instead of spoken
        c = dict(DEFAULTS)
        c.update(cfg.get("voice") or {})
        self.c = c
        self.mqtt = mqtt
        self.stt = stt
        self.tts = tts
        self.llm = llm
        self.player = player
        self.clock = clock
        self.assembler = AudioAssembler(c, self._ask, self._clip_done, clock)
        self._seq = SeqCounter()
        self._jobs = queue.Queue()
        self._stop = threading.Event()
        self._threads = []
        self._lock = threading.Lock()
        self._node1 = {}
        self._vision = None
        self._vision_at = None
        self._cause = None
        self._cause_at = None
        self._cause_event = threading.Event()
        self._wake_at = {}
        self.busy = False
        self.night = False
        self.stats = {"wakes": 0, "clips": 0, "dropped": 0, "commands": 0, "llm": 0, "errors": 0}

    # --- lifecycle ---

    def start(self):
        self.mqtt.subscribe(TOPIC_NODE1_EVENT, self.on_event)
        self.mqtt.subscribe(TOPIC_NODE1_AUDIO, self.on_audio, qos=0)
        self.mqtt.subscribe(TOPIC_NODE1_TELEMETRY, self.on_telemetry, qos=0)
        self.mqtt.subscribe(TOPIC_GW_VISION, self.on_vision, qos=0)
        self.mqtt.subscribe(TOPIC_GW_CAUSE, self.on_cause)
        self.mqtt.subscribe(TOPIC_GW_STATE, self.on_state, qos=0)
        for target in (self._worker, self._ticker):
            t = threading.Thread(target=target, name=target.__name__, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self._jobs.put(None)
        for t in self._threads:
            t.join(2.0)

    def _worker(self):
        while not self._stop.is_set():
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception:
                self.stats["errors"] += 1
                log.exception("voice job failed")

    def _ticker(self):
        while not self._stop.wait(0.2):
            self.assembler.tick()

    # --- MQTT inputs ---

    def on_event(self, topic, msg):
        if msg.get("type") != "wake":
            return
        self.stats["wakes"] += 1
        self._wake_at["last"] = self.clock()
        log.info("wake word")
        if self.night:
            return  # quiet at night: no chime
        threading.Thread(target=self.player.play_bytes, args=(chime_wav(), "chime.wav"), daemon=True).start()

    def on_audio(self, topic, msg):
        self.assembler.on_frame(msg)

    def on_telemetry(self, topic, msg):
        with self._lock:
            for k in ("pan_deg", "tilt_deg", "fan_pct"):
                if isinstance(msg.get(k), (int, float)):
                    self._node1[k] = float(msg[k])

    def on_vision(self, topic, msg):
        with self._lock:
            self._vision = msg
            self._vision_at = self.clock()

    def on_state(self, topic, msg):
        night = msg.get("mode") == "night"
        if night != self.night:
            log.info("night mode %s: replies %s", "on" if night else "off", "muted" if night else "spoken")
        self.night = night

    def on_cause(self, topic, msg):
        with self._lock:
            self._cause = msg
            self._cause_at = self.clock()
        self._cause_event.set()

    # --- audio -> command ---

    def _ask(self, sid, missing):
        self.mqtt.publish(TOPIC_NODE1_CMD, {"seq": self._seq.next(), "ts": now_ts(), "cmd": "audio_ack",
                                            "sid": sid, "missing": missing})

    def _clip_done(self, clip, pcm):
        if pcm is None:
            self.stats["dropped"] += 1
            self._jobs.put(lambda: self.say("no_speech"))
            return
        self.stats["clips"] += 1
        if self.busy:
            log.info("clip %d ignored: still busy with the last command", clip.sid)
            return
        self.busy = True
        self._jobs.put(lambda: self.handle_clip(clip.sid, pcm, clip.rate, self._wake_at.get("last", clip.started)))

    def handle_clip(self, sid, pcm, rate, wake_at=None):
        """STT -> intent -> action -> reply. Runs on the worker thread."""
        try:
            result = {"ts": now_ts(), "sid": sid, "text": None, "intent": None, "source": None, "reply": None,
                      "ok": False}
            try:
                text = self.stt.transcribe(pcm, rate)
            except CloudError as e:
                log.warning("%s", e)
                self.say("offline")
                result["source"] = "offline"
                return self._report(result, wake_at)
            return self.handle_text(text, sid, wake_at, result)
        finally:
            self.busy = False

    def handle_text(self, text, sid=0, wake_at=None, result=None):
        """Text (from STT, or typed in --chat) -> intent -> action -> reply. Returns the ab/gw/voice record."""
        result = result or {"ts": now_ts(), "sid": sid, "text": None, "intent": None, "source": None,
                            "reply": None, "ok": False}
        try:
            result["text"] = text
            if not text:
                self.say("no_speech")
                return self._report(result, wake_at)
            intent, reply = keyword_intent(text), None
            result["source"] = "keywords"
            if intent is None:
                try:
                    intent, reply = self.llm.classify(text)
                    result["source"] = "llm"
                    self.stats["llm"] += 1
                except CloudError as e:
                    log.warning("%s", e)
                    intent = "unknown"
            log.info("heard %r -> %s (%s)", text, intent, result["source"])
            result["intent"] = intent
            result["reply"] = self.act(intent, llm_reply=reply)
            result["ok"] = intent != "unknown"
            self.stats["commands"] += 1
            return self._report(result, wake_at)
        finally:
            self.busy = False

    def _report(self, result, wake_at):
        if wake_at is not None:
            result["latency_s"] = round(self.clock() - wake_at, 2)
        self.mqtt.publish(TOPIC_GW_VOICE, result, qos=0)
        return result

    # --- actions ---

    def _cmd(self, body):
        body = dict(body)
        body["seq"] = self._seq.next()
        body["ts"] = now_ts()
        self.mqtt.publish(TOPIC_NODE1_CMD, body)

    def act(self, intent, llm_reply=None):
        """Do the command and speak. Returns the sentence that was said."""
        if intent == "stop":
            self._cmd({"cmd": "stop"})
            return self.say("stop")
        if intent in ("fan_up", "fan_down"):
            with self._lock:
                cur = self._node1.get("fan_pct")
            cur = 50.0 if cur is None else cur
            step = float(self.c["fan_step_pct"]) * (1 if intent == "fan_up" else -1)
            pct = int(max(0, min(100, cur + step)))
            if pct == int(cur):
                return self.say("fan_max" if intent == "fan_up" else "fan_min")
            self._cmd({"cmd": "fan", "pct": pct})
            with self._lock:
                self._node1["fan_pct"] = float(pct)
            return self.say(intent)
        if intent == "aim":
            target = self.aim_target()
            if target is None:
                return self.say("aim_nobody")
            self._cmd({"cmd": "aim", "pan_deg": target[0], "tilt_deg": target[1]})
            return self.say("aim_ok")
        if intent == "why":
            return self.explain_dust()
        if llm_reply:
            return self.say_text(llm_reply)
        return self.say("unknown")

    def aim_target(self):
        with self._lock:
            vision, at, node1 = self._vision, self._vision_at, dict(self._node1)
        if not vision or at is None or self.clock() - at > self.c["person_max_age_s"]:
            return None
        persons = vision.get("persons") or []
        if not persons:
            return None
        p = persons[0]  # vision.py sorts nearest first
        pan = node1.get("pan_deg", 0.0) + float(p.get("pan_err_deg") or 0.0)
        tilt = node1.get("tilt_deg", 0.0) + float(p.get("tilt_err_deg") or 0.0)
        return (round(max(-PAN_LIMIT, min(PAN_LIMIT, pan)), 1), round(max(-TILT_LIMIT, min(TILT_LIMIT, tilt)), 1))

    def explain_dust(self):
        with self._lock:
            cause, at = self._cause, self._cause_at
        if cause and at is not None and self.clock() - at <= self.c["cause_fresh_s"]:
            return self._say_cause(cause)
        self._cause_event.clear()
        self.mqtt.publish(TOPIC_CAUSE_REQUEST, {"ts": now_ts(), "reason": "voice"})
        self.mqtt.publish(TOPIC_VISION_CMD, {"cmd": "survey", "reason": "voice"})
        self.say("why_wait")
        if self._cause_event.wait(self.c["cause_wait_s"]):
            with self._lock:
                cause = self._cause
            return self._say_cause(cause)
        return self.say("why_none")

    def _say_cause(self, cause):
        advice = (cause or {}).get("advice_th")
        if not advice:
            return self.say("why_none")
        return self.say_text(advice)

    # --- speaking ---

    def say(self, key):
        return self.say_text(REPLIES[key])

    def say_text(self, text):
        if self.text_out is not None:
            self.text_out(text)
            return text
        if self.night:
            log.info("night, not spoken: %s", text)
            return text
        try:
            path = self.tts.synthesize(text)
        except CloudError as e:
            log.warning("cannot speak %r: %s", text, e)
            return text
        self.player.play(path)
        return text


def prepare(tts, extra=()):
    """Make the WAV for every fixed reply and dust-cause advice (run once while online)."""
    from ai.cause_rules import ADVICE_TH
    texts = list(REPLIES.values()) + list(ADVICE_TH.values()) + list(extra)
    made = 0
    for text in texts:
        if tts.cached(text):
            continue
        tts.synthesize(text)
        made += 1
    return made, len(texts)


class _PrintMqtt(object):
    """--chat without --live: show what would go out instead of sending it."""

    def subscribe(self, topic, handler, qos=1):
        pass

    def publish(self, topic, msg, qos=1, retain=False):
        if topic == TOPIC_GW_VOICE:
            return True
        shown = dict((k, v) for k, v in msg.items() if k not in ("seq", "ts"))
        print("   [MQTT %s] %s" % (topic, json.dumps(shown, ensure_ascii=False)))
        return True


def chat(cfg, llm, live=False, read=input, write=print):
    """Type a command, get the unit's answer, like talking to it. Ctrl+D or "exit" to stop."""
    log.setLevel(logging.WARNING)  # keep the conversation readable; warnings (e.g. Claude errors) still show
    if live:
        from common.mqtt_client import MqttClient
        mqtt = MqttClient("voice_chat", cfg)
        mqtt.start(wait=True)
    else:
        mqtt = _PrintMqtt()
        cfg = dict(cfg)
        cfg["voice"] = dict(cfg.get("voice") or {}, cause_wait_s=1)
    voice = Voice(cfg, mqtt, None, None, llm, None, text_out=lambda t: write("เครื่อง: " + t))
    if live:
        for topic, handler in ((TOPIC_NODE1_TELEMETRY, voice.on_telemetry), (TOPIC_GW_VISION, voice.on_vision),
                               (TOPIC_GW_CAUSE, voice.on_cause), (TOPIC_GW_STATE, voice.on_state)):
            mqtt.subscribe(topic, handler, qos=0)
    write("พิมพ์คำสั่งแทนการพูด เช่น หันมาทางนี้ / ฟอกแรงขึ้น / เบาลง / ทำไมฝุ่นเยอะ / หยุด  (exit = ออก)")
    write("โหมด: %s, Claude: %s" % ("ส่งคำสั่งจริงทาง MQTT" if live else "แค่แสดงคำสั่ง ไม่ส่งจริง",
                                    "เปิด" if llm.api_key else "ปิด (ไม่มี AI_API_KEY ใช้แค่คำสำคัญ)"))
    while True:
        try:
            text = read("คุณ: ").strip()
        except (EOFError, KeyboardInterrupt):
            write("")
            break
        if text.lower() in ("exit", "quit", "ออก"):
            break
        if not text:
            continue
        r = voice.handle_text(text)
        write("   (%s, %s)" % (r.get("intent"), r.get("source")))
    if live:
        mqtt.stop()


def main():
    import argparse

    import requests

    from common.config import load_config, secret, writable_dir
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    ap = argparse.ArgumentParser(description="Voice commands (AIR-20)")
    ap.add_argument("--prepare", action="store_true", help="make the offline reply WAVs, then exit")
    ap.add_argument("--say", metavar="TEXT", help="speak one sentence on the speaker, then exit")
    ap.add_argument("--wav", metavar="FILE", help="run one 16 kHz mono WAV through STT + intent (no MQTT action)")
    ap.add_argument("--chat", action="store_true",
                    help="type commands instead of speaking; replies are printed (no microphone, STT or speaker)")
    ap.add_argument("--live", action="store_true",
                    help="with --chat: really send the commands on MQTT (default: only print them)")
    args = ap.parse_args()

    cfg = load_config()
    setup_logging("voice", cfg)
    c = dict(DEFAULTS)
    c.update(cfg.get("voice") or {})
    data = writable_dir(cfg.get("data_dir", "/var/lib/airraikordee"), "data")
    cache = os.path.join(data, "tts_cache")
    session = requests.Session()
    stt_key = secret("STT_API_KEY") or secret("GOOGLE_API_KEY")
    tts_key = secret("TTS_API_KEY") or stt_key
    stt = GoogleStt(stt_key, session, c)
    tts = GoogleTts(tts_key, session, c, cache)
    llm = ClaudeIntent(secret("AI_API_KEY") or secret("ANTHROPIC_API_KEY"), session, c)
    player = Player(c, os.path.join(data, "voice_tmp"))

    if args.prepare:
        made, total = prepare(tts)
        print("made %d new reply WAVs, %d in the cache at %s" % (made, total, cache))
        return
    if args.say:
        player.play(tts.synthesize(args.say))
        return
    if args.chat:
        chat(cfg, llm, live=args.live)
        return
    if args.wav:
        r = wave.open(args.wav, "rb")
        pcm, rate = r.readframes(r.getnframes()), r.getframerate()
        r.close()
        text = stt.transcribe(pcm, rate)
        intent = keyword_intent(text)
        reply = None
        if intent is None and text:
            intent, reply = llm.classify(text)
        print(json.dumps({"text": text, "intent": intent, "llm_reply": reply}, ensure_ascii=False))
        return

    for name, key in (("STT_API_KEY", stt_key), ("TTS_API_KEY", tts_key), ("AI_API_KEY", llm.api_key)):
        if not key:
            log.error("%s not set in .env", name)
    client = MqttClient("voice", cfg)
    client.start(wait=False)
    voice = Voice(cfg, client, stt, tts, llm, player)
    voice.start()
    log.info("voice ready; reply cache %s", cache)
    try:
        run_until_signal(lambda: log.info("stats %s", voice.stats), tick_s=60)
    finally:
        voice.stop()
        client.stop()


if __name__ == "__main__":
    main()
