"""Claude reads scanned PTRs (Milestone 5.4): python -m parse.llm_fallback [--doc-id ID] [--limit N] [--dry-run]

Scanned filings (House paper PDFs, Senate paper reports' page images) have no text layer, so the text parsers
skip them. Here Claude (through the local Claude Code CLI, on the Claude subscription) reads the pages and
transcribes each transaction; the rows go through the same normalization (parse/normalize.py) and upsert
(parse.run.save) as parsed rows, with confidence VISION_CONFIDENCE (0.5 where a value didn't normalize), and the
filing gets parse_method = 'vision'.

Vision trades show in the app and can alert (flagged "read by Claude from a scanned filing"), but analytics never
use them (analytics/outcomes.py leaves them out of scope): a misread amount or ticker can't skew the stats.

Each pass (pipeline stage vision_parse, after parse and before enrich so new rows get symbols and alerts the same
pass): live-detected filings first (LIVE_PER_PASS), then the backlog newest first (BACKLOG_PER_PASS, at most
BACKLOG_PER_DAY a day, ET). A failed read is retried after a day, at most MAX_ATTEMPTS times. Every read is logged
in agent_runs as agent 'filing_parser'.
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from common import config
from common import log as logs
from db import connect
from parse import normalize
from parse.normalize import ParsedTrade
from parse.run import save

log = logging.getLogger("parse.llm_fallback")

AGENT = "filing_parser"
VISION_CONFIDENCE = 0.7
LIVE_PER_PASS = 5
BACKLOG_PER_PASS = 3
BACKLOG_PER_DAY = 20
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(hours=24)
TIMEOUT_SECONDS = 300
PAGES_MANIFEST = "pages.json"  # written by ingest/senate.py once a paper report's page images are all cached
ET = ZoneInfo("America/New_York")

# Older Senate forms mark the owner in front of the asset: "(S) MH Four Winds LLC". Not a ticker (S is SentinelOne).
OWNER_MARK = re.compile(r"^\s*\((?:S|SP|DC|J|JT)\)\s*", re.I)
MARK_OWNERS = {"S": "spouse", "SP": "spouse", "DC": "dependent", "J": "joint", "JT": "joint"}
TYPES = {"Purchase": "BUY", "Sale": "SELL", "Partial Sale": "SELL_PARTIAL", "Exchange": "EXCHANGE"}
NULLABLE = {"anyOf": [{"type": "string"}, {"type": "null"}]}
SCHEMA = {
    "type": "object",
    "properties": {
        "readable": {"type": "boolean", "description": "false if the pages can't be read at all"},
        "is_ptr": {"type": "boolean", "description": "false if this isn't a Periodic Transaction Report"},
        "filer_name": NULLABLE,
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"anyOf": [{"type": "string", "enum": ["self", "spouse", "joint", "dependent"]},
                                        {"type": "null"}]},
                    "asset": {"type": "string", "description": "the asset name exactly as written"},
                    "ticker": {**NULLABLE, "description": "only a ticker the filer wrote on the form"},
                    "type": {"anyOf": [{"type": "string", "enum": list(TYPES)}, {"type": "null"}]},
                    "date": {**NULLABLE, "description": "transaction date exactly as written, e.g. 6/30/26"},
                    "amount": {**NULLABLE, "description": "the checked amount range, e.g. $15,001 - $50,000"},
                    "notes": NULLABLE,
                },
                "required": ["owner", "asset", "ticker", "type", "date", "amount", "notes"],
                "additionalProperties": False,
            },
        },
        "problems": {**NULLABLE, "description": "anything unreadable or ambiguous"},
    },
    "required": ["readable", "is_ptr", "filer_name", "rows", "problems"],
    "additionalProperties": False,
}

SYSTEM = """\
You transcribe scanned U.S. congressional Periodic Transaction Reports (PTRs) into structured rows. Accuracy
matters more than completeness: copy values exactly as written, use null for anything you can't read, and never
guess a ticker, date or amount that isn't on the form."""

PROMPT = """\
Read every page of this scanned PTR ({files}) and return one row per transaction.

House form (one row per asset): FULL ASSET NAME; an owner box at the left (SP = spouse, DC = dependent child,
JT = joint; blank = self); TYPE OF TRANSACTION checkbox columns Purchase / Sale / Partial Sale / Exchange; DATE OF
TRANSACTION; DATE NOTIFIED (ignore it); AMOUNT OF TRANSACTION checkbox columns A-K:
A $1,001 - $15,000, B $15,001 - $50,000, C $50,001 - $100,000, D $100,001 - $250,000, E $250,001 - $500,000,
F $500,001 - $1,000,000, G $1,000,001 - $5,000,000, H $5,000,001 - $25,000,000, I $25,000,001 - $50,000,000,
J Over $50,000,000, K Over $1,000,000 (a spouse or child asset).
Skip the printed sample row ("Example Mega Corp Common Stock") and empty rows.

Senate paper forms: either Transaction Date, Owner (Self / Spouse / Joint / Child = dependent), Ticker, Asset
Name, Asset Type, Type (Purchase / Sale (Full) = Sale / Sale (Partial) = Partial Sale / Exchange) and Amount
columns; or "Periodic Disclosure of Financial Transactions" (Identification of Assets, Purchase / Sale / Exchange,
Transaction Date, Amount columns), where a prefix marks the owner: (S) spouse, (DC) dependent child, (J) joint,
none = self. A prefix like (S) is an owner, never a ticker. Lines that only name a holding entity (often ending
with ":") and have no transaction marked are headers, not rows; skip the printed examples (IBM, Microsoft).

For each row: owner, asset (as written, including any ticker or "(private)"), ticker (a ticker symbol the filer
wrote for that asset: in a ticker field, in parentheses, or after the name as in "Gold Miners ETF GDX"; null if
none is written), type, date (as written), amount (the checked range written like the list above), notes (e.g.
"amount box unclear", "option: call"). If the pages aren't a PTR, set is_ptr false."""


