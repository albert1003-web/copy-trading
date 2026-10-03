"""Trade outcomes (Milestone 3.1, F2/F5): python -m analytics.outcomes [--report]

Measures every priced trade from disclosure, not the trade date (hard rule 3), into `trade_outcomes`.

  scope      trades with a symbol (listed | renamed | unlisted). Rows for trades that leave the scope are deleted.
  calendar   SPY's bars in `prices` are the trading days.
  D0         the first trading-day open after filings.available_at: the same day if that's a trading day and
             available_at is before 09:30 ET, else the next trading day. Pending (no row) until SPY has that bar.
  Return(h)  adj_close(D0 + h) / adjusted open(D0) - 1, h in {1, 5, 10, 20, 60} trading days. The open is
             dividend-unadjusted, so it's put on adj_close's basis: open * adj_close / close on D0. d0_open is raw.
  abnormal   Return(h) - SPY's return over the same window. A horizon fills once SPY has its bar; a missing
             symbol bar leaves it NULL (nothing is forward-filled).
  win_h      abnormal return > 0, for BUYs we could copy (stock / other, or bought calls). Sales, exchanges and
             bought puts get no label: the Roth can't short.
  complete   D0 + 60 trading days has passed (even if the symbol has no bar then, e.g. delisted).
  tx_ret     context only, never a signal or score input: the move from the close on the last trading day on or
             before tx_date to the D0 open (tx_abn_ret vs SPY). Shows how much of the move the lag gave away.

Every run recomputes every in-scope trade (seconds), so re-based price histories and re-enrichment are picked
up, and re-runs are idempotent. Only this stage's columns are written; open_infl_* belongs to M3.2.
"""

import argparse
import logging
import sqlite3
import statistics
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from common import log as logs
from db import connect

log = logging.getLogger("analytics.outcomes")

BENCHMARK = "SPY"
PRICED_STATUSES = ("listed", "renamed", "unlisted")  # same scope as prices.fetch
HORIZONS = (1, 5, 10, 20, 60)
COMPLETE_AFTER = 60
EASTERN = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)

COLUMNS = (
    ["d0_date", "d0_open"]
    + [f"ret_{h}" for h in HORIZONS]
    + [f"abn_ret_{h}" for h in HORIZONS]
    + [f"win_{h}" for h in HORIZONS]
    + ["tx_ret", "tx_abn_ret", "complete", "computed_at"]
)
UPSERT = (
    f"INSERT INTO trade_outcomes (trade_id, {', '.join(COLUMNS)}) VALUES (?{', ?' * len(COLUMNS)}) "
    f"ON CONFLICT (trade_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in COLUMNS)}"
)


@dataclass
class Summary:
    in_scope: int = 0
    pending: int = 0  # D0 not reached yet (or before the first SPY bar)
    no_d0_price: int = 0  # no usable symbol (or SPY) bar on D0
    with_outcome: int = 0  # rows written
    complete: int = 0
    filled: dict[str, int] = field(default_factory=dict)  # abn_ret_h filled, per horizon
    removed: int = 0
    errors: list[str] = field(default_factory=list)


