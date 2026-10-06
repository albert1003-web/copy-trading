"""Parse cached PTRs into trades (Milestone 1.3): python -m parse.run [--reparse] [--doc-id ID] [--chamber C]

Reads each electronic filing's cached raw file (never the network) and upserts its rows into `trades`,
keyed by (doc_id, line_no) so trade_ids stay stable when a filing is re-parsed. Then it sets
filings.parse_status:
  parsed        rows came out, and every field was recognized
  needs_review  no rows came out, some value wasn't recognized, or a trade date is after the filing date
                (rows are still written, with confidence < 1)
  failed        the file is missing or the parser raised an error (logged; the run carries on)
Scanned filings are already needs_review from ingest and are never touched here.
"""

import argparse
import logging
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common import config
from common import log as logs
from db import connect
from parse import house_pdf, senate_html
from parse.normalize import ParsedTrade

log = logging.getLogger("parse.run")

PARSERS: dict[str, Callable[[Path], list[ParsedTrade]]] = {
    "house": lambda path: house_pdf.parse(path.read_bytes()),
    "senate": lambda path: senate_html.parse(path.read_text(encoding="utf-8")),
}


@dataclass
class Summary:
    parsed: int = 0
    needs_review: int = 0
    failed: int = 0
    rows: int = 0
    scanned_waiting: int = 0


def select(conn: sqlite3.Connection, *, reparse: bool, doc_ids: list[str] | None, chamber: str | None):
    """Electronic filings with a cached file: pending ones, every one (--reparse), or the given doc_ids."""
    sql = "SELECT * FROM filings WHERE doc_format = 'electronic' AND raw_path IS NOT NULL"
    params: list = []
    if doc_ids:
        sql += f" AND doc_id IN ({','.join('?' * len(doc_ids))})"
        params += doc_ids
    elif not reparse:
        sql += " AND parse_status = 'pending'"
    if chamber:
        sql += " AND chamber = ?"
        params.append(chamber)
    return conn.execute(sql + " ORDER BY first_seen_at, doc_id", params).fetchall()


def save(conn: sqlite3.Connection, filing: sqlite3.Row, trades: list[ParsedTrade], *,
         clean_confidence: float = 1.0) -> None:
    """Upserts the filing's rows by (doc_id, line_no), leaving enrichment columns alone. A row with no problems
    gets clean_confidence (below 1 for rows read by Claude from a scan); one with problems 0.5."""
    for t in trades:
        conn.execute(
            """
            INSERT INTO trades (doc_id, member_id, line_no, ticker, asset_name, asset_code, asset_type, action,
                                owner, tx_date, disclosure_date, amount_min, amount_max, description, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (doc_id, line_no) DO UPDATE SET
              member_id       = COALESCE(trades.member_id, excluded.member_id),
              ticker          = excluded.ticker,
              asset_name      = excluded.asset_name,
              asset_code      = excluded.asset_code,
              asset_type      = excluded.asset_type,
              action          = excluded.action,
              owner           = excluded.owner,
              tx_date         = excluded.tx_date,
              disclosure_date = excluded.disclosure_date,
              amount_min      = excluded.amount_min,
              amount_max      = excluded.amount_max,
              description     = excluded.description,
              confidence      = excluded.confidence
            """,
            (
                filing["doc_id"], filing["member_id"], t.line_no, t.ticker, t.asset_name, t.asset_code, t.asset_type,
                t.action, t.owner, t.tx_date, filing["filing_date"], t.amount_min, t.amount_max, t.description,
                min(t.confidence(filing["filing_date"]), clean_confidence),
            ),
        )
    _drop_stale_rows(conn, filing["doc_id"], len(trades))


def _drop_stale_rows(conn: sqlite3.Connection, doc_id: str, keep: int) -> None:
    """A re-parse that yields fewer rows removes the extras, unless an alert or a position points at one."""
    stale = conn.execute("SELECT trade_id FROM trades WHERE doc_id = ? AND line_no > ?", (doc_id, keep)).fetchall()
    for (trade_id,) in stale:
        referenced = conn.execute(
            "SELECT 1 FROM alerts WHERE trade_id = ? UNION ALL SELECT 1 FROM my_positions WHERE trade_id = ?",
            (trade_id, trade_id),
        ).fetchone()
        if referenced:
            log.warning("%s: trade %d is no longer in the filing but is referenced; kept", doc_id, trade_id)
        else:
            conn.execute("DELETE FROM trades WHERE trade_id = ?", (trade_id,))


def parse_filing(conn: sqlite3.Connection, filing: sqlite3.Row, raw_root: Path) -> tuple[str, int]:
    """Parses and stores one filing in its own transaction. Returns (parse_status, rows written)."""
    doc_id = filing["doc_id"]
    try:
        trades = PARSERS[filing["chamber"]](raw_root / filing["raw_path"])
        if len({t.line_no for t in trades}) != len(trades):
            raise ValueError("duplicate line numbers")
        line_nos = sorted(t.line_no for t in trades)
        if line_nos != list(range(1, len(trades) + 1)):
            raise ValueError(f"line numbers aren't 1..{len(trades)}: {line_nos[:5]}...")
        save(conn, filing, trades)
        problems = sorted({p for t in trades for p in t.problems(filing["filing_date"])})
        if not trades:
            status = "needs_review"
            log.warning("%s: no transactions found", doc_id)
        elif problems:
            status = "needs_review"
            log.warning("%s: needs review: %s", doc_id, ", ".join(problems))
        else:
            status = "parsed"
        conn.execute("UPDATE filings SET parse_status = ?, parse_method = 'text' WHERE doc_id = ?", (status, doc_id))
        conn.commit()
        return status, len(trades)
    except Exception as e:  # one bad filing must not stop the run
        conn.rollback()
        log.error("%s: parse failed: %s", doc_id, e)
        conn.execute("UPDATE filings SET parse_status = 'failed' WHERE doc_id = ?", (doc_id,))
        conn.commit()
        return "failed", 0


def run(
    conn: sqlite3.Connection,
    *,
    reparse: bool = False,
    doc_ids: list[str] | None = None,
    chamber: str | None = None,
    raw_root: Path | None = None,
) -> Summary:
    summary = Summary()
    raw_root = raw_root or config.raw_dir()
    for filing in select(conn, reparse=reparse, doc_ids=doc_ids, chamber=chamber):
        status, rows = parse_filing(conn, filing, raw_root)
        setattr(summary, status, getattr(summary, status) + 1)
        summary.rows += rows
    summary.scanned_waiting = conn.execute(
        "SELECT COUNT(*) FROM filings WHERE doc_format = 'scanned' AND parse_status = 'needs_review'"
    ).fetchone()[0]
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--reparse", action="store_true", help="re-parse every electronic filing, not just pending")
    parser.add_argument("--doc-id", action="append", help="parse only this filing (repeatable)")
    parser.add_argument("--chamber", choices=sorted(PARSERS), help="only this chamber")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    s = run(conn, reparse=args.reparse, doc_ids=args.doc_id, chamber=args.chamber)
    log.info(
        "Parsed %d filing(s) -> %d trade row(s); needs_review %d, failed %d; %d scanned filing(s) await review",
        s.parsed, s.rows, s.needs_review, s.failed, s.scanned_waiting,
    )
    return 1 if s.failed else 0


if __name__ == "__main__":
    sys.exit(main())
