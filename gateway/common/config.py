"""Load config.yaml and secrets from .env.

Secrets (MQTT password, Firebase token, API keys) live only in /etc/airraikordee/.env, never in Git (E5).
Real environment variables win over the .env file, so systemd `Environment=` can override anything.
"""
import os

import yaml

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(GATEWAY_DIR, "config.yaml")
ENV_FILES = ["/etc/airraikordee/.env", os.path.join(GATEWAY_DIR, ".env")]


def load_env(paths=None):
    """Read KEY=VALUE lines from the first .env file that exists. Returns its path or None."""
    for path in paths or ENV_FILES:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key.strip(), value)
        return path
    return None


def load_config(path=None):
    """Load config.yaml (path, $AIRRAIKORDEE_CONFIG, or gateway/config.yaml) and the .env secrets."""
    path = path or os.environ.get("AIRRAIKORDEE_CONFIG", DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    load_env()
    return cfg


def secret(name, default=""):
    return os.environ.get(name, default)


def writable_dir(preferred, fallback_name):
    """Return `preferred` if we can write there (on the Jetson), else gateway/<fallback_name> (on a laptop)."""
    for path in (preferred, os.path.join(GATEWAY_DIR, fallback_name)):
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            continue
        if os.access(path, os.W_OK):
            return path
    raise OSError("no writable directory for %s" % fallback_name)
