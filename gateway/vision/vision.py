"""Camera: person detection for aiming (AIR-16) and the survey with snapshots (AIR-17).

- Reads the USB webcam (640x480, ~10 fps) and runs jetson-inference detectNet (SSD-Mobilenet-v2, TensorRT) on
  the GPU. Each person's box centre becomes a pan/tilt error in degrees from the camera field of view, and a
  rough distance from the box height. Publishes ab/gw/vision 5 times a second:
    {ts, persons: [{pan_err_deg, tilt_err_deg, dist_est_m, conf}], person_present, surveying}
  persons are nearest first; pan_err > 0 = person is to the right, tilt_err > 0 = above the centre.
- Snapshots: {"cmd": "snapshot", "tag": ...} on ab/gw/vision/cmd saves the next frame to the snapshot folder
  (last 200 kept) and answers on ab/gw/vision/snapshot {ts, path, tag, person_present}.
- Survey: {"cmd": "survey"} on ab/gw/vision/cmd, a dust spike from cause_rules, or every survey_every_s turns the
  head through 8 directions (survey_step commands to Node 1), takes a snapshot at each, turns back, then
  publishes ab/gw/vision/survey {ts, snapshots: [...], reason} and asks cause_rules for a fresh result.
  No survey in night mode. Snapshots never leave the Jetson (cause_cloud blurs faces before any upload).

Run on the Jetson from gateway/:  python3 -m vision.vision
"""
import logging
import math
import os
import threading
import time

from common.protocol import (TOPIC_GW_CAUSE, TOPIC_GW_STATE, TOPIC_GW_VISION, TOPIC_NODE1_ACK, TOPIC_NODE1_CMD,
                             TOPIC_NODE1_TELEMETRY, SeqCounter, now_ts)

log = logging.getLogger("vision")

TOPIC_VISION_CMD = "ab/gw/vision/cmd"
TOPIC_VISION_SNAPSHOT = "ab/gw/vision/snapshot"
TOPIC_VISION_SURVEY = "ab/gw/vision/survey"
TOPIC_CAUSE_REQUEST = "ab/gw/cause/request"
PERSON_CLASS = 1  # "person" in ssd_coco_labels.txt
SURVEY_ANGLES = (-135, -90, -45, 0, 45, 90, 135, 178)  # one sweep, inside the ±178° soft limit
DEFAULTS = {
    "device": "/dev/video0", "width": 640, "height": 480, "fps": 10, "hfov_deg": 60, "vfov_deg": 45,
    "publish_hz": 5, "model": "ssd-mobilenet-v2", "threshold": 0.5, "person_height_m": 1.7,
    "snapshot_keep": 200, "survey_every_s": 1800, "survey_step_timeout_s": 20, "survey_settle_s": 1.0,
    "camera_retry_s": 5,
}


def box_to_angles(cx, cy, width, height, hfov_deg, vfov_deg):
    """Pixel position -> (pan_err_deg, tilt_err_deg) with a pinhole model (exact at the edges, not linear)."""
    nx = (cx - width / 2.0) / (width / 2.0)
    ny = (height / 2.0 - cy) / (height / 2.0)
    pan = math.degrees(math.atan(nx * math.tan(math.radians(hfov_deg) / 2.0)))
    tilt = math.degrees(math.atan(ny * math.tan(math.radians(vfov_deg) / 2.0)))
    return round(pan, 1), round(tilt, 1)


def estimate_distance(box_h, height, vfov_deg, person_height_m):
    """Distance in metres from the box height; rough, and too far when the person is cut off by the frame."""
    if box_h <= 0:
        return None
    focal_px = (height / 2.0) / math.tan(math.radians(vfov_deg) / 2.0)
    return round(person_height_m * focal_px / box_h, 2)


def persons_from_boxes(boxes, c):
    """boxes: [(cx, cy, w, h, conf)] -> protocol persons, nearest first."""
    persons = []
    for cx, cy, w, h, conf in boxes:
        pan, tilt = box_to_angles(cx, cy, c["width"], c["height"], c["hfov_deg"], c["vfov_deg"])
        persons.append({"pan_err_deg": pan, "tilt_err_deg": tilt, "conf": round(conf, 2),
                        "dist_est_m": estimate_distance(h, c["height"], c["vfov_deg"], c["person_height_m"])})
    persons.sort(key=lambda p: p["dist_est_m"] if p["dist_est_m"] is not None else 1e9)
    return persons


