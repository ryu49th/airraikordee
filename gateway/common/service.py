"""Run a module until systemd (SIGTERM) or Ctrl+C (SIGINT) stops it."""
import signal
import threading


def run_until_signal(on_tick=None, tick_s=60.0):
    stop = threading.Event()

    def _stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop.wait(tick_s):
        if on_tick:
            on_tick()
