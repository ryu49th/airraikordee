"""System health (AIR-21, GW-16, spec section 4 health.py).

- Remembers when each node was last heard; a node is offline after node_offline_s (30 s) of silence.
- Publishes ab/gw/health every publish_every_s (10 s), and at once when a node goes on- or offline.
- When a node that was online goes offline, also publishes a node_offline alert on ab/gw/alert (level crit),
  which the uploader stores and notify/discord.py sends.
- Reads Jetson CPU temperature, RAM and disk use; logs a warning when the disk is above disk_warn_pct (85 %).
- Counts filter hours (fan running) and PMS5003 hours (dust readings arriving), reset by a new
  config.filter_reset_ts from the dashboard (ab/gw/config), and the fan RPM ratio against a fresh filter.
  These counters survive restarts in data_dir/health_state.json.

Payload of ab/gw/health (HealthRecord in docs/protocol.md, minus the fields the uploader adds):
  {ts, node1_online, node1_last_seen, node2_online, node2_last_seen, cpu_temp_c, ram_pct, disk_pct,
   filter_hours, pms_hours, fan_rpm_ratio}

Run on the Jetson from gateway/:  python3 -m health.health
"""
import glob
import json
import logging
import os
import shutil
import threading
import time

from common.protocol import (TOPIC_GW_ALERT, TOPIC_GW_CONFIG, TOPIC_GW_HEALTH, TOPIC_GW_STATE, TOPIC_GW_UI_CMD,
                             TOPIC_NODE1_EVENT, TOPIC_NODE1_TELEMETRY, TOPIC_NODE2_TELEMETRY, now_ts)

log = logging.getLogger("health")

NODES = ("node1", "node2")
NODE_NAMES = {"node1": "เครื่องฟอก (Node 1)", "node2": "กล่องชาร์จ (Node 2)"}


# --- system readings (Linux / Jetson; None where a file is missing, e.g. on a Mac) --------------------

class SystemProbe(object):
    # The Nano's PMIC-Die zone is a fixed placeholder (often 100 °C), so it is never used.
    SKIP_ZONES = ("PMIC-Die",)

    def __init__(self, disk_path="/"):
        self.disk_path = disk_path

    def cpu_temp_c(self):
        best = None
        for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            kind = _read(os.path.join(zone, "type"))
            raw = _read(os.path.join(zone, "temp"))
            if kind in self.SKIP_ZONES or raw is None:
                continue
            try:
                temp = int(raw) / 1000.0
            except ValueError:
                continue
            if kind == "CPU-therm":
                return round(temp, 1)
            if 0 < temp < 125:
                best = temp if best is None else max(best, temp)
        return round(best, 1) if best is not None else None

    def ram_pct(self):
        text = _read("/proc/meminfo")
        if not text:
            return None
        info = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                info[parts[0].rstrip(":")] = int(parts[1])
        total, avail = info.get("MemTotal"), info.get("MemAvailable")
        if not total or avail is None:
            return None
        return int(round(100.0 * (total - avail) / total))

    def disk_pct(self):
        try:
            u = shutil.disk_usage(self.disk_path)
        except OSError:
            return None
        return int(round(100.0 * u.used / u.total)) if u.total else None


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# --- health -------------------------------------------------------------------------------------------

