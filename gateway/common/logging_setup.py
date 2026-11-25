"""Logging to stderr (journald picks it up under systemd) and to a rotating file per service."""
import logging
import logging.handlers
import os

from common.config import writable_dir

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(name, cfg=None):
    log_cfg = (cfg or {}).get("logging", {})
    log_dir = writable_dir(log_cfg.get("dir", "/var/log/airraikordee"), "logs")

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(log_cfg.get("level", "INFO"))

    formatter = logging.Formatter(FORMAT)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    path = os.path.join(log_dir, name + ".log")
    rotating = logging.handlers.RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    rotating.setFormatter(formatter)
    root.addHandler(rotating)

    return logging.getLogger(name)
