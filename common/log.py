"""Logging to the console and to ~/TradeTracker/logs/pipeline.log."""

import logging
from logging.handlers import RotatingFileHandler

from common import config

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(console)

    log_dir = config.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    file = RotatingFileHandler(log_dir / "pipeline.log", maxBytes=5_000_000, backupCount=3)
    file.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file)
    logging.getLogger("httpx").setLevel(logging.WARNING)
