"""Alerts -> Discord (AIR-52, DB-06).

Listens to
  ab/gw/alert   one-off alerts {ts, type, level, title, text} (uart_bridge, health.py, ...)
  ab/gw/state   decision.py's active alerts: an item is sent when its type first appears in `alerts`
and posts each one to a Discord webhook as a coloured embed, at most once per alert type per
`cooldown_min` (15 min, like `config.alert_cooldown_min`). PM alerts get the newest survey photo (at most
`photo_max_age_s` old) with every face blurred on the Jetson first; if blurring is not possible the photo is
left out, never sent as it is. An HTTP 429 waits for Discord's retry_after and tries again; other failures retry
with backoff, and the alert is dropped after `max_attempts` so one bad message cannot block the queue.

The webhook URL is a secret: DISCORD_WEBHOOK_URL in .env (never in Git or in logs; only the last 4 characters
are logged). The dashboard flow in docs/protocol.md (deviceSecrets + config.notify) can replace it later.

Run on the Jetson from gateway/:  python3 -m notify.discord          (service)
                                   python3 -m notify.discord --test   (one test message, then exit)
"""
import json
import logging
import queue
import threading
import time

from common.protocol import TOPIC_GW_ALERT, TOPIC_GW_STATE, TOPIC_GW_UI_CMD, now_ts

log = logging.getLogger("discord")

TOPIC_VISION_SURVEY = "ab/gw/vision/survey"
WEBHOOK_PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")

DEFAULTS = {
    "cooldown_min": 15,
    "photo_for": ["pm_high"],       # alert types that get the latest survey photo
    "photo_max_age_s": 900,
    "photo_max_width": 640,
    "timeout_s": 10,
    "max_attempts": 5,
    "unit_name": "AirRaiKorDee",
}

TITLES = {
    "pm_high": "PM2.5 สูง", "node_offline": "อุปกรณ์ออฟไลน์", "uart_fail": "Node 1 ไม่ตอบคำสั่ง",
    "charge_cut_temp": "หยุดชาร์จเพราะร้อนเกิน", "charge_fault": "ที่ชาร์จขัดข้อง", "battery_low": "แบตเตอรี่ต่ำ",
    "charge_full": "ชาร์จเต็มแล้ว", "filter_clogged": "ไส้กรองอุดตัน", "stop_pressed": "กดปุ่มหยุด",
}
COLOURS = {"info": 0x3498DB, "warn": 0xF1C40F, "crit": 0xE74C3C}


def valid_webhook(url):
    return isinstance(url, str) and url.startswith(WEBHOOK_PREFIXES) and len(url) <= 300


