"""Cloud AI second opinion on the dust cause (AIR-19).

When cause_rules is unsure (confidence < cloud_min_confidence) and a survey took snapshots in the last
`snapshot_max_age_s`, the survey photos are sent with the rule features to Claude, at most once per
cloud_min_interval_s. Faces are blurred on the Jetson first; if blurring is not possible nothing is uploaded.
The reply is forced to JSON {cause, confidence, advice}. On any failure (offline, timeout, refusal, bad JSON)
the rule result stays. Publishes ab/gw/cause with source "cloud" and logs both answers to SQLite
(cause_log.db) for the accuracy table in the report.

The Jetson runs Python 3.6, which the Anthropic Python SDK does not support, so this calls the Messages API
over HTTPS with requests. API key: AI_API_KEY (or ANTHROPIC_API_KEY) in .env.

Run on the Jetson from gateway/:  python3 -m ai.cause_cloud
"""
import base64
import json
import logging
import os
import sqlite3
import threading
import time

from ai.cause_rules import ADVICE_TH, CAUSES
from common.protocol import TOPIC_GW_CAUSE, now_ts

log = logging.getLogger("cause_cloud")

TOPIC_VISION_SURVEY = "ab/gw/vision/survey"
API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-opus-5-5"
HAAR_DIRS = ("/usr/share/opencv4/haarcascades", "/usr/share/opencv/haarcascades",
             "/usr/local/share/opencv4/haarcascades")
DEFAULTS = {"cloud_min_confidence": 0.8, "cloud_min_interval_s": 600, "cloud_timeout_s": 30,
            "snapshot_max_age_s": 900, "max_images": 8, "image_max_width": 640}
SCHEMA = {
    "type": "object",
    "properties": {
        "cause": {"type": "string", "enum": list(CAUSES)},
        "confidence": {"type": "number"},
        "advice": {"type": "string"},
    },
    "required": ["cause", "confidence", "advice"],
    "additionalProperties": False,
}
PROMPT = """You help a home air purifier explain where indoor PM2.5 dust comes from.
The photos are the room seen from the purifier, taken in 8 directions just now. Faces are blurred.
Sensor readings (PM2.5 in µg/m³, humidity in %%): %s
The purifier's simple rules guessed "%s" with confidence %.2f.

Pick the most likely cause from: window_open (an open window or door to outside), cooking, incense_candle
(incense, candles, mosquito coils), smoking, outdoor_infiltration (dust leaking in from outside while windows
are closed), unknown. Only choose a cause the photos or readings support; otherwise answer unknown.
confidence is 0 to 1. advice is one short, friendly sentence in Thai that tells the user what to do; it is read
aloud by the purifier."""