class JetsonCamera(object):
    """USB webcam + detectNet on the Jetson GPU. Only imported on the Jetson.

    The network loads once (≈15 s, GPU memory); only the video source is reopened after the camera is unplugged.
    """

    def __init__(self, c):
        try:
            import jetson_inference as ji
            import jetson_utils as ju
        except ImportError:
            import jetson.inference as ji
            import jetson.utils as ju
        self._ju = ju
        self.c = c
        self.net = ji.detectNet(c["model"], threshold=c["threshold"])
        self.source = None

    def open(self):
        c = self.c
        self.source = self._ju.videoSource(c["device"], argv=["--input-width=%d" % c["width"],
                                                              "--input-height=%d" % c["height"],
                                                              "--input-rate=%d" % c["fps"]])

    def read(self):
        """Returns (frame, boxes) or (None, []) on a capture timeout."""
        frame = self.source.Capture(timeout=1000)
        if frame is None:
            return None, []
        boxes = [(d.Center[0], d.Center[1], d.Width, d.Height, d.Confidence)
                 for d in self.net.Detect(frame, overlay="none") if d.ClassID == PERSON_CLASS]
        return frame, boxes

    def save(self, frame, path):
        self._ju.cudaDeviceSynchronize()
        self._ju.saveImage(path, frame)

    def close(self):
        try:
            self.source.Close()
        except Exception:
            pass
        self.source = None


