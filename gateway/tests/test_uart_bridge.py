import json
import queue
import threading
import time

import pytest

from bridge.uart_bridge import UartBridge
from common.protocol import TOPIC_GW_ALERT, TOPIC_NODE1_ACK, TOPIC_NODE1_CMD, TOPIC_NODE1_EVENT, TOPIC_NODE1_TELEMETRY

CFG = {"node1": {"device": "/dev/fake", "ack_timeout_s": 0.05, "retries": 3, "heartbeat_s": 0.05,
                 "reconnect_s": 0.05}}


class Unplugged(Exception):
    pass


class FakeSerial(object):
    """In-memory ESP32: records writes, optionally ACKs commands after dropping the first `drop_acks`."""

    def __init__(self, ack=True, drop_acks=0):
        self.rx = queue.Queue()
        self.written = []
        self.ack = ack
        self.drop_acks = drop_acks
        self.unplugged = False
        self.closed = False

    def readline(self):
        if self.unplugged:
            raise Unplugged("device reports readiness to read but returned no data")
        try:
            return self.rx.get(timeout=0.02)
        except queue.Empty:
            return b""

    def write(self, data):
        if self.unplugged:
            raise Unplugged("write failed")
        msg = json.loads(data.decode("utf-8"))
        self.written.append(msg)
        if self.ack and msg.get("cmd") != "hb":
            if self.drop_acks:
                self.drop_acks -= 1
            else:
                self.rx.put(('{"ack":%d}\n' % msg["seq"]).encode())
        return len(data)

    def close(self):
        self.closed = True

    def commands(self):
        return [m for m in self.written if m.get("cmd") != "hb"]


def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def make_bridge(request):
    from conftest import FakeMqtt
    bridges = []

    def make(serials):
        serials = list(serials)

        def factory():
            item = serials.pop(0) if len(serials) > 1 else serials[0]
            if isinstance(item, Exception):
                raise item
            return item

        mqtt = FakeMqtt()
        bridge = UartBridge(CFG, mqtt, serial_factory=factory)
        bridges.append(bridge)
        return bridge, mqtt

    yield make
    for b in bridges:
        b.stop()


def test_telemetry_and_events_are_published(make_bridge):
    ser = FakeSerial()
    bridge, mqtt = make_bridge([ser])
    bridge.start()
    ser.rx.put(b'{"node_id":"node1","seq":1,"ts":1,"pm25":12.5}\n')
    ser.rx.put(b'ets Jun  8 2016 boot garbage\n')
    ser.rx.put(b'{"node_id":"node1","seq":2,"ts":2,"type":"stop_pressed"}\n')
    assert wait_for(lambda: mqtt.on(TOPIC_NODE1_EVENT))
    assert mqtt.on(TOPIC_NODE1_TELEMETRY)[0]["pm25"] == 12.5
    assert mqtt.on(TOPIC_NODE1_EVENT)[0]["type"] == "stop_pressed"
    assert bridge.stats["bad_lines"] == 1


def test_partial_lines_are_joined(make_bridge):
    ser = FakeSerial()
    bridge, mqtt = make_bridge([ser])
    bridge.start()
    ser.rx.put(b'{"node_id":"node1","seq":1,')
    ser.rx.put(b'"ts":1,"pm25":7}\n')
    assert wait_for(lambda: mqtt.on(TOPIC_NODE1_TELEMETRY))
    assert mqtt.on(TOPIC_NODE1_TELEMETRY)[0]["pm25"] == 7


def test_command_is_renumbered_acked_and_confirmed(make_bridge):
    ser = FakeSerial()
    bridge, mqtt = make_bridge([ser])
    bridge.start()
    assert wait_for(bridge.connected.is_set)
    mqtt.deliver(TOPIC_NODE1_CMD, {"seq": 900, "ts": 1, "cmd": "fan", "pct": 70})
    assert wait_for(lambda: mqtt.on(TOPIC_NODE1_ACK))
    sent = ser.commands()
    assert len(sent) == 1 and sent[0]["cmd"] == "fan" and sent[0]["pct"] == 70
    assert sent[0]["seq"] != 900                      # bridge's own seq on the wire
    assert mqtt.on(TOPIC_NODE1_ACK)[0]["ack_seq"] == 900
    assert bridge.stats["cmd_ok"] == 1


def test_lost_acks_are_retried(make_bridge):
    ser = FakeSerial(drop_acks=2)
    bridge, mqtt = make_bridge([ser])
    bridge.start()
    assert wait_for(bridge.connected.is_set)
    mqtt.deliver(TOPIC_NODE1_CMD, {"seq": 1, "cmd": "stop"})
    assert wait_for(lambda: bridge.stats["cmd_ok"] == 1)
    assert len(ser.commands()) == 3
    assert len(set(m["seq"] for m in ser.commands())) == 1  # same seq on every resend
    assert bridge.stats["resends"] == 2
    assert not mqtt.on(TOPIC_GW_ALERT)


def test_no_ack_after_retries_raises_alert(make_bridge):
    ser = FakeSerial(ack=False)
    bridge, mqtt = make_bridge([ser])
    bridge.start()
    assert wait_for(bridge.connected.is_set)
    mqtt.deliver(TOPIC_NODE1_CMD, {"seq": 1, "cmd": "stop"})
    assert wait_for(lambda: mqtt.on(TOPIC_GW_ALERT))
    assert len(ser.commands()) == 4                   # first try + 3 retries
    assert mqtt.on(TOPIC_GW_ALERT)[0]["type"] == "node1_no_ack"


def test_heartbeat_is_sent(make_bridge):
    ser = FakeSerial()
    bridge, mqtt = make_bridge([ser])
    bridge.start()
    assert wait_for(lambda: len([m for m in ser.written if m.get("cmd") == "hb"]) >= 3)


def test_reconnects_after_unplug(make_bridge):
    first, second = FakeSerial(), FakeSerial()
    bridge, mqtt = make_bridge([OSError("no such device"), first, second])
    bridge.start()
    assert wait_for(lambda: bridge.connected.is_set() and first.written)
    first.unplugged = True
    assert wait_for(lambda: second.written)            # reopened the port and heartbeats again
    second.rx.put(b'{"node_id":"node1","seq":5,"ts":5,"pm25":30}\n')
    assert wait_for(lambda: mqtt.on(TOPIC_NODE1_TELEMETRY))
    kinds = [a["type"] for a in mqtt.on(TOPIC_GW_ALERT)]
    assert kinds == ["node1_disconnected", "node1_disconnected"]
    assert first.closed
    assert bridge.stats["reconnects"] == 2


def test_queue_coalesces_and_stop_jumps_ahead():
    from conftest import FakeMqtt
    bridge = UartBridge(CFG, FakeMqtt(), serial_factory=FakeSerial)
    bridge.on_cmd(TOPIC_NODE1_CMD, {"cmd": "aim", "pan_deg": 10})
    bridge.on_cmd(TOPIC_NODE1_CMD, {"cmd": "fan", "pct": 40})
    bridge.on_cmd(TOPIC_NODE1_CMD, {"cmd": "aim", "pan_deg": 20})
    assert [(m["cmd"], m.get("pan_deg")) for m in bridge._queue] == [("fan", None), ("aim", 20)]
    bridge.on_cmd(TOPIC_NODE1_CMD, {"cmd": "stop"})
    assert [m["cmd"] for m in bridge._queue] == ["stop"]
    bridge.on_cmd(TOPIC_NODE1_CMD, {"pan_deg": 5})      # no cmd: ignored
    assert len(bridge._queue) == 1
