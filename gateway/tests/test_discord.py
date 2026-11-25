import json

from common.protocol import TOPIC_GW_ALERT, TOPIC_GW_STATE, TOPIC_NODE1_TELEMETRY
from conftest import FakeMqtt
from health.health import Health
from notify.discord import (TOPIC_VISION_SURVEY, DiscordNotifier, Webhook, build_payload, valid_webhook)

URL = "https://discord.com/api/webhooks/123/abcdefgh"


class Clock(object):
    def __init__(self):
        self.t = 1794384000.0

    def __call__(self):
        return self.t


class FakeWebhook(object):
    def __init__(self, results=None):
        self.results = list(results or [])
        self.posts = []

    def post(self, payload, photo=None):
        self.posts.append((payload, photo))
        return self.results.pop(0) if self.results else ("ok", None)


def make(results=None, photo_reader=None):
    clock = Clock()
    waits = []
    hook = FakeWebhook(results)
    n = DiscordNotifier({}, FakeMqtt(), hook, photo_reader, clock=clock, sleep=waits.append)
    return n, hook, clock, waits


def drain(n):
    while not n._queue.empty():
        n.deliver(n._queue.get())


def pm_state(alerts):
    return {"ts": 1794384000, "mode": "idle", "alerts": alerts}


PM = {"type": "pm_high", "level": "warn", "title": "PM2.5 สูง", "text": "PM2.5 68 เกิน 10 นาที", "since": 1794384000}


def test_high_dust_sends_one_message_per_cooldown():
    """AIR-52 done-when: dust stays high -> exactly one Discord message."""
    n, hook, clock, _ = make()
    for _ in range(30):                      # decision.py repeats the state every second
        n.on_state(TOPIC_GW_STATE, pm_state([PM]))
        clock.t += 1
    drain(n)
    assert len(hook.posts) == 1
    embed = hook.posts[0][0]["embeds"][0]
    assert embed["title"] == "PM2.5 สูง" and embed["description"].startswith("PM2.5 68")

    n.on_state(TOPIC_GW_STATE, pm_state([]))  # clears, then comes back within 15 min: still quiet
    clock.t += 300
    n.on_state(TOPIC_GW_STATE, pm_state([PM]))
    drain(n)
    assert len(hook.posts) == 1 and n.stats["skipped_cooldown"] == 1

    n.on_state(TOPIC_GW_STATE, pm_state([]))
    clock.t += 15 * 60
    n.on_state(TOPIC_GW_STATE, pm_state(["pm_high"]))  # the uploader-style string form works too
    drain(n)
    assert len(hook.posts) == 2


def test_cooldown_is_per_type():
    n, hook, clock, _ = make()
    n.on_alert(TOPIC_GW_ALERT, {"type": "uart_fail", "severity": "warn", "msg": "Node 1 ไม่ตอบ"})
    n.on_alert(TOPIC_GW_ALERT, {"type": "node_offline", "level": "crit", "text": "Node 2"})
    n.on_alert(TOPIC_GW_ALERT, {"type": "uart_fail", "severity": "warn", "msg": "again"})
    drain(n)
    assert [p[0]["embeds"][0]["footer"]["text"].split(" · ")[1] for p in hook.posts] == ["uart_fail", "node_offline"]
    assert hook.posts[0][0]["embeds"][0]["description"] == "Node 1 ไม่ตอบ"     # old severity/msg names
    assert hook.posts[1][0]["embeds"][0]["color"] == 0xE74C3C


def test_rate_limit_waits_retry_after_then_sends():
    n, hook, _, waits = make([("retry", 2.5), ("retry", None), ("ok", None)])
    n.on_alert(TOPIC_GW_ALERT, {"type": "charge_fault", "level": "crit", "text": "x"})
    drain(n)
    assert waits == [2.5, 2.0] and len(hook.posts) == 3 and n.stats["sent"] == 1


def test_failed_alert_is_dropped_and_frees_the_cooldown():
    n, hook, _, _ = make([("drop", None)])
    n.on_alert(TOPIC_GW_ALERT, {"type": "battery_low", "text": "x"})
    drain(n)
    assert n.stats["dropped"] == 1
    n.on_alert(TOPIC_GW_ALERT, {"type": "battery_low", "text": "x"})
    drain(n)
    assert n.stats["sent"] == 1