def build_payload(alert, unit_name, with_photo):
    level = alert.get("level") if alert.get("level") in COLOURS else "warn"
    embed = {
        "title": (alert.get("title") or TITLES.get(alert.get("type"), "แจ้งเตือน"))[:256],
        "description": (alert.get("text") or "")[:2000],
        "color": COLOURS[level],
        "footer": {"text": "%s · %s" % (unit_name, alert.get("type") or "alert")},
    }
    ts = alert.get("ts")
    if isinstance(ts, (int, float)):
        embed["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    if with_photo:
        embed["image"] = {"url": "attachment://photo.jpg"}
    # no @everyone / role pings from alert text
    return {"username": unit_name, "embeds": [embed], "allowed_mentions": {"parse": []}}


class Webhook(object):
    """POSTs to the webhook. Returns ("ok" | "retry" | "drop", seconds to wait before a retry)."""

    def __init__(self, url, session, timeout):
        self.url = url
        self.session = session
        self.timeout = timeout

    def post(self, payload, photo=None):
        try:
            if photo is None:
                r = self.session.post(self.url, json=payload, timeout=self.timeout)
            else:
                r = self.session.post(self.url, data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                                      files={"files[0]": ("photo.jpg", photo, "image/jpeg")}, timeout=self.timeout)
        except Exception as e:
            log.warning("Discord unreachable: %s", type(e).__name__)  # the exception text may contain the URL
            return "retry", None
        if r.status_code in (200, 204):
            return "ok", None
        if r.status_code == 429:
            wait = None
            try:
                wait = float(r.json().get("retry_after"))
            except Exception:
                pass
            if wait is None:
                try:
                    wait = float(r.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    wait = 5.0
            log.info("Discord rate limit, waiting %.1f s", wait)
            return "retry", wait
        if r.status_code >= 500:
            return "retry", None
        log.warning("Discord rejected the message: HTTP %s %s", r.status_code, r.text[:200])
        return "drop", None


class DiscordNotifier(object):
    def __init__(self, cfg, mqtt, webhook, photo_reader=None, clock=time.time, sleep=None):
        c = dict(DEFAULTS)
        c.update((cfg.get("notify") or {}).get("discord") or {})
        self.c = c
        self.mqtt = mqtt
        self.webhook = webhook
        self.photo_reader = photo_reader   # path -> blurred JPEG bytes, or None
        self.clock = clock
        self._stop = threading.Event()
        self.sleep = sleep or self._stop.wait
        self._queue = queue.Queue()
        self._last_sent = {}               # type -> time of the last message (cooldown)
        self._state_active = set()
        self._survey = None                # (time, [paths])
        self._lock = threading.Lock()
        self._thread = None
        self.stats = {"queued": 0, "sent": 0, "skipped_cooldown": 0, "dropped": 0, "retries": 0}

    # --- lifecycle ---

    def start(self):
        self.mqtt.subscribe(TOPIC_GW_ALERT, self.on_alert)
        self.mqtt.subscribe(TOPIC_GW_STATE, self.on_state)
        self.mqtt.subscribe(TOPIC_VISION_SURVEY, self.on_survey)
        self.mqtt.subscribe(TOPIC_GW_UI_CMD, self.on_ui_cmd)
        self._thread = threading.Thread(target=self._loop, name="discord", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(5.0)

    # --- inputs ---

    def on_alert(self, topic, msg):
        alert = {"ts": msg.get("ts") or now_ts(), "type": msg.get("type") or "alert",
                 "level": msg.get("level") or msg.get("severity") or "warn",
                 "title": msg.get("title"), "text": msg.get("text") or msg.get("msg") or ""}
        self.submit(alert)

    def on_state(self, topic, msg):
        active = set()
        for item in msg.get("alerts") or []:
            alert = item if isinstance(item, dict) else {"type": item}
            t = alert.get("type")
            if not t:
                continue
            active.add(t)
            if t not in self._state_active:
                self.submit({"ts": alert.get("since") or msg.get("ts") or now_ts(), "type": t,
                             "level": alert.get("level") or "warn", "title": alert.get("title"),
                             "text": alert.get("text") or ""})
        self._state_active = active

    def on_ui_cmd(self, topic, msg):
        """"Send test" button on the dashboard: one message now, ignoring the cooldown."""
        if msg.get("cmd") == "test_notify" and msg.get("channel", "discord") == "discord":
            self.submit({"ts": msg.get("ts") or now_ts(), "type": "test", "level": "info",
                         "title": "ทดสอบการแจ้งเตือน", "text": "ส่งจากปุ่มทดสอบบนแดชบอร์ด"}, force=True)

    def on_survey(self, topic, msg):
        paths = [p if isinstance(p, str) else (p or {}).get("path") for p in msg.get("snapshots") or []]
        paths = [p for p in paths if p]
        if paths:
            with self._lock:
                self._survey = (self.clock(), paths)

    # --- sending ---

    def submit(self, alert, force=False):
        """Queue an alert unless the same type was sent within the cooldown. Returns True if queued."""
        t = alert.get("type")
        now = self.clock()
        with self._lock:
            last = self._last_sent.get(t)
            if not force and last is not None and now - last < self.c["cooldown_min"] * 60:
                self.stats["skipped_cooldown"] += 1
                log.info("%s not sent: last one %.0f min ago", t, (now - last) / 60.0)
                return False
            self._last_sent[t] = now  # reserve the slot now, so a burst of the same alert sends one message
        self.stats["queued"] += 1
        self._queue.put(alert)
        return True

    def _loop(self):
        while not self._stop.is_set():
            alert = self._queue.get()
            if alert is None:
                return
            self.deliver(alert)

    def photo_for(self, alert):
        if alert.get("type") not in self.c["photo_for"] or self.photo_reader is None:
            return None
        with self._lock:
            survey = self._survey
        if not survey or self.clock() - survey[0] > self.c["photo_max_age_s"]:
            return None
        for path in survey[1]:
            try:
                photo = self.photo_reader(path)
            except Exception as e:
                log.warning("no photo (%s)", e)
                return None
            if photo:
                return photo
        return None

    def deliver(self, alert):
        photo = self.photo_for(alert)
        payload = build_payload(alert, self.c["unit_name"], photo is not None)
        delay = 2.0
        for attempt in range(int(self.c["max_attempts"])):
            if attempt:
                self.stats["retries"] += 1
            result, wait = self.webhook.post(payload, photo)
            if result == "ok":
                self.stats["sent"] += 1
                log.info("sent %s to Discord%s", alert.get("type"), " with photo" if photo else "")
                return True
            if result == "drop":
                break
            if wait is not None:
                self.sleep(wait)  # Discord said how long (HTTP 429)
            else:
                self.sleep(delay)
                delay = min(delay * 2, 60.0)
            if self._stop.is_set():
                break
        self.stats["dropped"] += 1
        with self._lock:
            self._last_sent.pop(alert.get("type"), None)  # not delivered: let the next one through
        log.warning("gave up sending %s to Discord", alert.get("type"))
        return False


def blurred_photo_reader(max_width):
    """path -> JPEG bytes with faces blurred. Raises if OpenCV or the face model is missing (no photo then)."""
    import cv2

    from ai.cause_cloud import FaceBlurrer
    blurrer = FaceBlurrer(cv2)

    def read(path):
        image = cv2.imread(path)
        if image is None:
            return None
        image, _ = blurrer.blur(image)
        h, w = image.shape[:2]
        if w > max_width:
            image = cv2.resize(image, (max_width, int(h * max_width / float(w))))
        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return buf.tobytes() if ok else None
    return read


def main():
    import argparse

    import requests

    from common.config import load_config, secret
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    ap = argparse.ArgumentParser(description="Alerts -> Discord (AIR-52)")
    ap.add_argument("--test", action="store_true", help="send one test message and exit")
    args = ap.parse_args()

    cfg = load_config()
    setup_logging("discord", cfg)
    c = dict(DEFAULTS)
    c.update((cfg.get("notify") or {}).get("discord") or {})
    url = secret("DISCORD_WEBHOOK_URL")
    if not valid_webhook(url):
        log.error("DISCORD_WEBHOOK_URL in .env is missing or is not a Discord webhook URL; nothing will be sent")
        if args.test:
            raise SystemExit(1)
    else:
        log.info("Discord webhook ...%s", url[-4:])
    webhook = Webhook(url, requests.Session(), float(c["timeout_s"]))

    if args.test:
        result, _ = webhook.post(build_payload({"ts": now_ts(), "type": "test", "level": "info",
                                                "title": "ทดสอบการแจ้งเตือน",
                                                "text": "ถ้าเห็นข้อความนี้ Discord ใช้ได้แล้ว"}, c["unit_name"], False))
        print("Discord:", result)
        raise SystemExit(0 if result == "ok" else 1)

    try:
        reader = blurred_photo_reader(int(c["photo_max_width"]))
    except Exception as e:
        log.warning("photos off: %s", e)
        reader = None
    client = MqttClient("discord", cfg)
    client.start(wait=False)
    notifier = DiscordNotifier(cfg, client, webhook if valid_webhook(url) else _NoWebhook(), reader)
    notifier.start()
    log.info("discord notifier running, cooldown %s min per alert type", c["cooldown_min"])
    try:
        run_until_signal(lambda: log.info("stats %s", notifier.stats), tick_s=300)
    finally:
        notifier.stop()
        client.stop()


class _NoWebhook(object):
    def post(self, payload, photo=None):
        return "drop", None


if __name__ == "__main__":
    main()
