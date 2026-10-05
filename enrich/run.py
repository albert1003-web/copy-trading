"""Enrich parsed trades (Milestone 1.4): python -m enrich.run [--offline]

Each pass (safe to re-run; everything is recomputed):
  1. Refresh the reference files if they're over a week old (legislators, committees, Nasdaq/NYSE symbol lists),
     and download any pinned committee snapshot not cached yet.
  2. Upsert `members`; match every filing's filer to a member (filings.member_id, trades.member_id).
  3. Validate tickers (trades.symbol, ticker_status, is_etf) and compute filing_delay_days.
  4. Sector, industry and market-cap bucket from `securities` (filled by the nightly stage) and `prices`.
  5. Committee assignments per Congress, and trades.committee_relevant.
"""

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

from common import config
from common import http as polite
from common import log as logs
from db import connect
from enrich import committees, members, reference, securities, tickers

log = logging.getLogger("enrich.run")


@dataclass
class Summary:
    members: int = 0
    unmatched_filers: list[str] = field(default_factory=list)
    ticker_status: dict[str, int] = field(default_factory=dict)
    mcap_buckets: dict[str, int] = field(default_factory=dict)
    committee_memberships: int = 0
    committee_relevant: int = 0


def run(
    conn: sqlite3.Connection,
    http: httpx.Client | None,
    raw_root: Path | None = None,
    *,
    today: date | None = None,
    snapshots: dict[int, str] | None = None,
) -> Summary:
    """Pass http=None to use the cached reference files without checking for updates."""
    raw_root = raw_root or config.raw_dir()
    today = today or date.today()
    pins = committees.snapshots() if snapshots is None else snapshots
    if http is not None:
        reference.refresh(http, raw_root)
        committees.refresh_snapshots(http, raw_root, pins)
    people = members.load(*reference.legislators(raw_root))
    members.upsert(conn, people)
    unmatched = members.assign(conn, people, members.aliases())
    counts = tickers.assign(conn, reference.symbols(raw_root), tickers.aliases(), tickers.alias_until())
    buckets = securities.assign(conn)
    seats = committees.load(raw_root, today, pins)
    committees.store(conn, seats, today)
    relevant = committees.assign(conn, committees.sector_map())
    conn.commit()
    return Summary(members=len(people), unmatched_filers=unmatched, ticker_status=dict(counts),
                   mcap_buckets=dict(buckets), committee_memberships=len(seats), committee_relevant=relevant)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--offline", action="store_true", help="use cached reference files; don't check for updates")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    try:
        if args.offline:
            s = run(conn, None)
        else:
            with polite.client() as http:
                s = run(conn, http)
    except (RuntimeError, FileNotFoundError) as e:
        log.error("Enrichment failed: %s", e)
        return 1
    statuses = ", ".join(f"{k} {v}" for k, v in sorted(s.ticker_status.items()))
    log.info("Enriched: %d members; %d unmatched filer(s); tickers: %s; %d committee seats, %d committee-relevant "
             "trade(s); size buckets: %s", s.members, len(s.unmatched_filers), statuses or "no trades",
             s.committee_memberships, s.committee_relevant, s.mcap_buckets)
    return 1 if s.unmatched_filers else 0


if __name__ == "__main__":
    sys.exit(main())
