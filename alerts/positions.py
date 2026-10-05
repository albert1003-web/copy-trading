"""Exit alerts for the positions you logged (Milestone 4.3). Only your own open my_positions rows are watched:
member filings never trigger an exit email by themselves.

  watched    open positions whose exit_rule is a rule label (common/exit_rules.label, chosen on the Positions page)
             and that have no exit_alerts row yet. Other text (older free-form notes) isn't watched.
  prices     the ticker's stored bars on SPY's calendar, split-adjusted but not dividend-adjusted: the same basis
             as the price you paid. Your fill is the entry; on the buy day only the close counts (its low/high may
             have come before you bought).
  sale       MemberSale fires as soon as the source trade's member has a sale filing of the symbol available after
             your buy (not on the sale's D0 bar, so you can sell at the next open). Without a source trade only
             the 60-day limit applies.
  data_end   the ticker's bars stopped 5+ trading days before SPY's (delisted or renamed?): one email to check it.
             A bar that's merely a day late doesn't count.
  split      if the stored close on the buy day is more than 40% away from your price, a split since then has
             moved every level: the position is skipped with a warning (re-log it at the adjusted price).
One email per position, recorded in exit_alerts only after it's sent (alerts/run.py).
"""

import sqlite3
from bisect import bisect_left
from dataclasses import dataclass, field

from common.exit_rules import MemberSale, Rule, Series, make_series, parse_rule, simulate

BENCHMARK = "SPY"
SALES = ("SELL", "SELL_PARTIAL")
STALE_DAYS = 5  # trading days a ticker's last bar may trail SPY's before it counts as stopped
SPLIT_TOLERANCE = 0.4


@dataclass
class ExitAlert:
    position: dict
    rule: Rule
    reason: str  # stop | target | hold | sale | data_end
    day: str  # when it fired (sale: the date the sale filing became available)
    price: float | None  # the level or price it fired at
    close: float | None = None  # that day's close
    sale: dict | None = None  # the member's sale filing (MemberSale)
    notes: list[str] = field(default_factory=list)


def _calendar(conn: sqlite3.Connection) -> list[str]:
    return sorted(d for (d,) in conn.execute("SELECT date FROM prices WHERE ticker = ?", (BENCHMARK,)))


def _series(conn: sqlite3.Connection, symbol: str, pos: dict[str, int], n: int) -> Series:
    # close passed as adj_close too: no dividend adjustment, matching what you paid
    rows = conn.execute("SELECT date, open, high, low, close, close FROM prices WHERE ticker = ?", (symbol,))
    return make_series(rows, pos, n)


def _member_sale(conn: sqlite3.Connection, position: dict) -> dict | None:
    """The source member's first sale filing of the symbol made available after the buy, if any."""
    row = conn.execute(
        """
        SELECT s.doc_id, s.tx_date, f.filing_date, f.source_url, COALESCE(f.available_at, f.first_seen_at) AS available,
               m.name AS member_name
        FROM trades src
        JOIN trades s ON s.member_id = src.member_id AND s.symbol = COALESCE(src.symbol, ?)
        JOIN filings f ON f.doc_id = s.doc_id
        LEFT JOIN members m ON m.member_id = s.member_id
        WHERE src.trade_id = ? AND s.action IN (?, ?) AND substr(COALESCE(f.available_at, f.first_seen_at), 1, 10) >= ?
        ORDER BY available LIMIT 1
        """,
        (position["ticker"], position["trade_id"], *SALES, position["buy_date"]),
    ).fetchone()
    return dict(row) if row else None


def due(conn: sqlite3.Connection) -> tuple[list[ExitAlert], list[str]]:
    """(exit alerts to send, warnings) for every watched open position."""
    days = _calendar(conn)
    pos = {d: i for i, d in enumerate(days)}
    alerts: list[ExitAlert] = []
    warnings: list[str] = []
    rows = conn.execute(
        """
        SELECT p.* FROM my_positions p
        WHERE p.status = 'open' AND NOT EXISTS (SELECT 1 FROM exit_alerts e WHERE e.position_id = p.position_id)
        ORDER BY p.position_id
        """
    ).fetchall()
    for row in rows:
        position = dict(row)
        rule = parse_rule(position["exit_rule"])
        if rule is None:
            continue
        ticker = position["ticker"].upper()
        notes = []
        if isinstance(rule, MemberSale):
            if position["trade_id"] is None:
                notes.append("No source trade is linked to this position, so only the "
                             f"{rule.max_hold}-day limit applies.")
            elif sale := _member_sale(conn, position):
                alerts.append(ExitAlert(position, rule, "sale", sale["available"][:10], None, sale=sale))
                continue
        if not days:
            continue
        buy = bisect_left(days, position["buy_date"])
        if buy >= len(days):
            continue  # no bars since the buy yet
        series = _series(conn, ticker, pos, len(days))
        if series.last < buy:
            if len(days) - 1 - max(series.last, buy - 1) >= STALE_DAYS:
                alerts.append(ExitAlert(position, rule, "data_end", days[max(series.last, 0)], None,
                                        notes=notes + [f"No prices for {ticker} since the buy."]))
            continue
        first_close = next(c for c in series.close[buy:] if c is not None)
        if abs(first_close / position["buy_price"] - 1) > SPLIT_TOLERANCE:
            warnings.append(f"position {position['position_id']} ({ticker}): close {first_close:.2f} after the buy is "
                            f"far from the buy price {position['buy_price']:.2f} (a split?); not watched")
            continue
        ex = simulate(series, buy, rule, entry=position["buy_price"], bought_intraday=True)
        if ex is None or ex.reason == "open":
            continue
        if ex.reason == "data_end" and len(days) - 1 - series.last < STALE_DAYS:
            continue  # just a late bar
        alerts.append(ExitAlert(position, rule, ex.reason, days[ex.idx], ex.price, series.close[ex.idx], notes=notes))
    return alerts, warnings
