"""Email alerts for watchlist trades (Milestone 1.5): python -m alerts.run [--dry-run] [--since DATE]

Sends one email per filing (see alerts/rules.py for what qualifies), then records each alerted trade in
`alerts` (or the filing in `filing_alerts`) so nothing is sent twice. A failed send records nothing and is
retried on the next run.

The first run stores an alerts start time and only alerts on filings first seen after it, so the
backlog already in the database never floods the inbox. --since overrides it (testing, catch-up).
"""

import argparse
import logging
import smtplib
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import groupby

from alerts import email, rules
from alerts.email import Line, Message
from alerts.score import score
from common import log as logs
from db import connect

log = logging.getLogger("alerts.run")

START_SOURCE = "alerts.start"  # source_state row whose checked_at is the alerts start time


@dataclass
class Summary:
    since: str = ""
    emails: int = 0
    trades: int = 0
    scans: int = 0
    failures: int = 0


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_time(conn: sqlite3.Connection, now: str, *, save: bool = True) -> str:
    """When alerting began; set on the first real (not dry) run."""
    row = conn.execute("SELECT checked_at FROM source_state WHERE source = ?", (START_SOURCE,)).fetchone()
    if row:
        return row[0]
    if not save:
        return now
    conn.execute("INSERT INTO source_state (source, checked_at) VALUES (?, ?)", (START_SOURCE, now))
    conn.commit()
    log.info("Alerts start now (%s); filings seen before this won't be emailed", now)
    return now


def run(
    conn: sqlite3.Connection,
    send: Callable[[Message], None] | None,
    *,
    since: str | None = None,
    dry_run: bool = False,
    now: Callable[[], str] = utc_now,
    out: Callable[[str], None] = print,
) -> Summary:
    stamp = now()
    summary = Summary(since=since or start_time(conn, stamp, save=not dry_run))

    def deliver(message: Message, record: Callable[[], None]) -> bool:
        if dry_run:
            out(f"--- {message.subject}\n{message.text}\n")
            return True
        try:
            send(message)
        except (smtplib.SMTPException, OSError) as e:
            summary.failures += 1
            log.error("Sending %r failed: %s (will retry next run)", message.subject, e)
            return False
        record()
        conn.commit()
        return True

    found = rules.trades(conn, summary.since)
    for _, group in groupby(found, key=lambda item: item[1]["doc_id"]):
        lines = []
        for rule, row in group:
            trade = dict(row)
            points, reasons = score(trade) if rule == rules.WATCHLIST_BUY else (None, [])
            lines.append(Line(rule, trade, points, reasons))

        def record(lines=lines):
            conn.executemany(
                "INSERT INTO alerts (trade_id, sent_at, score, rule) VALUES (?, ?, ?, ?)",
                [(ln.trade["trade_id"], stamp, ln.score, ln.rule) for ln in lines],
            )

        if deliver(email.compose_trades(lines), record):
            summary.emails += 1
            summary.trades += len(lines)

    for filing in rules.scanned_filings(conn, summary.since):
        def record(doc_id=filing["doc_id"]):
            conn.execute("INSERT INTO filing_alerts (doc_id, kind, sent_at) VALUES (?, 'scanned', ?)", (doc_id, stamp))

        if deliver(email.compose_scan(dict(filing)), record):
            summary.emails += 1
            summary.scans += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the emails; send and record nothing")
    parser.add_argument("--since", type=date.fromisoformat,
                        help="alert on filings first seen on/after this date (default: the alerts start time)")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    send = None
    if not args.dry_run:
        try:
            send = email.gmail_sender()
        except email.ConfigError as e:
            log.error("%s", e)
            return 1
    s = run(conn, send, since=args.since.isoformat() if args.since else None, dry_run=args.dry_run)
    log.info("Alerts since %s: %d email(s), %d trade(s), %d scanned filing(s); %d failed%s",
             s.since, s.emails, s.trades, s.scans, s.failures, " (dry run)" if args.dry_run else "")
    return 1 if s.failures else 0


if __name__ == "__main__":
    sys.exit(main())