class Health(object):
    def __init__(self, cfg, mqtt, probe=None, state_path=None, clock=time.time):
        h = cfg.get("health", {})
        self.offline_s = float(h.get("node_offline_s", 30))
        self.publish_every_s = float(h.get("publish_every_s", 10))
        self.disk_warn_pct = float(h.get("disk_warn_pct", 85))
        self.baseline_hours = float(h.get("baseline_hours", 1.0))
        self.save_every_s = float(h.get("save_every_s", 60))
        self.mqtt = mqtt
        self.probe = probe or SystemProbe()
        self.state_path = state_path
        self.clock = clock

        self.last_seen = dict((n, None) for n in NODES)
        self.online = dict((n, False) for n in NODES)
        self.filter_hours = 0.0
        self.pms_hours = 0.0
        self.filter_reset_ts = None
        self.baseline = {}            # fan % bucket -> RPM with a fresh filter
        self.fan_rpm_ratio = None

        self._fan_rpm = None
        self._fan_pct = None          # from Node 1 telemetry, else from ab/gw/state
        self._pm_ok_at = None         # last time Node 1 sent a real PM reading
        self._last_count = None
        self._last_publish = None
        self._last_save = None
        self._disk_warned = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # --- lifecycle ---------------------------------------------------------------------------------

    def start(self):
        self.load_state()
        self.mqtt.subscribe(TOPIC_NODE1_TELEMETRY, self.on_node1)
        self.mqtt.subscribe(TOPIC_NODE1_EVENT, self.on_node1)
        self.mqtt.subscribe(TOPIC_NODE2_TELEMETRY, self.on_node2)
        self.mqtt.subscribe(TOPIC_GW_STATE, self.on_state)
        self.mqtt.subscribe(TOPIC_GW_CONFIG, self.on_config)
        self.mqtt.subscribe(TOPIC_GW_UI_CMD, self.on_ui_cmd)
        self._thread = threading.Thread(target=self._loop, name="health", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(3.0)
        self.save_state()

    def _loop(self):
        while not self._stop.wait(1.0):
            try:
                self.step()
            except Exception:
                log.exception("health step failed")

    # --- MQTT handlers -----------------------------------------------------------------------------

    def on_node1(self, topic, msg):
        now = self.clock()
        with self._lock:
            self.last_seen["node1"] = now
            if topic == TOPIC_NODE1_TELEMETRY:
                if _num(msg.get("fan_rpm")):
                    self._fan_rpm = float(msg["fan_rpm"])
                if _num(msg.get("fan_pct")):
                    self._fan_pct = float(msg["fan_pct"])
                if _num(msg.get("pm25")):
                    self._pm_ok_at = now

    def on_node2(self, topic, msg):
        with self._lock:
            self.last_seen["node2"] = self.clock()

    def on_state(self, topic, msg):
        with self._lock:
            if _num(msg.get("fan_pct")):
                self._fan_pct = float(msg["fan_pct"])

    def on_ui_cmd(self, topic, msg):
        if msg.get("cmd") != "reset_filter":
            return
        with self._lock:
            log.info("new filter (button): filter_hours %.1f -> 0, RPM baseline cleared", self.filter_hours)
            self.filter_hours = 0.0
            self.baseline = {}
            self.fan_rpm_ratio = None
            self._last_save = None

    def on_config(self, topic, msg):
        ts = msg.get("filter_reset_ts")
        if not _num(ts):
            return
        with self._lock:
            if self.filter_reset_ts is None:
                self.filter_reset_ts = ts       # first value we ever see: remember it, keep the counted hours
                return
            if ts > self.filter_reset_ts:
                log.info("new filter (filter_reset_ts=%s): filter_hours %.1f -> 0, RPM baseline cleared",
                         ts, self.filter_hours)
                self.filter_reset_ts = ts
                self.filter_hours = 0.0
                self.baseline = {}
                self.fan_rpm_ratio = None
                self._last_save = None          # save soon

    # --- periodic work -----------------------------------------------------------------------------

    def step(self):
        """Called about once a second. Returns the message when one was published, else None."""
        now = self.clock()
        with self._lock:
            self._count_hours(now)
            changed, went_down = self._update_online(now)
            due = self._last_publish is None or now - self._last_publish >= self.publish_every_s
            msg = self._message(now) if (changed or due) else None
            if msg is not None:
                self._last_publish = now
            save = self._last_save is None or now - self._last_save >= self.save_every_s
        for node in went_down:
            self.mqtt.publish(TOPIC_GW_ALERT, {
                "ts": int(now), "type": "node_offline", "level": "crit", "title": "อุปกรณ์ออฟไลน์",
                "text": "%s ไม่ส่งข้อมูลมา %.0f วินาที" % (NODE_NAMES.get(node, node), self.offline_s)})
        if msg is not None:
            self._check_disk(msg["disk_pct"])
            self.mqtt.publish(TOPIC_GW_HEALTH, msg)
        if save:
            self._last_save = now
            self.save_state()
        return msg

    def _update_online(self, now):
        """Returns (anything changed, nodes that just went from online to offline)."""
        changed = False
        went_down = []
        for node in NODES:
            seen = self.last_seen[node]
            up = seen is not None and now - seen < self.offline_s
            if up != self.online[node]:
                self.online[node] = up
                changed = True
                if up:
                    log.info("%s online", node)
                else:
                    log.warning("%s offline (no message for %.0f s)", node, self.offline_s)
                    went_down.append(node)
        return changed, went_down

    def _count_hours(self, now):
        last, self._last_count = self._last_count, now
        if last is None:
            return
        dt = now - last
        if dt <= 0 or dt > 120:
            return  # clock jump or the process slept: do not count it
        node1_up = self.last_seen["node1"] is not None and now - self.last_seen["node1"] < self.offline_s
        fan_on = node1_up and ((self._fan_rpm or 0) > 0 or (self._fan_pct or 0) > 0)
        if fan_on:
            self.filter_hours += dt / 3600.0
        if self._pm_ok_at is not None and now - self._pm_ok_at < self.offline_s:
            self.pms_hours += dt / 3600.0
        self._update_ratio(fan_on)

    def _update_ratio(self, fan_on):
        if not fan_on or not self._fan_rpm or not self._fan_pct:
            self.fan_rpm_ratio = None
            return
        bucket = str(int(round(self._fan_pct / 5.0) * 5))
        if self.filter_hours <= self.baseline_hours:
            old = self.baseline.get(bucket)
            # learn what a fresh filter looks like at this fan speed (slow average, so one odd reading is fine)
            self.baseline[bucket] = self._fan_rpm if old is None else old + 0.05 * (self._fan_rpm - old)
        base = self.baseline.get(bucket)
        self.fan_rpm_ratio = round(self._fan_rpm / base, 2) if base else None

    def _message(self, now):
        def seen(node):
            t = self.last_seen[node]
            return int(t) if t is not None else None

        return {
            "ts": now_ts(),
            "node1_online": self.online["node1"],
            "node1_last_seen": seen("node1"),
            "node2_online": self.online["node2"],
            "node2_last_seen": seen("node2"),
            "cpu_temp_c": self.probe.cpu_temp_c(),
            "ram_pct": self.probe.ram_pct(),
            "disk_pct": self.probe.disk_pct(),
            "filter_hours": round(self.filter_hours, 2),
            "pms_hours": round(self.pms_hours, 2),
            "fan_rpm_ratio": self.fan_rpm_ratio,
        }

    def _check_disk(self, pct):
        if pct is None:
            return
        if pct > self.disk_warn_pct and not self._disk_warned:
            self._disk_warned = True
            log.warning("disk %d%% full (warning above %.0f%%): check logs, snapshots and the outbox",
                        pct, self.disk_warn_pct)
        elif pct <= self.disk_warn_pct - 2:
            self._disk_warned = False

    # --- counters on disk --------------------------------------------------------------------------

    def load_state(self):
        if not self.state_path or not os.path.isfile(self.state_path):
            return False
        try:
            with open(self.state_path, encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("ignoring %s: %s", self.state_path, e)
            return False
        with self._lock:
            self.filter_hours = float(s.get("filter_hours", 0.0))
            self.pms_hours = float(s.get("pms_hours", 0.0))
            self.filter_reset_ts = s.get("filter_reset_ts")
            self.baseline = dict((str(k), float(v)) for k, v in (s.get("baseline") or {}).items())
        return True

    def save_state(self):
        if not self.state_path:
            return
        with self._lock:
            s = {"filter_hours": self.filter_hours, "pms_hours": self.pms_hours,
                 "filter_reset_ts": self.filter_reset_ts, "baseline": self.baseline}
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(s, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("cannot write %s: %s", self.state_path, e)


def main():
    from common.config import load_config, writable_dir
    from common.logging_setup import setup_logging
    from common.mqtt_client import MqttClient
    from common.service import run_until_signal

    cfg = load_config()
    setup_logging("health", cfg)
    data_dir = writable_dir(cfg.get("data_dir", "/var/lib/airraikordee"), "data")
    probe = SystemProbe(disk_path=data_dir)
    client = MqttClient("health", cfg)
    client.start(wait=False)
    health = Health(cfg, client, probe, os.path.join(data_dir, cfg.get("health", {}).get("state_file",
                                                                                         "health_state.json")))
    health.start()
    log.info("health running: offline after %.0f s, heartbeat every %.0f s", health.offline_s,
             health.publish_every_s)

    def report():
        log.info("cpu %s°C ram %s%% disk %s%% filter %.1f h pms %.1f h online %s",
                 health.probe.cpu_temp_c(), health.probe.ram_pct(), health.probe.disk_pct(),
                 health.filter_hours, health.pms_hours, health.online)

    try:
        run_until_signal(report, tick_s=300)
    finally:
        health.stop()
        client.stop()


if __name__ == "__main__":
    main()
