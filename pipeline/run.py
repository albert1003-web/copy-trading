"""One scheduled pipeline pass (Milestone 1.6): python -m pipeline.run [--force] [--nightly]

Runs every stage in order (ingest House, ingest Senate, parse, enrich, alerts) in one process. Once per weekday
evening it also runs the nightly stages (prices, outcomes, open inflation, leaderboard, factors, exits, securities;
M2.2/2.3, M3.1-3.5, M4.2): the first run at or after 18:00 ET that finds no successful nightly for that day runs them,
so a night missed while the Mac slept is caught up on wake. Every stage runs even if an earlier one failed, so one
source's outage never blocks the other's alerts. Each run is recorded in `pipeline_runs`, which the app's Pipeline
tab shows (failures are shown there, never emailed).

  failure  an exception, a source that couldn't be fetched, missing Gmail settings, an alert not sent
  warning  downloads that will retry, newly failed parses, unmatched filers (shown, never emailed)

Polite by construction (hard rule 6): a run is skipped unless the last one started at least 29 minutes ago
on weekdays or 119 minutes ago on weekends (America/New_York). --force skips that check. launchd starts this
every 30 minutes (python -m pipeline.schedule install). Only one run happens at a time (a file lock).
"""

import argparse
import fcntl
import json
import logging
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from common import config
from common import http as polite
from common import log as logs
from db import connect

log = logging.getLogger("pipeline.run")

EASTERN = ZoneInfo("America/New_York")
WEEKDAY_INTERVAL = timedelta(minutes=29)  # launchd fires every 30 min; allow a little jitter
WEEKEND_INTERVAL = timedelta(minutes=119)
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class StageResult:
    summary: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


Stage = Callable[[sqlite3.Connection], StageResult]


@dataclass
class RunResult:
    run_id: int
    status: str
    stages: dict[str, dict]
    warnings: list[str]


# --- stages ---------------------------------------------------------------------------------------


def _summary(value) -> dict:
    return asdict(value) if is_dataclass(value) else dict(value or {})


def ingest_house(conn: sqlite3.Connection) -> StageResult:
    from ingest import house

    with polite.client() as http:
        s = house.run(conn, http, house.default_years(date.today()))
    warnings = [f"house: {s.download_failures} PDF download(s) failed (will retry)"] if s.download_failures else []
    return StageResult(_summary(s), [f"house {src} failed" for src in s.failed_sources], warnings)


def ingest_senate(conn: sqlite3.Connection) -> StageResult:
    from ingest import senate

    with polite.client() as http:
        s = senate.run(conn, http)
    warnings = [f"senate: {s.download_failures} report download(s) failed (will retry)"] if s.download_failures else []
    return StageResult(_summary(s), [f"senate {src} failed" for src in s.failed_sources], warnings)


def parse_filings(conn: sqlite3.Connection) -> StageResult:
    from parse import run as parse_run

    s = parse_run.run(conn)
    warnings = []
    if s.failed:
        warnings.append(f"parse: {s.failed} filing(s) failed to parse")
    if s.needs_review:
        warnings.append(f"parse: {s.needs_review} new filing(s) need review")
    return StageResult(_summary(s), [], warnings)


def enrich_trades(conn: sqlite3.Connection) -> StageResult:
    from enrich import run as enrich_run

    with polite.client() as http:
        s = enrich_run.run(conn, http)
    warnings = [f"enrich: no member matches filer {name!r}" for name in s.unmatched_filers]
    return StageResult(_summary(s), [], warnings)


def send_alerts(conn: sqlite3.Connection) -> StageResult:
    from alerts import email
    from alerts import run as alerts_run

    s = alerts_run.run(conn, email.gmail_sender())  # ConfigError (no Gmail settings) fails the stage
    errors = [f"alerts: {s.failures} email(s) failed to send (will retry)"] if s.failures else []
    return StageResult(_summary(s), errors, [])


def fetch_prices(conn: sqlite3.Connection) -> StageResult:
    from prices import fetch

    s = fetch.run(conn, fetch.YahooSource())
    partial = s.failed_batches and not s.errors
    warnings = [f"prices: {s.failed_batches} batch(es) failed (retried next night)"] if partial else []
    return StageResult(_summary(s), s.errors, warnings)


STAGES: list[tuple[str, Stage]] = [
    ("ingest_house", ingest_house),
    ("ingest_senate", ingest_senate),
    ("parse", parse_filings),
    ("enrich", enrich_trades),
    ("alerts", send_alerts),
]


def fetch_securities(conn: sqlite3.Connection) -> StageResult:
    from enrich import securities

    s = securities.refresh(conn, securities.YahooInfo())
    warnings = [f"securities: {s.failed} symbol(s) failed (retried next night)"] if s.failed else []
    return StageResult(_summary(s), [], warnings)


def compute_outcomes(conn: sqlite3.Connection) -> StageResult:
    from analytics import outcomes

    s = outcomes.run(conn)
    return StageResult(_summary(s), s.errors, [])


def compute_open_inflation(conn: sqlite3.Connection) -> StageResult:
    from analytics import open_inflation

    s = open_inflation.run(conn)
    warnings = [f"open_inflation: unknown member_id {m} in high_attention_members.csv" for m in s.unknown_attention]
    return StageResult(_summary(s), s.errors, warnings)


def compute_leaderboard(conn: sqlite3.Connection) -> StageResult:
    from analytics import leaderboard

    s = leaderboard.run(conn)
    return StageResult(_summary(s), s.errors, [])


