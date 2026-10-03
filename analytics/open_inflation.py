"""Open-price inflation (Milestone 3.2, F3): python -m analytics.open_inflation [--report]

Is the first open after a disclosure inflated, so that buying a few days later is cheaper?

  open_infl_k  Open(D0) / Open(D0 + k) - 1 for k in {1, 2, 3, 5} trading days (SPY's bars are the calendar), from
               raw opens (Yahoo's split-adjusted, dividend-unadjusted values). Positive means buying k days later
               was cheaper. Written to trade_outcomes for every row that has a d0_date (from analytics.outcomes);
               NULL if a bar is missing or D0 + k hasn't happened yet.
  aggregates   over copyable BUYs (trade_outcomes.copyable = 1), per group: all, member, mcap (trades.mcap_bucket,
               'unknown' until securities has the symbol) and attention (high = in high_attention_members.csv,
               else other). The unit is the filing, not the trade: a filing's trades share D0 and the same market
               moves, so each filing is one observation (the mean over its trades in the group); n counts filings.
               n, mean, median, share > 0, and an empirical-Bayes shrunk mean per k, so a small group can't claim
               an extreme delay. Into open_inflation_stats.
  best delay   per group with at least MIN_N filings: the k with the highest shrunk mean if it's above 0, else 0
               (buy at the D0 open). Fewer trades: NULL (callers fall back to the mcap group, then all). Into
               entry_delays.

Both tables are replaced each run (latest only), and every run recomputes everything. Raw open moves include
market drift, and free price data drops delisted tickers (survivorship bias).
"""

import argparse
import csv
import logging
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from analytics.stats import sample, shrink
from common import log as logs
from db import connect

log = logging.getLogger("analytics.open_inflation")

BENCHMARK = "SPY"
KS = (1, 2, 3, 5)
MIN_N = 20  # filings
ATTENTION_CSV = Path(__file__).with_name("high_attention_members.csv")


@dataclass
class Summary:
    rows: int = 0  # outcome rows with a d0_date
    filled: dict[str, int] = field(default_factory=dict)  # open_infl_k filled, per k
    copyable: int = 0  # copyable BUYs with open_infl_1
    groups: int = 0
    with_delay: int = 0  # groups with a best_k (n >= MIN_N)
    unknown_attention: list[str] = field(default_factory=list)  # CSV ids not in members
    errors: list[str] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_attention(path: Path) -> set[str]:
    with path.open(newline="") as f:
        return {row["member_id"].strip() for row in csv.DictReader(f) if row.get("member_id", "").strip()}


def inflation(opens: dict[str, float | None], days: list[str], d0: int, k: int) -> float | None:
    if d0 + k >= len(days):
        return None
    start, later = opens.get(days[d0]), opens.get(days[d0 + k])
    if start is None or later is None or start <= 0 or later <= 0:
        return None
    return start / later - 1


