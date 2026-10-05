"""Signal factors (Milestone 3.5): python -m analytics.factors [--report]

The trade-feature half of the v2 alert score. For each feature level (common/signals.py: market-cap bucket,
filing delay, amount, committee overlap, kind of asset), the average 20-day excess vs SPY of copyable buys
(trade_outcomes.copyable = 1), measured from D0.

  unit       the filing, as in the leaderboard: a filing's trades at one level are one observation.
  clipping   filing excesses are clipped at the 1st/99th percentile of all filings, as for the leaderboard score.
  shrinkage  levels are shrunk toward the pooled mean within each factor (analytics.stats.shrink), so a level
             with a handful of filings has almost no effect.
  effect     shrunk level - that factor's pooled mean. Measured within the factor, not against ('all', 'all'):
             a filing with trades at two levels counts once in each, so a factor's pooled mean differs slightly
             from the overall one, and a factor with no real spread must add exactly 0.
  output     signal_factors, replaced each run, plus ('all', 'all') (the overall pooled mean). alerts/score.py
             reads it: expected excess = member's shrunk score + sum of the trade's level effects.

Free price data drops delisted tickers: results carry survivorship bias until a paid provider.
"""

import argparse
import logging
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from analytics.stats import pooled_mean, sample, shrink
from common import log as logs
from common.signals import FACTORS, levels
from db import connect

log = logging.getLogger("analytics.factors")

HORIZON = 20
CLIP = 0.01


@dataclass
class Summary:
    filings: int = 0
    levels: int = 0
    errors: list[str] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(conn: sqlite3.Connection, *, now=utc_now) -> Summary:
    summary = Summary()
    rows = conn.execute(
        f"""
        SELECT t.doc_id, t.mcap_bucket, t.filing_delay_days, t.amount_min, t.committee_relevant, t.asset_type,
               t.ticker_status, t.is_etf, o.abn_ret_{HORIZON} AS abn
        FROM trade_outcomes o JOIN trades t ON t.trade_id = o.trade_id
        WHERE o.copyable = 1 AND o.abn_ret_{HORIZON} IS NOT NULL
        """
    ).fetchall()

    # (factor, level) -> doc_id -> trade excesses
    trades: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        trades[("all", "all")][r["doc_id"]].append(r["abn"])
        for factor, level in levels(r).items():
            trades[(factor, level)][r["doc_id"]].append(r["abn"])
    filings = {key: [statistics.fmean(v) for v in by_doc.values()] for key, by_doc in trades.items()}
    summary.filings = len(filings.get(("all", "all"), []))

    out: list[tuple] = []
    if summary.filings:
        ordered = sorted(filings[("all", "all")])
        last = len(ordered) - 1
        low, high = ordered[round(CLIP * last)], ordered[round((1 - CLIP) * last)]
        clipped = {key: [min(max(v, low), high) for v in values] for key, values in filings.items()}
        stamp = now()
        for factor in ("all", *FACTORS):
            groups = {level: sample(v) for (f, level), v in clipped.items() if f == factor}
            mu = pooled_mean(groups)
            for level, value in shrink(groups).items():
                n_trades = sum(len(v) for v in trades[(factor, level)].values())
                out.append((factor, level, groups[level].n, n_trades, groups[level].mean, value, value - mu, stamp))
    summary.levels = len(out)

    with conn:  # one transaction: replaced whole
        conn.execute("DELETE FROM signal_factors")
        conn.executemany("INSERT INTO signal_factors (factor, level, n, n_trades, mean, shrunk, effect, computed_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", out)
    return summary


def report(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT * FROM signal_factors ORDER BY factor = 'all' DESC, factor, n DESC").fetchall()
    if not rows:
        return ["Signal factors: none yet. Run: python -m analytics.factors (after analytics.outcomes)"]
    pooled = next(r["shrunk"] for r in rows if r["factor"] == "all")
    lines = [f"Signal factors: 20-day excess vs SPY of copyable buys, per filing; pooled mean {pooled:+.2%}",
             "  factor     level        filings  trades     mean   effect (shrunk, vs the factor's mean)"]
    lines += [f"  {r['factor']:10} {r['level']:12} {r['n']:7} {r['n_trades']:7}  {r['mean']:+7.2%}  "
              f"{r['effect']:+7.2%}" for r in rows if r["factor"] != "all"]
    lines.append("  Free data drops delisted tickers: results carry survivorship bias until a paid provider.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", action="store_true", help="print the factor effects and exit")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    if not args.report:
        s = run(conn)
        log.info("Signal factors: %d filings, %d levels", s.filings, s.levels)
    print("\n".join(report(conn)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
