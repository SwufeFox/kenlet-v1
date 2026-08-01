"""
日志 — 控制台 + 滚动文件。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: str | None = None, log_file: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    try:
        from kenlet.config import load_config
        cfg = load_config().get("logging", {})
    except Exception:
        cfg = {}

    lvl = (level or cfg.get("level", "INFO")).upper()
    log_path = log_file or cfg.get("file", "logs/kenlet-v1.log")
    fmt = cfg.get("format", "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    datefmt = cfg.get("datefmt", "%Y-%m-%d %H:%M:%S")
    max_bytes = int(cfg.get("max_bytes", 10_485_760))
    backup = int(cfg.get("backup_count", 5))

    root = logging.getLogger()
    root.setLevel(getattr(logging, lvl, logging.INFO))
    root.handlers.clear()

    formatter = logging.Formatter(fmt, datefmt=datefmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup, encoding="utf-8")
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except Exception:
        pass

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