@dataclass
class Summary:
    read: int = 0  # filings Claude read into rows
    rows: int = 0
    unreadable: int = 0  # read, but no usable rows (not a PTR, unreadable, empty)
    failed: int = 0  # the read itself failed (will retry)
    skipped_no_files: int = 0
    errors: list[str] = field(default_factory=list)


def utc_now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- selection ------------------------------------------------------------------------------------------------


def files_for(filing: sqlite3.Row | dict, raw_root: Path) -> list[Path]:
    """The scanned pages on disk: the House PDF, or a Senate report's complete set of page images."""
    if not filing["raw_path"]:
        return []
    raw = raw_root / filing["raw_path"]
    if filing["chamber"] == "house":
        return [raw] if raw.exists() else []
    folder = raw.with_suffix("")  # senate/<year>/<uuid>.html -> senate/<year>/<uuid>/
    manifest = folder / PAGES_MANIFEST
    if not manifest.exists():
        return []
    pages = [folder / name for name in json.loads(manifest.read_text())]
    return pages if all(p.exists() for p in pages) else []


def candidates(conn: sqlite3.Connection, now: datetime, doc_ids: list[str] | None = None) -> list[sqlite3.Row]:
    """Scanned filings Claude should read now: live ones first, then today's share of the backlog."""
    retry_before = stamp(now - RETRY_AFTER)
    base = f"""
        SELECT * FROM filings
        WHERE doc_format = 'scanned' AND parse_method IS NULL AND vision_attempts < {MAX_ATTEMPTS}
          AND (vision_attempted_at IS NULL OR vision_attempted_at < ?)
    """
    if doc_ids:
        marks = ", ".join("?" * len(doc_ids))
        return conn.execute(f"SELECT * FROM filings WHERE doc_id IN ({marks}) AND doc_format = 'scanned'",
                            doc_ids).fetchall()
    live = conn.execute(base + " AND available_basis = 'seen' ORDER BY first_seen_at DESC LIMIT ?",
                        (retry_before, LIVE_PER_PASS)).fetchall()
    day_start = stamp(datetime.combine(now.astimezone(ET).date(), datetime.min.time(), ET))
    used_today = conn.execute(
        "SELECT COUNT(*) FROM filings WHERE doc_format = 'scanned' AND available_basis = 'filed' "
        "AND vision_attempted_at >= ?", (day_start,)).fetchone()[0]
    room = max(0, min(BACKLOG_PER_PASS, BACKLOG_PER_DAY - used_today))
    backlog = conn.execute(base + " AND available_basis = 'filed' ORDER BY filing_date DESC, doc_id LIMIT ?",
                           (retry_before, room)).fetchall() if room else []
    return list(live) + list(backlog)


# --- normalization --------------------------------------------------------------------------------------------


def _date(raw: str | None) -> str | None:
    text = normalize.clean(raw)
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _asset_type(asset: str, ticker: str | None, notes: str | None) -> str:
    text = f"{asset} {notes or ''}".lower()
    if normalize.is_option_name(asset) or " option" in text or "call" in text.split() or "put" in text.split():
        return "option"
    if ticker or "stock" in text or "common" in text or "shares" in text:
        return "stock"
    return "other"


def to_trades(rows: list[dict]) -> list[ParsedTrade]:
    trades = []
    for i, r in enumerate(rows, 1):
        asset = normalize.clean(r.get("asset")) or "?"
        ticker = normalize.ticker(r.get("ticker")) or normalize.ticker_in_name(OWNER_MARK.sub("", asset))
        mark = OWNER_MARK.match(asset)
        marked = mark.group(0).strip(" ()").upper() if mark else None
        if ticker and ticker == marked:
            ticker = None  # the row's owner mark copied into the ticker field (S and J are also real tickers)
        low, high = normalize.amount(r.get("amount"))
        trades.append(ParsedTrade(
            line_no=i,
            owner=normalize.owner(r.get("owner") or MARK_OWNERS.get(marked, "")),
            asset_name=asset,
            ticker=ticker,
            asset_code=None,
            asset_type=_asset_type(asset, ticker, r.get("notes")),
            action=TYPES.get(r.get("type") or ""),
            tx_date=_date(r.get("date")),
            amount_min=low,
            amount_max=high,
            description=normalize.clean(r.get("notes")) or None,
        ))
    return trades