class Calendar:
    """Trading days (ISO dates, ascending)."""

    def __init__(self, days: list[str]):
        self.days = days

    def d0(self, available_at: str) -> int | None:
        """Index of the first trading-day open after available_at, or None if outside the calendar."""
        moment = datetime.fromisoformat(available_at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        et = moment.astimezone(EASTERN)
        day = et.date().isoformat()
        if not self.days or day < self.days[0]:
            return None
        i = bisect_left(self.days, day)
        if i < len(self.days) and self.days[i] == day and et.time() >= MARKET_OPEN:
            i += 1
        return i if i < len(self.days) else None

    def on_or_before(self, day: str) -> int | None:
        i = bisect_right(self.days, day) - 1
        return i if i >= 0 else None


Bars = dict[str, tuple[float | None, float | None, float | None]]  # date -> (open, close, adj_close)


def load_bars(conn: sqlite3.Connection, symbol: str) -> Bars:
    return {r[0]: (r[1], r[2], r[3]) for r in
            conn.execute("SELECT date, open, close, adj_close FROM prices WHERE ticker = ?", (symbol,))}


def _positive(*values) -> bool:
    return all(v is not None and v > 0 for v in values)


def adj_open(bars: Bars, day: str) -> float | None:
    bar = bars.get(day)
    if bar is None or not _positive(*bar):
        return None
    open_, close, adj = bar
    return open_ * adj / close


def adj_close(bars: Bars, day: str) -> float | None:
    bar = bars.get(day)
    return bar[2] if bar is not None and _positive(bar[2]) else None


def copyable_buy(trade) -> bool:
    """A BUY we could copy in the Roth: a stock or other listed asset, or bought calls (not puts).
    Same text check as the alerts' call detection, kept here so the stages stay decoupled."""
    if trade["action"] != "BUY":
        return False
    if trade["asset_type"] != "option":
        return True
    text = f"{trade['asset_name'] or ''} {trade['description'] or ''}".lower()
    return "call" in text and "put" not in text


def outcome(trade, d0: int, cal: Calendar, bars: Bars, spy: Bars) -> dict:
    day = cal.days[d0]
    row: dict = dict.fromkeys(COLUMNS)
    row["d0_date"] = day
    row["complete"] = int(d0 + COMPLETE_AFTER < len(cal.days))
    bar = bars.get(day)
    row["d0_open"] = bar[0] if bar and _positive(bar[0]) else None

    entry, spy_entry = adj_open(bars, day), adj_open(spy, day)
    if entry is None or spy_entry is None:
        return row
    labeled = copyable_buy(trade)
    for h in HORIZONS:
        if d0 + h >= len(cal.days):
            break
        end = cal.days[d0 + h]
        price, bench = adj_close(bars, end), adj_close(spy, end)
        if price is None or bench is None:
            continue
        ret = price / entry - 1
        abn = ret - (bench / spy_entry - 1)
        row[f"ret_{h}"], row[f"abn_ret_{h}"] = ret, abn
        if labeled:
            row[f"win_{h}"] = int(abn > 0)

    tx_date = trade["tx_date"]
    if tx_date and tx_date < day:
        k = cal.on_or_before(tx_date)
        if k is not None:
            base, spy_base = adj_close(bars, cal.days[k]), adj_close(spy, cal.days[k])
            if base is not None and spy_base is not None:
                row["tx_ret"] = entry / base - 1
                row["tx_abn_ret"] = row["tx_ret"] - (spy_entry / spy_base - 1)
    return row


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_scope(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT t.trade_id, t.symbol, t.action, t.asset_type, t.asset_name, t.description, t.tx_date,
               COALESCE(f.available_at, f.first_seen_at) AS available_at
        FROM trades t JOIN filings f ON f.doc_id = t.doc_id
        WHERE t.symbol IS NOT NULL AND t.ticker_status IN ({','.join('?' * len(PRICED_STATUSES))})
        ORDER BY t.symbol, t.trade_id
        """,
        PRICED_STATUSES,
    ).fetchall()


def run(conn: sqlite3.Connection, *, now=utc_now) -> Summary:
    summary = Summary(filled=dict.fromkeys((str(h) for h in HORIZONS), 0))
    spy = load_bars(conn, BENCHMARK)
    if not spy:
        summary.errors.append(f"outcomes: no {BENCHMARK} prices (run prices.fetch first)")
        return summary
    cal = Calendar(sorted(spy))
    stamp = now()

    by_symbol: dict[str, list] = defaultdict(list)
    for trade in in_scope(conn):
        by_symbol[trade["symbol"]].append(trade)
    summary.in_scope = sum(len(trades) for trades in by_symbol.values())

    rows: list[tuple] = []
    for symbol, trades in by_symbol.items():
        bars = spy if symbol == BENCHMARK else load_bars(conn, symbol)
        for trade in trades:
            d0 = cal.d0(trade["available_at"]) if trade["available_at"] else None
            if d0 is None:
                summary.pending += 1
                continue
            row = outcome(trade, d0, cal, bars, spy)
            row["computed_at"] = stamp
            if adj_open(bars, row["d0_date"]) is None or adj_open(spy, row["d0_date"]) is None:
                summary.no_d0_price += 1
            summary.complete += row["complete"]
            for h in HORIZONS:
                summary.filled[str(h)] += row[f"abn_ret_{h}"] is not None
            rows.append((trade["trade_id"], *(row[c] for c in COLUMNS)))

    written = {r[0] for r in rows}
    stale = [(tid,) for (tid,) in conn.execute("SELECT trade_id FROM trade_outcomes") if tid not in written]
    with conn:  # one transaction
        conn.executemany(UPSERT, rows)
        conn.executemany("DELETE FROM trade_outcomes WHERE trade_id = ?", stale)
    summary.with_outcome, summary.removed = len(rows), len(stale)
    return summary


# --- report -------------------------------------------------------------------------------------------


def report(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT substr(t.disclosure_date, 1, 4) AS year, COUNT(*) AS n,
               SUM(o.d0_open IS NOT NULL) AS priced, SUM(o.complete) AS complete
        FROM trade_outcomes o JOIN trades t ON t.trade_id = o.trade_id
        GROUP BY year ORDER BY year
        """
    ).fetchall()
    if not rows:
        return ["Outcomes: none yet. Run: python -m analytics.outcomes (needs prices.fetch first)"]
    lines = ["Outcomes by disclosure year (rows / priced at D0 / complete):"]
    for r in rows:
        share = f"{100 * r['priced'] / r['n']:.0f}%" if r["n"] else "-"
        lines.append(f"  {r['year'] or '?':6} {r['n']:7} {r['priced'] or 0:7} ({share:>4}) {r['complete'] or 0:7}")

    lines.append("Copyable BUYs, abnormal return vs SPY from D0 (n / mean / median / hit rate):")
    for h in HORIZONS:
        values = [v for (v,) in conn.execute(
            f"SELECT abn_ret_{h} FROM trade_outcomes WHERE win_{h} IS NOT NULL")]
        if not values:
            lines.append(f"  h={h:<3} no data")
            continue
        hits = sum(v > 0 for v in values) / len(values)
        lines.append(f"  h={h:<3} {len(values):7}  {statistics.fmean(values):+.2%}  "
                     f"{statistics.median(values):+.2%}  {hits:.1%}")
    lines.append("  Free data drops delisted tickers: results carry survivorship bias until a paid provider.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", action="store_true", help="print coverage and BUY outcome stats and exit")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    if not args.report:
        s = run(conn)
        log.info("Outcomes: %d in scope, %d written (%d complete, %d without a D0 price), %d pending, %d removed; "
                 "filled per horizon %s", s.in_scope, s.with_outcome, s.complete, s.no_d0_price, s.pending,
                 s.removed, s.filled)
        for error in s.errors:
            log.error("%s", error)
        if s.errors:
            return 1
    print("\n".join(report(conn)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
