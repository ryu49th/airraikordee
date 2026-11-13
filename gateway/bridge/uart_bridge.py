"""Node 1 (ESP32-S3) USB serial <-> MQTT bridge.

Serial link: one JSON object per line, 115200 baud.
- Node 1 -> bridge: telemetry (published on ab/node1/telemetry), events with a "type" field
  (ab/node1/event), and {"ack": seq} replies to commands.
- bridge -> Node 1: commands from ab/node1/cmd, re-numbered with the bridge's own seq (several modules send
  commands, each with its own seq). Each one must be ACKed within ack_timeout_s, else it is resent up to
  `retries` times, then an alert goes out (E4). A {"cmd":"hb"} line every heartbeat_s keeps Node 1's
  2 s servo watchdog from stopping the head.
- If the port disappears (ESP32 reset, cable unplugged) the bridge keeps reopening it.

Run on the Jetson from gateway/:  python3 -m bridge.uart_bridge
"""
import logging
import threading
from collections import deque

from common.protocol import (TOPIC_GW_ALERT, TOPIC_NODE1_ACK, TOPIC_NODE1_CMD, TOPIC_NODE1_EVENT,
                             TOPIC_NODE1_TELEMETRY, ProtocolError, SeqCounter, SeqTracker, decode, encode,
                             now_ts)

log = logging.getLogger("uart_bridge")

# A newer command of these kinds makes any older queued one pointless (only the latest aim/fan matters).
COALESCE = ("aim", "fan", "survey_step")
MAX_LINE = 4096