class FaceBlurrer(object):
    """OpenCV Haar face detection (frontal + profile) and a heavy blur over every face found."""

    def __init__(self, cv2=None):
        if cv2 is None:
            import cv2
        self.cv2 = cv2
        dirs = list(HAAR_DIRS)
        if getattr(cv2, "data", None) is not None:
            dirs.insert(0, cv2.data.haarcascades)
        self.cascades = []
        for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
            for d in dirs:
                path = os.path.join(d, name)
                if os.path.isfile(path):
                    self.cascades.append(cv2.CascadeClassifier(path))
                    break
        if not self.cascades:
            raise RuntimeError("no Haar face cascade found; refusing to upload unblurred images")

    def blur(self, image):
        """Returns (blurred image, number of faces). Boxes are padded so hair and ears are covered too."""
        cv2 = self.cv2
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        faces = []
        for cascade in self.cascades:
            faces.extend(cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(24, 24)))
        h, w = image.shape[:2]
        for (x, y, fw, fh) in faces:
            pad_w, pad_h = int(fw * 0.3), int(fh * 0.3)
            x0, y0 = max(0, x - pad_w), max(0, y - pad_h)
            x1, y1 = min(w, x + fw + pad_w), min(h, y + fh + pad_h)
            region = image[y0:y1, x0:x1]
            k = max(31, (min(region.shape[:2]) // 2) | 1)
            image[y0:y1, x0:x1] = cv2.GaussianBlur(region, (k, k), 0)
        return image, len(faces)


def encode_jpeg(cv2, image, max_width):
    h, w = image.shape[:2]
    if w > max_width:
        image = cv2.resize(image, (max_width, int(h * max_width / float(w))))
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        raise ValueError("jpeg encode failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def build_request(images_b64, rule_msg):
    """Messages API body: photos + readings, JSON-only answer, server-side fallback on a refusal."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}
               for data in images_b64]
    features = json.dumps(rule_msg.get("features") or {}, ensure_ascii=False)
    content.append({"type": "text", "text": PROMPT % (features, rule_msg.get("cause", "unknown"),
                                                       float(rule_msg.get("confidence") or 0))})
    return {
        "model": MODEL,
        "max_tokens": 4000,
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "fallbacks": "default",
        "messages": [{"role": "user", "content": content}],
    }


def parse_response(body):
    """Messages API JSON -> {cause, confidence, advice} or None if it cannot be used."""
    if body.get("stop_reason") == "refusal":
        log.warning("cloud AI declined: %s", body.get("stop_details"))
        return None
    text = next((b.get("text") for b in body.get("content", []) if b.get("type") == "text"), None)
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if data.get("cause") not in CAUSES or not isinstance(data.get("confidence"), (int, float)):
        return None
    data["confidence"] = round(max(0.0, min(1.0, float(data["confidence"]))), 2)
    return data


class CauseLog(object):
    """SQLite record of every rules and cloud answer (accuracy table for the report)."""

    def __init__(self, path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.execute("CREATE TABLE IF NOT EXISTS answers (ts REAL, id TEXT, source TEXT, cause TEXT, "
                         "confidence REAL, rules_cause TEXT, detail TEXT)")
        self._db.commit()

    def add(self, msg, rules_cause=None, detail=None):
        with self._lock:
            self._db.execute("INSERT INTO answers VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (msg.get("ts"), msg.get("id"), msg.get("source"), msg.get("cause"),
                              msg.get("confidence"), rules_cause, json.dumps(detail or {}, ensure_ascii=False)))
            self._db.commit()

    def rows(self):
        with self._lock:
            return self._db.execute("SELECT source, cause, confidence, rules_cause FROM answers").fetchall()


class CauseCloud(object):
    def __init__(self, cfg, mqtt_client, api_key, cause_log, session=None, blurrer=None, read_image=None,
                 clock=time.time):
        c = dict(DEFAULTS)
        c.update(cfg.get("cause") or {})
        self.c = c
        self.mqtt = mqtt_client
        self.api_key = api_key
        self.log = cause_log
        self.session = session
        self._blurrer = blurrer
        self._read_image = read_image
        self.clock = clock
        self._survey = None            # (time, [paths])
        self._last_call = -1e12
        self._busy = threading.Lock()

    def start(self):
        self.mqtt.subscribe(TOPIC_GW_CAUSE, self.on_cause)
        self.mqtt.subscribe(TOPIC_VISION_SURVEY, self.on_survey)

    def on_survey(self, topic, msg):
        if msg.get("snapshots"):
            self._survey = (self.clock(), list(msg["snapshots"]))

    def on_cause(self, topic, msg):
        if msg.get("source") != "rules":
            return
        self.log.add(msg, detail=msg.get("features"))
        reason = self.should_ask(msg)
        if reason:
            log.info("not asking the cloud AI: %s", reason)
            return
        threading.Thread(target=self.ask, args=(msg,), daemon=True).start()

    def should_ask(self, msg):
        """None if the cloud should be asked, else the reason it is not."""
        if float(msg.get("confidence") or 0) >= self.c["cloud_min_confidence"]:
            return "rules are confident"
        if not self.api_key:
            return "AI_API_KEY not set"
        if self._survey is None or self.clock() - self._survey[0] > self.c["snapshot_max_age_s"]:
            return "no recent survey snapshots"
        if self.clock() - self._last_call < self.c["cloud_min_interval_s"]:
            return "asked less than %ds ago" % self.c["cloud_min_interval_s"]
        if self._busy.locked():
            return "already asking"
        return None

    def _images(self):
        import cv2
        blurrer = self._blurrer or FaceBlurrer(cv2)
        read = self._read_image or cv2.imread
        images, faces = [], 0
        for path in self._survey[1][:int(self.c["max_images"])]:
            image = read(path)
            if image is None:
                continue
            image, n = blurrer.blur(image)
            faces += n
            images.append(encode_jpeg(cv2, image, int(self.c["image_max_width"])))
        return images, faces

    def ask(self, rule_msg):
        """Blur, send, publish. Returns the published cloud message or None (the rules answer stays)."""
        with self._busy:
            self._last_call = self.clock()
            try:
                images, faces = self._images()
            except Exception as e:
                log.error("not uploading: %s", e)
                return None
            if not images:
                return None
            if self.session is None:
                import requests
                self.session = requests.Session()
            started = time.time()
            try:
                r = self.session.post(API_URL, json=build_request(images, rule_msg), timeout=self.c["cloud_timeout_s"],
                                      headers={"x-api-key": self.api_key, "anthropic-version": "2026-10-21",
                                               "anthropic-beta": "server-side-fallback-2026-07-01",
                                               "content-type": "application/json"})
                body = r.json()
            except Exception as e:
                log.warning("cloud AI unavailable, keeping the rules answer: %s", e)
                return None
            if r.status_code != 200:
                log.warning("cloud AI error %s: %s", r.status_code, str(body)[:300])
                return None
            answer = parse_response(body)
            if answer is None:
                log.warning("cloud AI answer unusable, keeping the rules answer")
                return None
            ts = now_ts()
            msg = {"ts": int(ts), "id": "c%d" % int(ts), "cause": answer["cause"],
                   "confidence": answer["confidence"], "source": "cloud",
                   "advice_th": answer["advice"] or ADVICE_TH[answer["cause"]],
                   "rules_cause": rule_msg.get("cause"), "rules_confidence": rule_msg.get("confidence")}
            self.log.add(msg, rules_cause=rule_msg.get("cause"),
                         detail={"images": len(images), "faces_blurred": faces,
                                 "seconds": round(time.time() - started, 1), "usage": body.get("usage")})
            log.info("cloud AI: %s (%.2f), rules said %s; %d images, %d faces blurred",
                     msg["cause"], msg["confidence"], msg["rules_cause"], len(images), faces)
            self.mqtt.publish(TOPIC_GW_CAUSE, msg)
            return msg


def main():
    from common.config import load_config, secret, writable_dir
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    cfg = load_config()
    setup_logging("cause_cloud", cfg)
    db = os.path.join(writable_dir(cfg.get("data_dir", "/var/lib/airraikordee"), "data"), "cause_log.db")
    api_key = secret("AI_API_KEY") or secret("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("AI_API_KEY not set in .env; only the local rules will answer")
    client = MqttClient("cause_cloud", cfg)
    client.start(wait=False)
    cloud = CauseCloud(cfg, client, api_key, CauseLog(db))
    cloud.start()
    log.info("cause_cloud running, log %s", db)
    try:
        run_until_signal()
    finally:
        client.stop()


if __name__ == "__main__":
    main()