def test_pm_alert_gets_a_fresh_blurred_photo_only():
    read = []

    def reader(path):
        read.append(path)
        return b"\xff\xd8jpeg"

    n, hook, clock, _ = make(photo_reader=reader)
    n.on_survey(TOPIC_VISION_SURVEY, {"snapshots": ["/snap/a.jpg", "/snap/b.jpg"]})
    n.on_state(TOPIC_GW_STATE, pm_state([PM]))
    n.on_alert(TOPIC_GW_ALERT, {"type": "uart_fail", "text": "x"})  # not a photo type
    drain(n)
    assert hook.posts[0][1] == b"\xff\xd8jpeg" and hook.posts[0][0]["embeds"][0]["image"]["url"] == "attachment://photo.jpg"
    assert hook.posts[1][1] is None and read == ["/snap/a.jpg"]

    n, hook, clock, _ = make(photo_reader=reader)
    n.on_survey(TOPIC_VISION_SURVEY, {"snapshots": ["/snap/old.jpg"]})
    clock.t += 3600  # too old
    n.on_state(TOPIC_GW_STATE, pm_state([PM]))
    drain(n)
    assert hook.posts[0][1] is None

    def broken(path):
        raise RuntimeError("no Haar face cascade found")

    n, hook, clock, _ = make(photo_reader=broken)
    n.on_survey(TOPIC_VISION_SURVEY, {"snapshots": ["/snap/a.jpg"]})
    n.on_state(TOPIC_GW_STATE, pm_state([PM]))
    drain(n)
    assert hook.posts[0][1] is None and "image" not in hook.posts[0][0]["embeds"][0]  # never unblurred


def test_payload_blocks_mentions_and_limits_lengths():
    p = build_payload({"type": "pm_high", "level": "nope", "title": "x" * 300, "text": "@everyone " + "y" * 3000,
                       "ts": 1794384000}, "AirRaiKorDee", False)
    e = p["embeds"][0]
    assert p["allowed_mentions"] == {"parse": []} and len(e["title"]) == 256 and len(e["description"]) == 2000
    assert e["timestamp"] == "2026-11-11T08:00:00Z" and e["color"] == 0xF1C40F


def test_webhook_http_results():
    class Resp(object):
        def __init__(self, code, body=None, headers=None):
            self.status_code = code
            self._body = body
            self.headers = headers or {}
            self.text = json.dumps(body or {})

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    class Session(object):
        def __init__(self, replies):
            self.replies = replies
            self.calls = []

        def post(self, url, **kw):
            self.calls.append(kw)
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    s = Session([Resp(204), Resp(429, {"retry_after": 1.5}), Resp(429, None, {"Retry-After": "3"}), Resp(400, {}),
                 Resp(502), IOError("connection to " + URL + " failed")])
    w = Webhook(URL, s, 5)
    assert w.post({"embeds": []}) == ("ok", None)
    assert w.post({}) == ("retry", 1.5)
    assert w.post({}) == ("retry", 3.0)
    assert w.post({}) == ("drop", None)
    assert w.post({}) == ("retry", None)
    assert w.post({}) == ("retry", None)
    w2 = Webhook(URL, Session([Resp(200)]), 5)
    w2.post({"embeds": []}, b"jpeg")
    call = w2.session.calls[0]
    assert "files[0]" in call["files"] and json.loads(call["data"]["payload_json"]) == {"embeds": []}


def test_valid_webhook():
    assert valid_webhook(URL)
    assert not valid_webhook("https://example.com/api/webhooks/1/x") and not valid_webhook("")
    assert not valid_webhook(URL + "x" * 300)


def test_health_publishes_node_offline_once():
    class Probe(object):
        def cpu_temp_c(self):
            return 40.0

        def ram_pct(self):
            return 30.0

        def disk_pct(self):
            return 20.0

    clock = Clock()
    mqtt = FakeMqtt()
    h = Health({"health": {"node_offline_s": 30}}, mqtt, Probe(), None, clock=clock)
    h.step()
    assert mqtt.on(TOPIC_GW_ALERT) == []            # never seen: no alert at boot
    h.on_node1(TOPIC_NODE1_TELEMETRY, {"pm25": 10, "fan_rpm": 0})
    h.step()
    clock.t += 31
    h.step()
    h.step()
    alerts = mqtt.on(TOPIC_GW_ALERT)
    assert len(alerts) == 1 and alerts[0]["type"] == "node_offline" and alerts[0]["level"] == "crit"
    assert "Node 1" in alerts[0]["text"]


def test_test_button_ignores_the_cooldown():
    n, hook, _, _ = make()
    n.on_ui_cmd(None, {"cmd": "test_notify", "channel": "discord"})
    n.on_ui_cmd(None, {"cmd": "test_notify"})
    n.on_ui_cmd(None, {"cmd": "test_notify", "channel": "line"})
    drain(n)
    assert len(hook.posts) == 2 and hook.posts[0][0]["embeds"][0]["title"] == "ทดสอบการแจ้งเตือน"


def test_health_reset_filter_button():
    class Probe(object):
        def cpu_temp_c(self):
            return 40.0

        def ram_pct(self):
            return 30.0

        def disk_pct(self):
            return 20.0

    h = Health({}, FakeMqtt(), Probe(), None, clock=Clock())
    h.filter_hours = 120.0
    h.baseline = {"50": 900}
    h.on_ui_cmd(None, {"cmd": "reset_filter"})
    assert h.filter_hours == 0.0 and h.baseline == {}
