"""Member leaderboard (Milestone 3.3, F6): python -m analytics.leaderboard [--report [--horizon H]]

How did each member's buys do after they became public, compared with the S&P 500?

  input        copyable BUYs (trade_outcomes.copyable = 1) with a member, from D0 (analytics.outcomes). A horizon
               counts once it has matured (abn_ret_h filled). SPY's return over the same window = ret_h - abn_ret_h.
  unit         the filing: its trades share D0 and the same market moves, so each filing is one observation (the
               mean over its trades). n_filings is the sample size; n_trades is context.
  per member   for h in {1, 5, 10, 20, 60}: mean_ret (average return of the buys), mean_spy_ret (SPY over the
               same windows), mean_abn_ret (the excess: mean_ret - mean_spy_ret), median_abn_ret, hit_rate (share
               of filings that beat SPY) and shrunk_score (empirical-Bayes mean excess, analytics.stats.shrink, so
               a member with 3 lucky filings can't top the list). The score uses filing excesses clipped at the
               1st/99th percentile of all filings at that horizon: one +300% filing otherwise inflates the noise
               estimate until every member shrinks to the same score. The displayed means are not clipped.
               Into member_horizon_stats.
  ranking      member_scores holds h = 20: members with at least MIN_N filings ranked by shrunk_score (1 = best),
               the rest unranked (rank NULL); ties go to the higher mean excess. consistency = share of calendar
               years (by D0, with >= 3 filings) in which the member's mean excess was > 0; NULL with fewer than 2
               such years.
  snapshots    as_of = today's ET date. A re-run on the same day replaces that day; earlier snapshots are kept.

Free price data drops delisted tickers: results carry survivorship bias until a paid provider.
"""

import argparse
import logging
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from analytics.stats import sample, shrink
from common import log as logs
from db import connect

log = logging.getLogger("analytics.leaderboard")

HORIZONS = (1, 5, 10, 20, 60)
RANK_HORIZON = 20
MIN_N = 20  # filings at RANK_HORIZON to be ranked
YEAR_MIN_FILINGS = 3  # filings in a year for it to count toward consistency
CLIP = 0.01  # score: clip filing excesses at these percentiles of all filings (1st / 99th)
EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Filing:
    year: str
    n_trades: int
    ret: float
    spy: float
    abn: float


@dataclass
class Summary:
    as_of: str = ""
    members: int = 0  # members with data at RANK_HORIZON
    ranked: int = 0
    filings: dict[str, int] = field(default_factory=dict)  # filings per horizon
    errors: list[str] = field(default_factory=list)


def today_et() -> date:
    return datetime.now(EASTERN).date()


def filings(conn: sqlite3.Connection, h: int) -> dict[str, list[Filing]]:
    """Member -> one Filing per filing with matured copyable BUYs at horizon h."""
    trades: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute(
        f"""
        SELECT t.member_id, t.doc_id, o.d0_date, o.ret_{h} AS ret, o.abn_ret_{h} AS abn
        FROM trade_outcomes o JOIN trades t ON t.trade_id = o.trade_id
        WHERE o.copyable = 1 AND t.member_id IS NOT NULL AND o.ret_{h} IS NOT NULL AND o.abn_ret_{h} IS NOT NULL
        """
    ):
        trades[(r["member_id"], r["doc_id"])].append(r)
    result: dict[str, list[Filing]] = defaultdict(list)
    for (member_id, _doc), rows in trades.items():
        ret = statistics.fmean(r["ret"] for r in rows)
        abn = statistics.fmean(r["abn"] for r in rows)
        result[member_id].append(Filing(rows[0]["d0_date"][:4], len(rows), ret, ret - abn, abn))
    return result


def consistency(member_filings: list[Filing]) -> float | None:
    by_year: dict[str, list[float]] = defaultdict(list)
    for f in member_filings:
        by_year[f.year].append(f.abn)
    years = [v for v in by_year.values() if len(v) >= YEAR_MIN_FILINGS]
    if len(years) < 2:
        return None
    return sum(statistics.fmean(v) > 0 for v in years) / len(years)


def clip_bounds(values: list[float], share: float = CLIP) -> tuple[float, float]:
    ordered = sorted(values)
    last = len(ordered) - 1
    return ordered[round(share * last)], ordered[round((1 - share) * last)]


def member_stats(by_member: dict[str, list[Filing]]) -> dict[str, dict]:
    if not by_member:
        return {}
    low, high = clip_bounds([f.abn for fs in by_member.values() for f in fs])
    shrunk = shrink({m: sample([min(max(f.abn, low), high) for f in fs]) for m, fs in by_member.items()})
    stats = {}
    for m, fs in by_member.items():
        mean_ret = statistics.fmean(f.ret for f in fs)
        mean_spy = statistics.fmean(f.spy for f in fs)
        stats[m] = {
            "n_filings": len(fs),
            "n_trades": sum(f.n_trades for f in fs),
            "mean_ret": mean_ret,
            "mean_spy_ret": mean_spy,
            "mean_abn_ret": mean_ret - mean_spy,
            "median_abn_ret": statistics.median(f.abn for f in fs),
            "hit_rate": sum(f.abn > 0 for f in fs) / len(fs),
            "shrunk_score": shrunk[m],
        }
    return stats


