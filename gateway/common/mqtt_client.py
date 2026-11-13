"""Thin wrapper around paho-mqtt used by every module.

- connects in the background and keeps reconnecting (broker restart, boot order),
- re-subscribes after every reconnect,
- decodes JSON once and hands handlers (topic, dict).
"""
import logging
import os
import threading

import paho.mqtt.client as mqtt

from common.protocol import ProtocolError, decode, encode

log = logging.getLogger("mqtt")


def _new_client(client_id):
    try:
        # paho-mqtt >= 2.0 (laptop); the Jetson pins 1.6.1, which has no CallbackAPIVersion.
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, clean_session=True)
    except AttributeError:
        return mqtt.Client(client_id=client_id, clean_session=True)


class MqttClient(object):
    def __init__(self, client_id, cfg):
        m = cfg.get("mqtt", {})
        self.host = m.get("host", "127.0.0.1")
        self.port = int(m.get("port", 1883))
        self.keepalive = int(m.get("keepalive", 30))
        self.connected = threading.Event()
        self._handlers = []  # (topic filter, handler, qos)
        self._lock = threading.Lock()

        self._client = _new_client(client_id)
        user = os.environ.get("MQTT_USER")
        if user:
            self._client.username_pw_set(user, os.environ.get("MQTT_PASS", ""))
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def start(self, wait=True, timeout=10.0):
        """Connect in the background. With wait=True, block until connected or timeout (returns bool)."""
        self._client.connect_async(self.host, self.port, self.keepalive)
        self._client.loop_start()
        if wait:
            return self.connected.wait(timeout)
        return self.connected.is_set()

    def stop(self):
        self._client.disconnect()
        self._client.loop_stop()

    def subscribe(self, topic, handler, qos=1):
        with self._lock:
            self._handlers.append((topic, handler, qos))
        if self.connected.is_set():
            self._client.subscribe(topic, qos)

    def publish(self, topic, msg, qos=1, retain=False):
        payload = msg if isinstance(msg, str) else encode(msg)
        info = self._client.publish(topic, payload.encode("utf-8"), qos=qos, retain=retain)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("broker %s:%d refused connection: %s", self.host, self.port, mqtt.connack_string(rc))
            return
        log.info("connected to broker %s:%d", self.host, self.port)
        with self._lock:
            for topic, _, qos in self._handlers:
                client.subscribe(topic, qos)
        self.connected.set()

    def _on_disconnect(self, client, userdata, rc):
        self.connected.clear()
        if rc != 0:
            log.warning("lost broker connection (rc=%s), reconnecting", rc)

    def _on_message(self, client, userdata, message):
        try:
            data = decode(message.payload)
        except ProtocolError as e:
            log.warning("ignoring message on %s: %s", message.topic, e)
            return
        with self._lock:
            handlers = list(self._handlers)
        for topic, handler, _ in handlers:
            if mqtt.topic_matches_sub(topic, message.topic):
                try:
                    handler(message.topic, data)
                except Exception:
                    log.exception("handler for %s failed", message.topic)