class UartBridge(object):
    def __init__(self, cfg, mqtt_client, serial_factory=None):
        n = cfg.get("node1", {})
        self.device = n.get("device", "/dev/airraikordee-node1")
        self.baud = int(n.get("baud", 115200))
        self.ack_timeout = float(n.get("ack_timeout_s", 0.3))
        self.retries = int(n.get("retries", 3))
        self.heartbeat_s = float(n.get("heartbeat_s", 0.5))
        self.reconnect_s = float(n.get("reconnect_s", 1.0))

        self.mqtt = mqtt_client
        self._open_serial = serial_factory or self._default_serial
        self._ser = None
        self._ser_lock = threading.Lock()
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._threads = []

        self._queue = deque()
        self._queue_cv = threading.Condition()
        self._seq = SeqCounter()
        self._acks = {}
        self._acks_lock = threading.Lock()
        self.rx_seq = SeqTracker()
        self.stats = {"rx_lines": 0, "bad_lines": 0, "cmd_ok": 0, "cmd_failed": 0, "resends": 0,
                      "reconnects": 0}

    def _default_serial(self):
        import serial
        return serial.Serial(self.device, self.baud, timeout=0.2, write_timeout=0.5)

    # --- lifecycle ---------------------------------------------------------------------------------

    def start(self):
        self.mqtt.subscribe(TOPIC_NODE1_CMD, self.on_cmd)
        for target in (self._serial_loop, self._sender_loop, self._heartbeat_loop):
            t = threading.Thread(target=target, name=target.__name__, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        with self._queue_cv:
            self._queue_cv.notify_all()
        self._close_serial()
        for t in self._threads:
            t.join(2.0)

    # --- serial side -------------------------------------------------------------------------------

    def _serial_loop(self):
        down_logged = False
        while not self._stop.is_set():
            try:
                ser = self._open_serial()
            except Exception as e:
                if not down_logged:
                    log.warning("cannot open %s: %s (retrying every %.1f s)", self.device, e, self.reconnect_s)
                    down_logged = True
                self._stop.wait(self.reconnect_s)
                continue

            with self._ser_lock:
                self._ser = ser
            if down_logged:
                self.stats["reconnects"] += 1
            down_logged = False
            self.connected.set()
            log.info("serial %s open", self.device)
            try:
                self._read_lines(ser)
            except Exception as e:
                if not self._stop.is_set():
                    log.warning("serial %s lost: %s", self.device, e)
                    down_logged = True
            finally:
                self.connected.clear()
                self._close_serial()
            self._stop.wait(self.reconnect_s)

    def _close_serial(self):
        with self._ser_lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _read_lines(self, ser):
        buf = b""
        while not self._stop.is_set():
            chunk = ser.readline()  # returns b"" or a partial line on timeout
            if not chunk:
                continue
            buf += chunk
            if not buf.endswith(b"\n"):
                if len(buf) > MAX_LINE:
                    log.warning("dropping %d bytes without newline", len(buf))
                    buf = b""
                continue
            line, buf = buf, b""
            self.handle_line(line)

    def handle_line(self, line):
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            msg = decode(text)
        except ProtocolError:
            self.stats["bad_lines"] += 1
            log.debug("non-JSON line from node1: %r", text[:120])  # e.g. ESP32 boot log
            return
        self.stats["rx_lines"] += 1

        if "ack" in msg:
            self._resolve_ack(msg["ack"])
            return

        seq = msg.get("seq")
        if isinstance(seq, int):
            lost = self.rx_seq.update(msg.get("node_id", "node1"), seq)
            if lost:
                log.warning("node1 seq gap: %d packet(s) lost before seq %d", lost, seq)
        topic = TOPIC_NODE1_EVENT if "type" in msg else TOPIC_NODE1_TELEMETRY
        self.mqtt.publish(topic, msg)

    def _write(self, msg):
        line = (encode(msg) + "\n").encode("utf-8")
        with self._ser_lock:
            if self._ser is None:
                return False
            try:
                self._ser.write(line)
                return True
            except Exception as e:
                log.warning("serial write failed: %s", e)  # the reader loop notices and reconnects
                return False

    def _heartbeat_loop(self):
        while not self._stop.wait(self.heartbeat_s):
            if self.connected.is_set():
                self._write({"cmd": "hb", "ts": now_ts()})

    # --- command side ------------------------------------------------------------------------------

    def on_cmd(self, topic, msg):
        cmd = msg.get("cmd")
        if not isinstance(cmd, str):
            log.warning("ignoring command without 'cmd': %r", msg)
            return
        with self._queue_cv:
            if cmd == "stop":
                self._queue.clear()  # stop beats everything still waiting
                self._queue.appendleft(msg)
            else:
                if cmd in COALESCE:
                    for old in [m for m in self._queue if m.get("cmd") == cmd]:
                        self._queue.remove(old)
                self._queue.append(msg)
            self._queue_cv.notify()

    def _superseded(self, cmd):
        with self._queue_cv:
            return any(m.get("cmd") == "stop" or (cmd in COALESCE and m.get("cmd") == cmd)
                       for m in self._queue)

    def _sender_loop(self):
        while not self._stop.is_set():
            with self._queue_cv:
                while not self._queue and not self._stop.is_set():
                    self._queue_cv.wait(0.5)
                if self._stop.is_set():
                    return
                msg = self._queue.popleft()
            self.deliver(msg)

    def deliver(self, msg):
        """Send one command and wait for its ACK, resending up to `retries` times. Returns True on ACK."""
        cmd = msg.get("cmd")
        seq = self._seq.next()
        wire = dict(msg)
        wire["seq"] = seq
        wire.setdefault("ts", now_ts())
        acked = threading.Event()
        with self._acks_lock:
            self._acks[seq] = acked
        try:
            for attempt in range(1 + self.retries):
                if attempt:
                    self.stats["resends"] += 1
                    log.info("no ack for %s seq %d, resend %d/%d", cmd, seq, attempt, self.retries)
                sent = self._write(wire)
                if acked.wait(self.ack_timeout):
                    self.stats["cmd_ok"] += 1
                    if "seq" in msg:
                        self.mqtt.publish(TOPIC_NODE1_ACK, {"ack_seq": msg["seq"], "ts": now_ts()})
                    return True
                if not sent and not self.connected.is_set():
                    break  # Node 1 is gone; the disconnect alert already went out
                if self._superseded(cmd):
                    log.debug("%s seq %d replaced by a newer command", cmd, seq)
                    return False
        finally:
            with self._acks_lock:
                self._acks.pop(seq, None)
        self.stats["cmd_failed"] += 1
        log.warning("node1 did not ack %s seq %d", cmd, seq)
        self.mqtt.publish(TOPIC_GW_ALERT, {
            "ts": now_ts(), "source": "uart_bridge", "type": "uart_fail", "severity": "warn",
            "msg": "Node 1 ไม่ตอบคำสั่ง %s หลังส่งซ้ำ %d ครั้ง" % (cmd, self.retries)})
        return False

    def _resolve_ack(self, seq):
        with self._acks_lock:
            event = self._acks.get(seq)
        if event is not None:
            event.set()


def main():
    from common.config import load_config
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    cfg = load_config()
    setup_logging("uart_bridge", cfg)
    client = MqttClient("uart_bridge", cfg)
    client.start(wait=False)
    bridge = UartBridge(cfg, client)
    bridge.start()
    log.info("uart_bridge running on %s", bridge.device)

    def report():
        log.info("stats %s lost=%s", bridge.stats, bridge.rx_seq.lost)

    try:
        run_until_signal(report, tick_s=60)
    finally:
        bridge.stop()
        client.stop()


if __name__ == "__main__":
    main()