def run(conn: sqlite3.Connection, *, today: date | None = None) -> Summary:
    as_of = (today or today_et()).isoformat()
    summary = Summary(as_of=as_of)
    horizon_rows: list[tuple] = []
    score_rows: list[tuple] = []
    for h in HORIZONS:
        by_member = filings(conn, h)
        summary.filings[str(h)] = sum(len(fs) for fs in by_member.values())
        stats = member_stats(by_member)
        horizon_rows += [
            (m, as_of, h, s["n_filings"], s["n_trades"], s["mean_ret"], s["mean_spy_ret"], s["mean_abn_ret"],
             s["median_abn_ret"], s["hit_rate"], s["shrunk_score"])
            for m, s in stats.items()
        ]
        if h != RANK_HORIZON:
            continue
        eligible = sorted((m for m, s in stats.items() if s["n_filings"] >= MIN_N),
                          key=lambda m: (stats[m]["shrunk_score"], stats[m]["mean_abn_ret"]), reverse=True)
        ranks = {m: i for i, m in enumerate(eligible, 1)}
        summary.members, summary.ranked = len(stats), len(ranks)
        score_rows = [
            (m, as_of, s["n_trades"], s["mean_abn_ret"], s["hit_rate"], s["shrunk_score"], ranks.get(m), h,
             s["n_filings"], s["mean_ret"], s["mean_spy_ret"], s["median_abn_ret"], consistency(by_member[m]))
            for m, s in stats.items()
        ]

    with conn:  # one transaction: today's snapshot replaced whole
        conn.execute("DELETE FROM member_horizon_stats WHERE as_of = ?", (as_of,))
        conn.execute("DELETE FROM member_scores WHERE as_of = ?", (as_of,))
        conn.executemany(
            "INSERT INTO member_horizon_stats (member_id, as_of, horizon, n_filings, n_trades, mean_ret, mean_spy_ret, "
            "mean_abn_ret, median_abn_ret, hit_rate, shrunk_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            horizon_rows)
        conn.executemany(
            "INSERT INTO member_scores (member_id, as_of, n_trades, mean_abn_ret, hit_rate, shrunk_score, rank, "
            "horizon, n_filings, mean_ret, mean_spy_ret, median_abn_ret, consistency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            score_rows)
    return summary


# --- report -------------------------------------------------------------------------------------------


def report(conn: sqlite3.Connection, horizon: int = RANK_HORIZON, limit: int = 30) -> list[str]:
    as_of = conn.execute("SELECT MAX(as_of) FROM member_horizon_stats").fetchone()[0]
    if not as_of:
        return ["Leaderboard: none yet. Run: python -m analytics.leaderboard (after analytics.outcomes)"]
    rows = conn.execute(
        """
        SELECT h.*, m.name, s.rank, s.consistency
        FROM member_horizon_stats h JOIN members m ON m.member_id = h.member_id
        LEFT JOIN member_scores s ON s.member_id = h.member_id AND s.as_of = h.as_of
        WHERE h.as_of = ? AND h.horizon = ?
        """,
        (as_of, horizon),
    ).fetchall()
    ranked = [r for r in rows if r["n_filings"] >= MIN_N]
    ranked.sort(key=lambda r: (r["shrunk_score"], r["mean_abn_ret"]), reverse=True)
    unranked = sorted((r for r in rows if r["n_filings"] < MIN_N), key=lambda r: -r["n_filings"])

    def line(i, r) -> str:
        cons = "-" if r["consistency"] is None or horizon != RANK_HORIZON else f"{r['consistency']:.0%}"
        return (f"  {i:>4} {r['name'][:24]:24} {r['n_filings']:5} {r['n_trades']:6}  {r['mean_ret']:+7.2%}  "
                f"{r['mean_spy_ret']:+7.2%}  {r['mean_abn_ret']:+7.2%}  {r['median_abn_ret']:+7.2%}  "
                f"{r['hit_rate']:5.0%}  {cons:>5}  {r['shrunk_score']:+7.2%}")

    lines = [f"Leaderboard as of {as_of}, h = {horizon} trading days from D0 (copyable buys, one observation per "
             f"filing; ranked: >= {MIN_N} filings)",
             "     #  member                   filings trades  avg ret  S&P 500   excess   median"
             "    hit  cons.    score"]
    lines += [line(i, r) for i, r in enumerate(ranked[:limit], 1)]
    if len(ranked) > limit:
        lines.append(f"  ... {len(ranked) - limit} more ranked")
    lines.append(f"Unranked (< {MIN_N} filings), most filings first:")
    lines += [line("-", r) for r in unranked[:10]]
    lines.append("  Free data drops delisted tickers: results carry survivorship bias until a paid provider.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", action="store_true", help="print the latest leaderboard and exit")
    parser.add_argument("--horizon", type=int, choices=HORIZONS, default=RANK_HORIZON, help="report horizon")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    if not args.report:
        s = run(conn)
        log.info("Leaderboard %s: %d members at h=%d, %d ranked; filings per horizon %s",
                 s.as_of, s.members, RANK_HORIZON, s.ranked, s.filings)
    print("\n".join(report(conn, args.horizon)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
