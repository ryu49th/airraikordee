import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeMqtt(object):
    """Stands in for common.mqtt_client.MqttClient: records publishes, lets tests deliver messages."""

    def __init__(self):
        self.handlers = []
        self.published = []
        self._lock = threading.Lock()

    def subscribe(self, topic, handler, qos=1):
        self.handlers.append((topic, handler))

    def publish(self, topic, msg, qos=1, retain=False):
        with self._lock:
            self.published.append((topic, msg))
        return True

    def deliver(self, topic, msg):
        for t, handler in self.handlers:
            if t == topic:
                handler(topic, msg)

    def on(self, topic):
        with self._lock:
            return [m for t, m in self.published if t == topic]
