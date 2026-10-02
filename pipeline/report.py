"""Detection-latency report (Milestone 1.6): python -m pipeline.report [--days 14]

  coverage   scheduled runs, and gaps longer than the schedule allows (the Mac asleep or off)
  detection  days from a filing's date to when we first saw it, per chamber; only filings first seen
             after scheduling started, so the initial backfill doesn't distort it
  house      whether the daily index lags the live search page (decides the open M1.1 item)
  alerts     minutes from first seeing a filing to emailing it
  review     filings and parse status per chamber and year (the needs_review queue after a backfill)
  prices     price coverage per symbol (ok | partial | missing) and the biggest gaps
"""

import argparse
import sqlite3
import statistics
import sys
from datetime import UTC, date, datetime, timedelta

from db import connect
from pipeline.run import interval, parse_stamp

GAP_SLACK = timedelta(minutes=15)  # tolerated lateness beyond the schedule's interval


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def coverage(conn: sqlite3.Connection, since: datetime) -> list[str]:
    runs = conn.execute(
        "SELECT started_at, status FROM pipeline_runs WHERE started_at >= ? ORDER BY run_id",
        (since.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    ).fetchall()
    if not runs:
        return ["Coverage: no runs yet. Install the schedule: python -m pipeline.schedule install"]
    ok = sum(r["status"] == "ok" for r in runs)
    lines = [f"Coverage: {len(runs)} runs since {runs[0]['started_at']}: {ok} ok, {len(runs) - ok} failed"]
    gaps = []
    times = [parse_stamp(r["started_at"]) for r in runs] + [datetime.now(UTC)]
    for earlier, later in zip(times, times[1:], strict=False):
        if later - earlier > interval(later) + GAP_SLACK:
            gaps.append((later - earlier, earlier, later))
    if gaps:
        lines.append(f"  {len(gaps)} gap(s) longer than the schedule (Mac asleep/off?), longest first:")
        for length, start, end in sorted(gaps, reverse=True)[:10]:
            hours = length.total_seconds() / 3600
            lines.append(f"    {hours:5.1f} h  {start:%a %m-%d %H:%M} -> {end:%a %m-%d %H:%M} UTC")
    else:
        lines.append("  No gaps: every run came on schedule.")
    return lines


def _scheduled_since(conn: sqlite3.Connection, since: datetime) -> str:
    """The later of the report window and the first pipeline run (filings seen before it are the backfill)."""
    first = conn.execute("SELECT MIN(started_at) FROM pipeline_runs").fetchone()[0]
    return max(first or "9999", since.strftime("%Y-%m-%dT%H:%M:%SZ"))


def detection(conn: sqlite3.Connection, since: datetime) -> list[str]:
    first = conn.execute("SELECT MIN(started_at) FROM pipeline_runs").fetchone()[0]
    if first is None:
        return ["Detection: no scheduled runs yet."]
    start = _scheduled_since(conn, since)
    lines = [f"Detection lag (filing date -> first seen), filings first seen since {start}:"]
    for chamber in ("house", "senate"):
        rows = conn.execute(
            "SELECT filing_date, first_seen_at FROM filings WHERE chamber = ? AND first_seen_at >= ? "
            "AND filing_date IS NOT NULL AND available_basis = 'seen'",
            (chamber, start),
        ).fetchall()
        days = [(parse_stamp(r["first_seen_at"]).date() - date.fromisoformat(r["filing_date"])).days for r in rows]
        if not days:
            lines.append(f"  {chamber:6} no new filings")
            continue
        p90 = sorted(days)[max(0, int(len(days) * 0.9) - 1)]
        lines.append(f"  {chamber:6} {len(days)} filings: same day {_pct(sum(d <= 0 for d in days), len(days))}, "
                     f"next day {_pct(sum(d == 1 for d in days), len(days))}, "
                     f"median {statistics.median(days):g} d, p90 {p90} d")
    return lines


def alert_latency(conn: sqlite3.Connection, since: datetime) -> list[str]:
    rows = conn.execute(
        """SELECT a.sent_at, MIN(f.first_seen_at) AS first_seen FROM alerts a
           JOIN trades t ON t.trade_id = a.trade_id JOIN filings f ON f.doc_id = t.doc_id
           WHERE f.first_seen_at >= ? GROUP BY a.sent_at, f.doc_id""",
        (_scheduled_since(conn, since),),
    ).fetchall()
    minutes = [(parse_stamp(r["sent_at"]) - parse_stamp(r["first_seen"])).total_seconds() / 60 for r in rows]
    minutes = [m for m in minutes if m >= 0]
    if not minutes:
        return ["Alerts: none sent for filings first seen since scheduling started."]
    return [f"Alerts: {len(minutes)} email(s); first seen -> emailed median {statistics.median(minutes):.0f} min, "
            f"max {max(minutes):.0f} min"]


def review_queue(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT f.chamber, f.filing_year AS year, COUNT(*) AS filings,
               SUM(f.parse_status = 'parsed') AS parsed,
               SUM(f.parse_status = 'needs_review' AND f.doc_format = 'scanned') AS scanned,
               SUM(f.parse_status = 'needs_review' AND f.doc_format <> 'scanned') AS review,
               SUM(f.parse_status = 'failed') AS failed,
               SUM(f.parse_status = 'pending') AS pending,
               SUM(f.available_basis = 'filed') AS backfilled,
               (SELECT COUNT(*) FROM trades t JOIN filings g ON g.doc_id = t.doc_id
                WHERE g.chamber = f.chamber AND g.filing_year IS f.filing_year) AS trades
        FROM filings f GROUP BY f.chamber, f.filing_year ORDER BY f.chamber, f.filing_year
        """
    ).fetchall()
    if not rows:
        return ["Review queue: no filings yet."]
    lines = ["Review queue (filings by chamber and filing year):",
             "  chamber year  filings parsed scanned review failed pending backfilled  trades"]
    for r in rows:
        lines.append(f"  {r['chamber']:7} {r['year'] or '?':>4} {r['filings']:8} {r['parsed']:6} {r['scanned']:7} "
                     f"{r['review']:6} {r['failed']:6} {r['pending']:7} {r['backfilled']:10} {r['trades']:7}")
    return lines


def report(conn: sqlite3.Connection, days: int = 14) -> str:
    from ingest.house import lag_report
    from prices.fetch import gaps

    since = datetime.now(UTC) - timedelta(days=days)
    sections = [coverage(conn, since), detection(conn, since), ["House index vs search page:", lag_report(conn)],
                alert_latency(conn, since), review_queue(conn), gaps(conn, limit=15)]
    return "\n\n".join("\n".join(lines) for lines in sections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=14, help="window to report on (default 14)")
    args = parser.parse_args(argv)
    print(report(connect(), args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
