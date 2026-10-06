"""Email alerts for watchlist trades (Milestone 1.5): python -m alerts.run [--dry-run] [--since DATE]

Sends one email per filing (see alerts/rules.py for what qualifies), then records each alerted trade in
`alerts` (or the filing in `filing_alerts`) so nothing is sent twice. Buys carry the score, a suggested entry
(alerts/score.py: v2 from the nightly analytics, v1 rules before they exist) and a passive suggested exit
(exit_rules, M4.3), and high-score buys a research brief (M5.3) from the injected `research` callable
(agents/researcher.brief, wired in by pipeline/run.py: alerts never imports agents; a failure there only drops the
brief). Then one exit email per position you logged whose exit rule fired (alerts/positions.py,
recorded in exit_alerts): exit emails only ever come from your own positions. A failed send records nothing and is
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

from alerts import email, positions, rules
from alerts.email import Line, Message
from alerts.score import load_model, score, suggested_entry
from common import log as logs
from db import connect

log = logging.getLogger("alerts.run")

START_SOURCE = "alerts.start"  # source_state row whose checked_at is the alerts start time


def load_exit(conn: sqlite3.Connection) -> str | None:
    """The passive exit line for buy emails, from analytics/exits.py's recommendation (None before it has run)."""
    row = conn.execute("SELECT description, confidence, reason FROM exit_rules WHERE recommended = 1").fetchone()
    if row is None:
        return None
    text = row["description"][0].lower() + row["description"][1:]
    return f"If you buy: suggested exit is to {text} (confidence {row['confidence']}: {row['reason']})"


@dataclass
class Summary:
    since: str = ""
    emails: int = 0
    trades: int = 0
    scans: int = 0
    exits: int = 0  # exit emails for positions you logged
    researched: int = 0  # buy emails that carry a research brief
    research_failed: int = 0  # ...of which the news summary failed (facts only)
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
    research: Callable[[sqlite3.Connection, list[tuple[dict, int]]], object | None] | None = None,
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
    model = load_model(conn) if found else None
    exit_line = load_exit(conn) if found else None
    for _, group in groupby(found, key=lambda item: item[1]["doc_id"]):
        lines = []
        for rule, row in group:
            trade = dict(row)
            if rule == rules.WATCHLIST_BUY:
                points, reasons = score(trade, model)
                entry, exit_text = suggested_entry(trade, model), exit_line
            else:
                points, reasons, entry, exit_text = None, [], None, None
            lines.append(Line(rule, trade, points, reasons, entry, exit_text))

        def record(lines=lines):
            conn.executemany(
                "INSERT INTO alerts (trade_id, sent_at, score, suggested_entry, suggested_exit, rule) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(ln.trade["trade_id"], stamp, ln.score, ln.entry, ln.exit, ln.rule) for ln in lines],
            )

        brief = None
        buys = [(ln.trade, ln.score) for ln in lines if ln.rule == rules.WATCHLIST_BUY]
        if research and buys:
            try:
                brief = research(conn, buys)  # decides itself whether the scores are high enough
            except Exception:  # never let research hold back an alert
                log.warning("Research for %s failed; sending without it", lines[0].trade["doc_id"], exc_info=True)

        if deliver(email.compose_trades(lines, brief), record):
            summary.emails += 1
            summary.trades += len(lines)
            summary.researched += brief is not None
            summary.research_failed += brief is not None and not getattr(brief, "narrative", None)

    for filing in rules.scanned_filings(conn, summary.since):
        def record(doc_id=filing["doc_id"]):
            conn.execute("INSERT INTO filing_alerts (doc_id, kind, sent_at) VALUES (?, 'scanned', ?)", (doc_id, stamp))

        if deliver(email.compose_scan(dict(filing)), record):
            summary.emails += 1
            summary.scans += 1

    due, warnings = positions.due(conn)
    for warning in warnings:
        log.warning("Exit watch: %s", warning)
    for alert in due:
        def record(a=alert):
            conn.execute(
                "INSERT INTO exit_alerts (position_id, rule, triggered_on, reason, price, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (a.position["position_id"], a.position["exit_rule"], a.day, a.reason, a.price, stamp))

        if deliver(email.compose_exit(alert), record):
            summary.emails += 1
            summary.exits += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the emails; send and record nothing")
    parser.add_argument("--since", type=date.fromisoformat,
                        help="alert on filings first seen on/after this date (default: the alerts start time)")
    parser.add_argument("--no-research", action="store_true", help="no research briefs on high-score buys")
    parser.add_argument("--research", action="store_true", help="with --dry-run: research anyway (uses Claude)")
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
    research = None
    if not args.no_research and (args.research or not args.dry_run):
        from agents import researcher  # the entry point wires it in; the alerts code itself doesn't import agents

        research = researcher.brief
    s = run(conn, send, since=args.since.isoformat() if args.since else None, dry_run=args.dry_run,
            research=research)
    log.info("Alerts since %s: %d email(s), %d trade(s), %d scanned filing(s), %d position exit(s), %d researched; "
             "%d failed%s", s.since, s.emails, s.trades, s.scans, s.exits, s.researched, s.failures,
             " (dry run)" if args.dry_run else "")
    return 1 if s.failures else 0


if __name__ == "__main__":
    sys.exit(main())
