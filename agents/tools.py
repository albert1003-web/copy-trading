"""Read-only database tools for agents, plus the server-side web search tool.

The agent's connection can't write: it opens the file read-only, sets query_only, and an authorizer allows only
reads (no INSERT/UPDATE/DELETE, ATTACH, PRAGMA or DDL, even inside a CTE). A progress handler stops slow queries.
"""

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from common import config
from db import SCHEMA

MAX_ROWS = 200
MAX_CHARS = 40_000  # per tool result, so one wide query can't flood the context
QUERY_SECONDS = 10.0
SAMPLE_ROWS = 3

ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def _authorize(action, _arg1, _arg2, _db, _trigger):
    return sqlite3.SQLITE_OK if action in ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def readonly_connect(path: Path | str | None = None) -> sqlite3.Connection:
    """A connection that can only read. The database must already exist."""
    path = Path(path) if path else config.db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_authorize)
    return conn


# --- schema documentation (from db/schema.sql, comments included) -------------------------------------------------

_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \(")


def schema_blocks(text: str | None = None) -> dict[str, tuple[str, str]]:
    """table -> (leading comment, CREATE TABLE statement with its column comments), in file order."""
    lines = (text if text is not None else SCHEMA.read_text()).splitlines()
    blocks: dict[str, tuple[str, str]] = {}
    i = 0
    while i < len(lines):
        m = _CREATE.match(lines[i])
        if not m:
            i += 1
            continue
        comment, j = [], i - 1
        while j >= 0 and lines[j].startswith("--"):
            comment.insert(0, lines[j][2:].strip())
            j -= 1
        start = i
        while i < len(lines) and lines[i].strip() != ");":
            i += 1
        blocks[m.group(1)] = (" ".join(comment), "\n".join(lines[start : i + 1]))
        i += 1
    return blocks


# --- tools ------------------------------------------------------------------------------------------------------


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False
    rows: int | None = None
    truncated: bool = False


def list_tables(conn: sqlite3.Connection) -> ToolOutput:
    blocks = schema_blocks()
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    tables = [{"table": n, "about": blocks.get(n, ("", ""))[0]} for n in names]
    return ToolOutput(json.dumps(tables), rows=len(tables))


def describe_table(conn: sqlite3.Connection, name: str) -> ToolOutput:
    known = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if name not in known:
        return ToolOutput(f"No table named {name!r}. Call list_tables for the names.", is_error=True)
    comment, ddl = schema_blocks().get(name, ("", ""))
    if not ddl:  # created by a migration only
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
    sample = run_query(conn, f'SELECT * FROM "{name}" LIMIT {SAMPLE_ROWS}')
    text = (f"-- {comment}\n" if comment else "") + ddl + "\n\nSample rows:\n" + sample.content
    return ToolOutput(text, rows=sample.rows)


def run_query(conn: sqlite3.Connection, sql: str) -> ToolOutput:
    """Runs one read-only statement and returns up to MAX_ROWS rows as JSON {columns, rows, truncated}."""
    deadline = time.monotonic() + QUERY_SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(MAX_ROWS + 1)
        columns = [d[0] for d in cursor.description or []]
    except sqlite3.Error as e:
        message = str(e)
        if "interrupted" in message:
            message = f"query took over {QUERY_SECONDS:.0f} s; narrow it"
        elif "not authorized" in message:
            message = "not allowed: the database is read-only (a single SELECT only)"
        return ToolOutput(f"SQL error: {message}", is_error=True)
    finally:
        conn.set_progress_handler(None, 0)
    truncated = len(rows) > MAX_ROWS
    rows = [list(r) for r in rows[:MAX_ROWS]]
    content = json.dumps({"columns": columns, "rows": rows, "truncated": truncated}, default=str)
    while len(content) > MAX_CHARS and rows:
        rows = rows[: len(rows) // 2]
        truncated = True
        content = json.dumps({"columns": columns, "rows": rows, "truncated": truncated}, default=str)
    return ToolOutput(content, rows=len(rows), truncated=truncated)


def _schema(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


DB_TOOLS = [
    {
        "name": "list_tables",
        "description": "Lists every table in the trade tracker database with a one-line description.",
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "describe_table",
        "description": "Shows a table's columns with their documented meaning (from the schema file) and a few "
        "sample rows. Call it before querying a table you haven't used yet.",
        "strict": True,
        "input_schema": _schema({"name": {"type": "string", "description": "Table name"}}),
    },
    {
        "name": "query",
        "description": f"Runs one read-only SQLite SELECT (or WITH ... SELECT) and returns at most {MAX_ROWS} rows "
        "as JSON. Writes are rejected. Aggregate in SQL rather than fetching raw rows.",
        "strict": True,
        "input_schema": _schema({"sql": {"type": "string", "description": "A single SELECT statement"}}),
    },
]


def call(conn: sqlite3.Connection, name: str, tool_input: dict) -> ToolOutput:
    """Runs one database tool by name."""
    try:
        if name == "list_tables":
            return list_tables(conn)
        if name == "describe_table":
            return describe_table(conn, str(tool_input.get("name", "")))
        if name == "query":
            return run_query(conn, str(tool_input.get("sql", "")))
    except sqlite3.Error as e:
        return ToolOutput(f"SQL error: {e}", is_error=True)
    return ToolOutput(f"Unknown tool {name!r}", is_error=True)


def web_search(max_uses: int) -> dict:
    """The server-side web search tool (runs on Anthropic's side; results arrive in the response)."""
    return {"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}
