"""Message protocol shared by every gateway module (contract: docs/protocol.md).

All messages are JSON objects. Telemetry and commands carry node_id, seq (uint32 per sender) and ts (unix seconds).
"""
import json
import math
import threading
import time

TOPIC_NODE1_TELEMETRY = "ab/node1/telemetry"
TOPIC_NODE1_EVENT = "ab/node1/event"
TOPIC_NODE1_CMD = "ab/node1/cmd"
TOPIC_NODE1_ACK = "ab/node1/ack"
TOPIC_NODE2_TELEMETRY = "ab/node2/telemetry"
TOPIC_NODE2_CMD = "ab/node2/cmd"
TOPIC_NODE2_ACK = "ab/node2/ack"
TOPIC_GW_ENV = "ab/gw/env"
TOPIC_GW_VISION = "ab/gw/vision"
TOPIC_GW_CAUSE = "ab/gw/cause"
TOPIC_GW_STATE = "ab/gw/state"
TOPIC_GW_HEALTH = "ab/gw/health"
# Gateway-internal additions (not node-facing): one-off alerts from any module, uploader backlog stats.
TOPIC_GW_ALERT = "ab/gw/alert"
TOPIC_GW_UPLOADER = "ab/gw/uploader"

UINT32 = 2 ** 32


class ProtocolError(ValueError):
    pass


def now_ts():
    return round(time.time(), 3)


def encode(msg):
    """dict -> compact JSON text (keeps Thai text readable, rejects NaN which Firebase cannot store)."""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def decode(data):
    """bytes/str -> dict. Raises ProtocolError for anything that is not a JSON object."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    try:
        msg = json.loads(data)
    except ValueError as e:
        raise ProtocolError("bad JSON: %s" % e)
    if not isinstance(msg, dict):
        raise ProtocolError("message is not a JSON object")
    return msg


def clean_json(value):
    """Replace NaN/inf (a sensor glitch) with None so the message can still be encoded."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return dict((k, clean_json(v)) for k, v in value.items())
    if isinstance(value, list):
        return [clean_json(v) for v in value]
    return value


class SeqCounter(object):
    """Thread-safe uint32 sequence number for one sender."""

    def __init__(self, start=0):
        self._value = start % UINT32
        self._lock = threading.Lock()

    def next(self):
        with self._lock:
            self._value = (self._value + 1) % UINT32
            return self._value


class SeqTracker(object):
    """Counts lost packets per node from gaps in seq (metric: packet delivery)."""

    def __init__(self):
        self._last = {}
        self.lost = {}
        self.received = {}

    def update(self, node_id, seq):
        """Returns how many packets were lost just before this one (0 on first packet, reset or duplicate)."""
        self.received[node_id] = self.received.get(node_id, 0) + 1
        last = self._last.get(node_id)
        self._last[node_id] = seq
        if last is None:
            return 0
        diff = (seq - last) % UINT32
        if diff == 0 or diff > UINT32 // 2:
            return 0  # duplicate, or the node rebooted and restarted its seq
        lost = diff - 1
        self.lost[node_id] = self.lost.get(node_id, 0) + lost
        return lost
