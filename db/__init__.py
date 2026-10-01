"""SQLite access for the pipelines: one shared database with the desktop app.

connect() applies schema.sql (CREATE ... IF NOT EXISTS) and then any pending migrations, so every
entry point works on a fresh, current, or older database. The app runs the same files on startup.
"""

import logging
import re
import sqlite3
from pathlib import Path

from common import config

log = logging.getLogger(__name__)

DB_DIR = Path(__file__).resolve().parent
SCHEMA = DB_DIR / "schema.sql"
MIGRATIONS = DB_DIR / "migrations"
MIGRATION_FILE = re.compile(r"^(\d+)_.+\.sql$")

# A migration may re-add something schema.sql already created; those errors mean "already applied".
IGNORABLE_ERRORS = ("duplicate column name",)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text())
    migrate(conn)
    return conn


def migrations() -> list[tuple[int, Path]]:
    found = []
    for file in MIGRATIONS.glob("*.sql"):
        if m := MIGRATION_FILE.match(file.name):
            found.append((int(m.group(1)), file))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> None:
    """Applies migrations numbered above PRAGMA user_version, then records the new version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, file in migrations():
        if version <= current:
            continue
        for statement in split_statements(file.read_text()):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as e:
                if not any(msg in str(e) for msg in IGNORABLE_ERRORS):
                    raise
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        log.info("Applied migration %s", file.name)


def split_statements(sql: str) -> list[str]:
    """Splits a SQL script into complete statements, dropping comment-only lines."""
    statements, buffer = [], ""
    for line in sql.splitlines():
        if not buffer and (not line.strip() or line.strip().startswith("--")):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        statements.append(buffer.strip())
    return statements
