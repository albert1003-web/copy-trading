import sqlite3
from pathlib import Path

from db import connect, migrations, split_statements

OLD_SCHEMA = Path(__file__).parent / "fixtures" / "schema_v0.sql"


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def indexes(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def latest_version():
    return migrations()[-1][0]


def test_fresh_database_has_full_schema_and_latest_version(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"members", "filings", "trades", "watchlist", "agent_runs", "source_state", "pipeline_runs"} <= tables
    assert {"filer_name", "doc_format", "index_seen_at", "search_seen_at"} <= columns(conn, "filings")
    assert {"line_no", "asset_name", "asset_code", "description", "symbol", "ticker_status"} <= columns(conn, "trades")
    assert "rule" in columns(conn, "alerts") and "kind" in columns(conn, "filing_alerts")
    assert "idx_trades_doc_line" in indexes(conn, "trades")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_old_database_is_upgraded_without_losing_data(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA.read_text())
    old.execute("INSERT INTO members (member_id, name, chamber) VALUES ('P1', 'Someone', 'house')")
    old.execute("INSERT INTO watchlist (member_id, added_at) VALUES ('P1', '2026-09-30T00:00:00Z')")
    old.commit()
    old.close()

    conn = connect(path)
    assert "filer_name" in columns(conn, "filings")
    assert {"line_no", "symbol"} <= columns(conn, "trades")
    assert "kind" in columns(conn, "filing_alerts")
    assert "idx_trades_doc_line" in indexes(conn, "trades")
    assert {"status", "model", "usage", "error", "finished_at"} <= columns(conn, "agent_runs")
    assert {"kind", "member_id", "approved", "applied_at", "apply_result"} <= columns(conn, "agent_proposals")
    assert {"parse_method", "vision_attempts", "vision_attempted_at", "vision_error"} <= columns(conn, "filings")
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version()


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "twice.db"
    connect(path).close()
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest_version()


def test_split_statements_skips_comments_and_keeps_multiline():
    sql = "-- header\n\nALTER TABLE a ADD COLUMN b TEXT;\nCREATE TABLE x (\n  y TEXT -- note; here\n);\n"
    assert split_statements(sql) == ["ALTER TABLE a ADD COLUMN b TEXT;", "CREATE TABLE x (\n  y TEXT -- note; here\n);"]