# --- the run --------------------------------------------------------------------------------------------------


def extract_with_claude(files: list[Path], doc_id: str, conn: sqlite3.Connection) -> dict:
    """One Claude Code read, logged as an agent run. Raises on failure (after logging the failed run)."""
    from agents import claude_code, runner  # the model-access layer; nothing else from agents

    trace = runner.Trace()
    run_id = runner.start_run(conn, AGENT, {"doc_id": doc_id, "files": [f.name for f in files]}, runner.MODEL)
    prompt = PROMPT.format(files=", ".join(f.name for f in files))
    try:
        output = claude_code.read_files(files, system=SYSTEM, prompt=prompt, schema=SCHEMA, trace=trace,
                                        timeout=TIMEOUT_SECONDS)
    except Exception as e:
        runner.fail_run(conn, run_id, str(e), trace)
        raise
    summary = (f"{len(output.get('rows') or [])} row(s) read from {doc_id}"
               + (f"; problems: {output['problems']}" if output.get("problems") else ""))
    runner.finish_run(conn, run_id, summary + "\n\n" + json.dumps(output, indent=1), [], trace)
    return output


def read_filing(conn: sqlite3.Connection, filing: sqlite3.Row, files: list[Path], *, now: datetime,
                extract: Callable[[list[Path], str, sqlite3.Connection], dict], dry_run: bool = False,
                out: Callable[[str], None] = print) -> tuple[str, int]:
    """Reads one filing; returns (outcome, rows). Outcome: read | unreadable | failed."""
    doc_id = filing["doc_id"]
    try:
        output = extract(files, doc_id, conn)
    except Exception as e:
        log.warning("%s: Claude couldn't read it: %s (will retry)", doc_id, e)
        if not dry_run:
            conn.execute("UPDATE filings SET vision_attempts = vision_attempts + 1, vision_attempted_at = ?, "
                         "vision_error = ? WHERE doc_id = ?", (stamp(now), str(e)[:500], doc_id))
            conn.commit()
        return "failed", 0

    trades = to_trades(output.get("rows") or []) if output.get("readable") and output.get("is_ptr") else []
    if dry_run:
        out(f"--- {doc_id} ({filing['filer_name']}, filed {filing['filing_date']}): {len(trades)} row(s)")
        for t in trades:
            out(f"  {t.action} {t.ticker or '-'} {t.asset_name} | {t.owner} | {t.tx_date} | "
                f"{t.amount_min}-{t.amount_max} | problems: {', '.join(t.problems(filing['filing_date'])) or '-'}")
        return ("read" if trades else "unreadable"), len(trades)
    if not trades:
        reason = output.get("problems") or ("not a PTR" if not output.get("is_ptr") else "no rows read")
        conn.execute("UPDATE filings SET vision_attempts = vision_attempts + 1, vision_attempted_at = ?, "
                     "vision_error = ? WHERE doc_id = ?", (stamp(now), str(reason)[:500], doc_id))
        conn.commit()
        return "unreadable", 0

    save(conn, filing, trades, clean_confidence=VISION_CONFIDENCE)
    clean = not any(t.problems(filing["filing_date"]) for t in trades)
    conn.execute(
        "UPDATE filings SET parse_method = 'vision', parse_status = ?, vision_attempts = vision_attempts + 1, "
        "vision_attempted_at = ?, vision_error = ? WHERE doc_id = ?",
        ("parsed" if clean else "needs_review", stamp(now), output.get("problems"), doc_id))
    conn.commit()
    return "read", len(trades)


def run(conn: sqlite3.Connection, *, doc_ids: list[str] | None = None, raw_root: Path | None = None,
        now: Callable[[], datetime] = utc_now, extract=extract_with_claude, dry_run: bool = False,
        out: Callable[[str], None] = print) -> Summary:
    summary = Summary()
    raw_root = raw_root or config.raw_dir()
    moment = now()
    for filing in candidates(conn, moment, doc_ids):
        files = files_for(filing, raw_root)
        if not files:
            summary.skipped_no_files += 1
            continue
        outcome, rows = read_filing(conn, filing, files, now=moment, extract=extract, dry_run=dry_run, out=out)
        setattr(summary, outcome, getattr(summary, outcome) + 1)
        summary.rows += rows
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--doc-id", action="append", help="read this scanned filing now (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="print the rows; write no trades (the read is logged)")
    args = parser.parse_args(argv)
    logs.setup()
    s = run(connect(), doc_ids=args.doc_id, dry_run=args.dry_run)
    log.info("Scanned filings: %d read (%d rows), %d unreadable, %d failed, %d without cached pages",
             s.read, s.rows, s.unreadable, s.failed, s.skipped_no_files)
    return 1 if s.failed else 0


if __name__ == "__main__":
    sys.exit(main())
