"""Historical backfill (Milestone 2.1): python -m pipeline.backfill --from 2020 [--to Y] [--chamber C] [--no-download]

Loads PTRs filed in the given years (default: through last year; the scheduled run polls the current one)
through the normal ingest code, marked as backfilled: their available_at (what D0 is measured from) is estimated
as after the close on the filing date, and they are never emailed. Then it parses whatever is pending and prints
the review queue.

Polite and resumable: the same 1 s pauses and failure stop as the scheduled run, cached files are reused, so an
interrupted backfill just continues. Holds the pipeline lock, so a scheduled run never overlaps it.
"""

import argparse
import logging
import sqlite3
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

from common import config
from common import http as polite
from common import log as logs
from db import connect
from pipeline.report import review_queue
from pipeline.run import single_run

log = logging.getLogger("pipeline.backfill")

CHAMBERS = ("house", "senate")


def run(
    conn: sqlite3.Connection,
    first_year: int,
    last_year: int,
    *,
    chambers: tuple[str, ...] = CHAMBERS,
    download: bool = True,
    raw_root: Path | None = None,
    house_http=None,
    senate_http=None,
) -> dict[str, dict]:
    """Backfills each chamber, then parses. Returns the stage summaries. Tests pass mock HTTP clients."""
    from ingest import house, senate
    from parse import run as parse_run

    results: dict[str, dict] = {}
    if "house" in chambers:
        with house_http or polite.client() as http:
            s = house.run(conn, http, list(range(first_year, last_year + 1)), download=download, backfill=True,
                          raw_root=raw_root)
        results["house"] = asdict(s)
    if "senate" in chambers:
        # By date received, ending with last_year: a new filing must be found by the live poll so it's alerted.
        with senate_http or polite.client() as http:
            s = senate.run(conn, http, since=date(first_year, 1, 1), until=date(last_year, 12, 31),
                           download=download, backfill=True, raw_root=raw_root)
        results["senate"] = asdict(s)
    results["parse"] = asdict(parse_run.run(conn, raw_root=raw_root))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--from", dest="first", type=int, required=True, help="first filing year, e.g. 2020")
    parser.add_argument("--to", dest="last", type=int, default=date.today().year - 1,
                        help="last filing year (default: last year)")
    parser.add_argument("--chamber", choices=CHAMBERS, help="only this chamber")
    parser.add_argument("--no-download", action="store_true", help="record filings without downloading files")
    args = parser.parse_args(argv)

    logs.setup()
    with single_run(config.db_path().parent / "pipeline.lock") as acquired:
        if not acquired:
            log.error("A pipeline run is in progress; try again when it finishes")
            return 1
        conn = connect()
        results = run(conn, args.first, args.last, chambers=(args.chamber,) if args.chamber else CHAMBERS,
                      download=not args.no_download)
    failed = []
    for name, summary in results.items():
        log.info("%s: %s", name, summary)
        failed += [f"{name} {src}" for src in summary.get("failed_sources", [])]
    print("\n".join(review_queue(conn)))
    if failed:
        log.error("Failed sources (re-run to retry): %s", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
