"""Paths and settings shared by every pipeline. Environment variables override the defaults.

All data lives outside the repo under ~/TradeTracker, next to the database the desktop app reads.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home() / "TradeTracker"


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Loads KEY=VALUE lines from .env without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _path(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value).expanduser() if value else default


def db_path() -> Path:
    return _path("TRACKER_DB_PATH", HOME / "tracker.db")


def raw_dir() -> Path:
    return _path("TRACKER_RAW_DIR", HOME / "raw")


def log_dir() -> Path:
    return _path("TRACKER_LOG_DIR", HOME / "logs")


def user_agent() -> str:
    contact = os.environ.get("TRACKER_CONTACT", "").strip()
    return f"TradeTracker/0.1 (personal research{'; ' + contact if contact else ''})"


load_dotenv()