class Vision(object):
    def __init__(self, cfg, mqtt_client, open_camera=None, snapshot_dir=None, clock=time.time, sleep=time.sleep):
        c = dict(DEFAULTS)
        c.update(cfg.get("vision") or {})
        self.c = c
        self.mqtt = mqtt_client
        self._open_camera = open_camera or (lambda: JetsonCamera(c))
        self.snapshot_dir = snapshot_dir
        self.clock = clock
        self.sleep = sleep
        self._stop = threading.Event()
        self._threads = []
        self._lock = threading.Lock()
        self._persons = []
        self._last_frame_ts = None
        self._snapshot_requests = []   # [(tag, threading.Event, result dict)]
        self._seq = SeqCounter()
        self._acks = {}
        self._pan_deg = 0.0
        self._mode = None
        self.surveying = threading.Event()
        self._last_survey = clock()

    def start(self):
        self.mqtt.subscribe(TOPIC_VISION_CMD, self.on_cmd)
        self.mqtt.subscribe(TOPIC_NODE1_ACK, self.on_ack)
        self.mqtt.subscribe(TOPIC_NODE1_TELEMETRY, self.on_telemetry)
        self.mqtt.subscribe(TOPIC_GW_STATE, self.on_state)
        self.mqtt.subscribe(TOPIC_GW_CAUSE, self.on_cause)
        for target in (self._camera_loop, self._publish_loop, self._schedule_loop):
            t = threading.Thread(target=target, name=target.__name__, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(3.0)

    # --- camera ------------------------------------------------------------------------------------

    def _camera_loop(self):
        camera = None
        while camera is None and not self._stop.is_set():
            try:
                camera = self._open_camera()  # loads the network once
            except Exception as e:
                log.error("cannot load the detector: %s (retrying in 30 s)", e)
                self._stop.wait(30)
        logged = False
        while not self._stop.is_set():
            try:
                camera.open()
            except Exception as e:
                if not logged:
                    log.warning("camera %s not available: %s (retrying every %ds)",
                                self.c["device"], e, self.c["camera_retry_s"])
                    logged = True
                self._stop.wait(self.c["camera_retry_s"])
                continue
            logged = False
            log.info("camera %s open", self.c["device"])
            try:
                while not self._stop.is_set():
                    self.process(camera)
            except Exception as e:
                log.warning("camera error: %s", e)
            finally:
                camera.close()
                with self._lock:
                    self._persons = []
            self._stop.wait(self.c["camera_retry_s"])

    def process(self, camera):
        """Read one frame, update the person list, and serve waiting snapshot requests."""
        frame, boxes = camera.read()
        if frame is None:
            return
        persons = persons_from_boxes(boxes, self.c)
        with self._lock:
            self._persons = persons
            self._last_frame_ts = self.clock()
            requests, self._snapshot_requests = self._snapshot_requests, []
        for tag, done, result in requests:
            result.update(self._save_snapshot(camera, frame, tag, bool(persons)))
            done.set()

    def _save_snapshot(self, camera, frame, tag, person_present):
        ts = now_ts()
        name = "%d_%s.jpg" % (int(ts * 1000), "".join(ch for ch in str(tag) if ch.isalnum() or ch in "-_")[:40])
        path = os.path.join(self.snapshot_dir, name)
        camera.save(frame, path)
        self._prune()
        msg = {"ts": ts, "path": path, "tag": tag, "person_present": person_present}
        self.mqtt.publish(TOPIC_VISION_SNAPSHOT, msg)
        return msg

    def _prune(self):
        files = sorted(f for f in os.listdir(self.snapshot_dir) if f.endswith(".jpg"))
        for name in files[:max(0, len(files) - int(self.c["snapshot_keep"]))]:
            try:
                os.remove(os.path.join(self.snapshot_dir, name))
            except OSError:
                pass

    def snapshot(self, tag, timeout=3.0):
        """Ask the camera thread for the next frame. Returns the snapshot message, or None on timeout."""
        done, result = threading.Event(), {}
        with self._lock:
            self._snapshot_requests.append((tag, done, result))
        return result if done.wait(timeout) else None

    # --- publishing --------------------------------------------------------------------------------

    def current(self):
        with self._lock:
            fresh = self._last_frame_ts is not None and self.clock() - self._last_frame_ts < 2.0
            persons = list(self._persons) if fresh else []
        return {"ts": now_ts(), "persons": persons, "person_present": bool(persons),
                "surveying": self.surveying.is_set()}

    def _publish_loop(self):
        period = 1.0 / float(self.c["publish_hz"])
        while not self._stop.wait(period):
            if self._last_frame_ts is not None:
                self.mqtt.publish(TOPIC_GW_VISION, self.current(), qos=0)

    # --- survey ------------------------------------------------------------------------------------

    def on_cmd(self, topic, msg):
        cmd = msg.get("cmd")
        if cmd == "snapshot":
            threading.Thread(target=self.snapshot, args=(msg.get("tag", "request"),), daemon=True).start()
        elif cmd == "survey":
            self.start_survey(msg.get("reason", "request"))

    def on_ack(self, topic, msg):
        event = self._acks.get(msg.get("ack_seq"))
        if event is not None:
            event.set()

    def on_telemetry(self, topic, msg):
        if isinstance(msg.get("pan_deg"), (int, float)):
            self._pan_deg = float(msg["pan_deg"])

    def on_state(self, topic, msg):
        self._mode = msg.get("mode")

    def on_cause(self, topic, msg):
        if msg.get("source") == "rules" and msg.get("reason") == "spike":
            self.start_survey("spike")

    def _schedule_loop(self):
        while not self._stop.wait(5.0):
            if self.clock() - self._last_survey >= self.c["survey_every_s"]:
                self.start_survey("schedule")

    def start_survey(self, reason):
        """Run a survey in the background. Returns False if one is running or it is night."""
        if self._mode == "night":
            log.info("survey (%s) skipped: night mode", reason)
            return False
        if self.surveying.is_set():
            return False
        self.surveying.set()
        self._last_survey = self.clock()
        threading.Thread(target=self.run_survey, args=(reason,), daemon=True).start()
        return True

    def _turn(self, pan_deg):
        """survey_step to Node 1 (through uart_bridge); wait for its ACK, i.e. the head reached the angle."""
        seq = self._seq.next()
        acked = threading.Event()
        self._acks[seq] = acked
        try:
            self.mqtt.publish(TOPIC_NODE1_CMD, {"seq": seq, "ts": now_ts(), "cmd": "survey_step",
                                                "pan_deg": pan_deg})
            return acked.wait(self.c["survey_step_timeout_s"])
        finally:
            self._acks.pop(seq, None)

    def run_survey(self, reason):
        start_pan = self._pan_deg
        shots = []
        try:
            log.info("survey started (%s)", reason)
            for angle in SURVEY_ANGLES:
                if self._stop.is_set():
                    break
                if not self._turn(angle):
                    log.warning("survey: no ack for pan %d", angle)
                self.sleep(self.c["survey_settle_s"])
                snap = self.snapshot("survey%+d" % angle)
                if snap:
                    shots.append(snap["path"])
            self._turn(start_pan)
        finally:
            self.surveying.clear()
        msg = {"ts": now_ts(), "snapshots": shots, "reason": reason}
        log.info("survey done: %d snapshots", len(shots))
        self.mqtt.publish(TOPIC_VISION_SURVEY, msg)
        self.mqtt.publish(TOPIC_CAUSE_REQUEST, {"ts": now_ts(), "reason": "survey"})
        return msg


def main():
    from common.config import load_config, writable_dir
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    cfg = load_config()
    setup_logging("vision", cfg)
    snapshots = writable_dir(os.path.join(cfg.get("data_dir", "/var/lib/airraikordee"), "snapshots"), "snapshots")
    client = MqttClient("vision", cfg)
    client.start(wait=False)
    vision = Vision(cfg, client, snapshot_dir=snapshots)
    vision.start()
    log.info("vision running, snapshots in %s", snapshots)
    try:
        run_until_signal()
    finally:
        vision.stop()
        client.stop()


if __name__ == "__main__":
    main()