def compute_factors(conn: sqlite3.Connection) -> StageResult:
    from analytics import factors

    s = factors.run(conn)
    return StageResult(_summary(s), s.errors, [])


def compute_exits(conn: sqlite3.Connection) -> StageResult:
    from analytics import exits

    s = exits.run(conn)
    return StageResult(_summary(s), s.errors, [])


NIGHTLY_STAGES: list[tuple[str, Stage]] = [
    ("prices", fetch_prices),
    ("outcomes", compute_outcomes),  # after prices, so it measures on tonight's bars
    ("open_inflation", compute_open_inflation),  # reads outcomes' d0_date / copyable
    ("leaderboard", compute_leaderboard),  # reads outcomes' returns
    ("factors", compute_factors),  # v2 alert score inputs; reads outcomes' returns
    ("exits", compute_exits),  # walk-forward exit backtests; reads outcomes and leaderboard inputs
    ("securities", fetch_securities),
]
NIGHTLY_SOURCE = "pipeline.nightly"  # source_state row whose checked_at is the last trading day done (ET)
NIGHTLY_HOUR = 18  # ET; after the close, once Yahoo has the day's final bar


# --- scheduling -----------------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TIME_FORMAT)


def parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=UTC)


def interval(moment: datetime) -> timedelta:
    """How long to wait between runs at this moment: 30 minutes on weekdays, 2 hours on weekends (ET)."""
    return WEEKEND_INTERVAL if moment.astimezone(EASTERN).weekday() >= 5 else WEEKDAY_INTERVAL


def due(last_started: str | None, now: datetime) -> bool:
    return last_started is None or now - parse_stamp(last_started) >= interval(now)


def nightly_day(now: datetime) -> date:
    """The latest weekday whose 18:00 ET has passed: the day the next nightly run covers."""
    et = now.astimezone(EASTERN)
    day = et.date() if et.hour >= NIGHTLY_HOUR else et.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def nightly_due(conn: sqlite3.Connection, now: datetime) -> bool:
    row = conn.execute("SELECT checked_at FROM source_state WHERE source = ?", (NIGHTLY_SOURCE,)).fetchone()
    return row is None or row[0] < nightly_day(now).isoformat()


def nightly_done(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(
        "INSERT INTO source_state (source, checked_at) VALUES (?, ?) "
        "ON CONFLICT (source) DO UPDATE SET checked_at = excluded.checked_at",
        (NIGHTLY_SOURCE, nightly_day(now).isoformat()),
    )
    conn.commit()


@contextmanager
def single_run(lock_path: Path) -> Iterator[bool]:
    """Yields True if this process holds the pipeline lock, False if another run has it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --- one run --------------------------------------------------------------------------------------


def run(
    conn: sqlite3.Connection,
    *,
    stages: list[tuple[str, Stage]] | None = None,
    nightly_stages: list[tuple[str, Stage]] | None = None,
    force: bool = False,
    nightly: bool | None = None,
    now: Callable[[], datetime] = utc_now,
) -> RunResult | None:
    """Runs every stage, plus the nightly ones when due (nightly=True forces them, False skips them), and records
    the run. Returns None if a run isn't due yet. Tests pass their own stages (and then no nightly stages unless
    they pass those too)."""
    started = now()
    last = conn.execute("SELECT started_at FROM pipeline_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    if not force and not due(last[0] if last else None, started):
        log.info("Not due yet (last run %s); skipping", last[0])
        return None

    run_id = conn.execute(
        "INSERT INTO pipeline_runs (started_at, status) VALUES (?, 'running')", (stamp(started),)
    ).lastrowid
    conn.commit()

    if nightly_stages is None:
        nightly_stages = NIGHTLY_STAGES if stages is None else []
    run_nightly = bool(nightly_stages) and (nightly if nightly is not None else nightly_due(conn, started))
    planned = list(stages if stages is not None else STAGES) + (nightly_stages if run_nightly else [])

    results: dict[str, dict] = {}
    warnings: list[str] = []
    for name, stage in planned:
        try:
            result = stage(conn)
        except Exception as e:  # one broken stage must not stop the others
            conn.rollback()
            log.exception("Stage %s crashed", name)
            result = StageResult(errors=[f"{name}: {type(e).__name__}: {e}"])
        results[name] = {"ok": not result.errors, "errors": result.errors, "summary": result.summary}
        warnings += result.warnings
        for error in result.errors:
            log.error("%s", error)

    if run_nightly and all(results[name]["ok"] for name, _stage in nightly_stages):
        nightly_done(conn, started)
    status = "ok" if all(r["ok"] for r in results.values()) else "failed"
    conn.execute(
        "UPDATE pipeline_runs SET finished_at = ?, status = ?, stages = ?, warnings = ? WHERE run_id = ?",
        (stamp(now()), status, json.dumps(results, default=str), json.dumps(warnings), run_id),
    )
    conn.commit()
    return RunResult(run_id, status, results, warnings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true", help="run even if the last run was too recent")
    parser.add_argument("--nightly", action="store_true",
                        help="also run the nightly stages (prices, analytics, securities) now")
    args = parser.parse_args(argv)

    logs.setup()
    with single_run(config.db_path().parent / "pipeline.lock") as acquired:
        if not acquired:
            log.info("Another pipeline run is in progress; exiting")
            return 0
        conn = connect()
        result = run(conn, force=args.force, nightly=True if args.nightly else None)
    if result is None:
        return 0
    failing = [name for name, r in result.stages.items() if not r["ok"]]
    log.info("Pipeline run %d: %s%s; %d warning(s)", result.run_id, result.status,
             f" ({', '.join(failing)})" if failing else "", len(result.warnings))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