def run(conn: sqlite3.Connection, *, attention_path: Path = ATTENTION_CSV, now=utc_now) -> Summary:
    summary = Summary(filled=dict.fromkeys((str(k) for k in KS), 0))
    days = [d for (d,) in conn.execute("SELECT date FROM prices WHERE ticker = ? ORDER BY date", (BENCHMARK,))]
    if not days:
        summary.errors.append(f"open_inflation: no {BENCHMARK} prices (run prices.fetch first)")
        return summary
    index = {d: i for i, d in enumerate(days)}
    attention = load_attention(attention_path)
    known = {m for (m,) in conn.execute("SELECT member_id FROM members")}
    summary.unknown_attention = sorted(attention - known)

    rows = conn.execute(
        """
        SELECT o.trade_id, o.d0_date, o.copyable, t.doc_id, t.symbol, t.member_id, t.mcap_bucket
        FROM trade_outcomes o JOIN trades t ON t.trade_id = o.trade_id
        WHERE o.d0_date IS NOT NULL ORDER BY t.symbol
        """
    ).fetchall()
    summary.rows = len(rows)

    updates: list[tuple] = []
    # (group_type, group_key, k) -> doc_id -> the filing's trade values
    values: dict[tuple[str, str, int], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    symbol, opens = None, {}
    for r in rows:
        if r["symbol"] != symbol:
            symbol = r["symbol"]
            opens = dict(conn.execute("SELECT date, open FROM prices WHERE ticker = ?", (symbol,)).fetchall())
        d0 = index.get(r["d0_date"])
        infl = {k: inflation(opens, days, d0, k) if d0 is not None else None for k in KS}
        updates.append((*(infl[k] for k in KS), r["trade_id"]))
        for k in KS:
            summary.filled[str(k)] += infl[k] is not None
        if not r["copyable"]:
            continue
        summary.copyable += infl[1] is not None
        groups = [("all", "all"), ("mcap", r["mcap_bucket"] or "unknown"),
                  ("attention", "high" if r["member_id"] in attention else "other")]
        if r["member_id"]:
            groups.append(("member", r["member_id"]))
        for k in KS:
            if infl[k] is not None:
                for group in groups:
                    values[(*group, k)][r["doc_id"]].append(infl[k])

    stamp = now()
    stats_rows, delay_rows = aggregate(values, stamp)
    summary.groups = len(delay_rows)
    summary.with_delay = sum(row[4] is not None for row in delay_rows)
    with conn:  # one transaction
        conn.executemany(
            f"UPDATE trade_outcomes SET {', '.join(f'open_infl_{k} = ?' for k in KS)} WHERE trade_id = ?", updates)
        conn.execute("DELETE FROM open_inflation_stats")
        conn.execute("DELETE FROM entry_delays")
        conn.executemany("INSERT INTO open_inflation_stats (group_type, group_key, k, n, n_trades, mean, median, "
                         "share_pos, shrunk_mean, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", stats_rows)
        conn.executemany("INSERT INTO entry_delays (group_type, group_key, n, n_trades, best_k, gain, computed_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", delay_rows)
    return summary


def aggregate(values: dict[tuple[str, str, int], dict[str, list[float]]],
              stamp: str) -> tuple[list[tuple], list[tuple]]:
    """Per-group stats rows and best-delay rows, one observation per filing. Shrinkage is within one group type
    and k."""
    filings = {key: [statistics.fmean(v) for v in by_doc.values()] for key, by_doc in values.items()}
    n_trades = {key: sum(len(v) for v in by_doc.values()) for key, by_doc in values.items()}
    samples = {key: sample(v) for key, v in filings.items()}
    shrunk: dict[tuple[str, str, int], float] = {}
    for group_type, k in {(t, k) for t, _key, k in samples}:
        peers = {key: s for (t, key, kk), s in samples.items() if t == group_type and kk == k}
        for key, value in shrink(peers).items():
            shrunk[(group_type, key, k)] = value

    stats_rows = [
        (t, key, k, s.n, n_trades[(t, key, k)], s.mean, statistics.median(filings[(t, key, k)]),
         sum(v > 0 for v in filings[(t, key, k)]) / s.n, shrunk[(t, key, k)], stamp)
        for (t, key, k), s in sorted(samples.items())
    ]

    delay_rows = []
    for t, key in sorted({(t, key) for t, key, _k in samples}):
        present = [k for k in KS if (t, key, k) in samples]
        n = max(samples[(t, key, k)].n for k in present)
        trades = max(n_trades[(t, key, k)] for k in present)
        best_k = gain = None
        if n >= MIN_N:
            candidates = [(shrunk[(t, key, k)], k) for k in present if shrunk[(t, key, k)] > 0]
            gain, best_k = max(candidates) if candidates else (0.0, 0)
        delay_rows.append((t, key, n, trades, best_k, gain, stamp))
    return stats_rows, delay_rows


# --- report -------------------------------------------------------------------------------------------


def report(conn: sqlite3.Connection, members: int = 15) -> list[str]:
    if not conn.execute("SELECT 1 FROM open_inflation_stats LIMIT 1").fetchone():
        return ["Open inflation: none yet. Run: python -m analytics.open_inflation (after analytics.outcomes)"]
    lines = ["Open inflation, copyable BUYs, one observation per filing (positive = buying k days later was cheaper)",
             "  group                  filings  trades  mean k=1     k=2     k=3     k=5   share>0 k=1  best k (gain)"]
    names = dict(conn.execute("SELECT member_id, name FROM members").fetchall())

    def line(group_type: str, key: str) -> str:
        by_k = {r["k"]: r for r in conn.execute(
            "SELECT * FROM open_inflation_stats WHERE group_type = ? AND group_key = ?", (group_type, key))}
        delay = conn.execute("SELECT * FROM entry_delays WHERE group_type = ? AND group_key = ?",
                             (group_type, key)).fetchone()
        cells = "  ".join(f"{by_k[k]['mean']:+.2%}" if k in by_k else "   -   " for k in KS)
        share = f"{by_k[1]['share_pos']:.0%}" if 1 in by_k else "-"
        best = "-" if delay["best_k"] is None else f"{delay['best_k']} ({delay['gain']:+.2%})"
        label = names.get(key, key) if group_type == "member" else key
        return f"  {label[:22]:22} {delay['n']:7} {delay['n_trades']:7}    {cells}  {share:>10}  {best}"

    lines.append(line("all", "all"))
    for group_type, title in (("mcap", "By market-cap bucket:"), ("attention", "By media attention:")):
        lines.append(title)
        lines += [line(group_type, key) for (key,) in conn.execute(
            "SELECT group_key FROM entry_delays WHERE group_type = ? ORDER BY n DESC", (group_type,))]
    lines.append(f"Members, most filings first (top {members}):")
    lines += [line("member", key) for (key,) in conn.execute(
        "SELECT group_key FROM entry_delays WHERE group_type = 'member' ORDER BY n DESC LIMIT ?", (members,))]
    lines.append("  Raw open moves include market drift; free data drops delisted tickers (survivorship bias).")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", action="store_true", help="print the aggregates and best delays and exit")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    if not args.report:
        s = run(conn)
        log.info("Open inflation: %d rows, filled per k %s; %d copyable BUYs, %d groups (%d with a best delay)",
                 s.rows, s.filled, s.copyable, s.groups, s.with_delay)
        for member_id in s.unknown_attention:
            log.warning("high_attention_members.csv: unknown member_id %s", member_id)
        for error in s.errors:
            log.error("%s", error)
        if s.errors:
            return 1
    print("\n".join(report(conn)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
