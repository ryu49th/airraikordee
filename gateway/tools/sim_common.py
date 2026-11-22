"""Shared helpers for the fake nodes (sim_node1.py, sim_node2.py).

Python 3.6 compatible (Jetson Nano / JetPack 4.6).
"""
import argparse
import json
import os
import sys
import threading
import time

# First file found wins for each key; variables already set in the shell win over both.
ENV_FILES = (".env", "/etc/airraikordee/.env")


def log(text):
    print(time.strftime("%H:%M:%S ") + text, flush=True)


def now():
    return int(time.time())


def clamp(value, low, high):
    return max(low, min(high, value))


def load_env(paths=ENV_FILES):
    """Read KEY=VALUE lines into os.environ without overriding what is already set."""
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def base_args(description):
    load_env()
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    p.add_argument("--user", default=os.environ.get("MQTT_USER"),
                   help="MQTT username (default: MQTT_USER from .env)")
    p.add_argument("--password", default=os.environ.get("MQTT_PASS"),
                   help="MQTT password (default: MQTT_PASS from .env)")
    p.add_argument("--speed", type=float, default=1.0,
                   help="simulated seconds per real second, e.g. 60 to charge the battery fast")
    p.add_argument("--seed", type=int, default=None, help="random seed for repeatable runs")
    p.add_argument("--no-keys", action="store_true", help="do not read keyboard commands")
    p.add_argument("-v", "--verbose", action="store_true", help="print every message sent")
    return p


class Seq(object):
    """uint32 sequence number, one counter per sender."""

    def __init__(self):
        self.value = 0

    def next(self):
        self.value = (self.value + 1) & 0xFFFFFFFF
        return self.value


class Link(object):
    """MQTT connection that reconnects by itself and speaks JSON."""

    def __init__(self, client_id, args, subscriptions, on_json):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            sys.exit("paho-mqtt is missing: pip3 install -r requirements.txt")
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
        except AttributeError:  # paho-mqtt 1.x (the only one on Python 3.6)
            client = mqtt.Client(client_id=client_id)
        if args.user:
            client.username_pw_set(args.user, args.password)
        self.client = client
        self.verbose = args.verbose
        self.where = "{}:{}".format(args.host, args.port)
        self._args = args
        self.connected = threading.Event()

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                log("connected to MQTT broker " + self.where)
                self.connected.set()
                for topic in subscriptions:
                    client.subscribe(topic, qos=1)
            else:
                log("broker refused the connection (rc={}): check MQTT_USER / MQTT_PASS".format(rc))

        def on_disconnect(client, userdata, rc):
            self.connected.clear()
            if rc != 0:
                log("lost the broker, reconnecting...")

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                log("ignored a non-JSON message on " + msg.topic)
                return
            if isinstance(payload, dict):
                on_json(msg.topic, payload)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        client.reconnect_delay_set(1, 30)

    def start(self, wait_s=10.0):
        log("connecting to " + self.where + " ...")
        self.client.connect_async(self._args.host, self._args.port, keepalive=30)
        self.client.loop_start()
        if not self.connected.wait(wait_s):
            log("no broker yet; running anyway and retrying in the background")

    def publish(self, topic, payload, qos=0):
        text = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, text, qos=qos)
        if self.verbose:
            log("-> {} {}".format(topic, text))

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


def start_keys(handler, help_text, enabled=True):
    """Read one-letter commands (letter + Enter) from the terminal in a background thread."""
    if not enabled or not sys.stdin.isatty():
        return
    print(help_text, flush=True)

    def loop():
        for line in sys.stdin:
            key = line.strip().lower()
            if key:
                handler(key)

    t = threading.Thread(target=loop)
    t.daemon = True
    t.start()


def run_every(period, tick):
    """Call tick() every `period` seconds until Ctrl+C."""
    next_t = time.time()
    try:
        while True:
            tick()
            next_t += period
            time.sleep(max(0.0, next_t - time.time()))
    except KeyboardInterrupt:
        print()
