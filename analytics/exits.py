"""Exit backtest engine (Milestone 4.1, F4): python -m analytics.exits --check

Simulates exit rules on daily bars for copyable BUYs, entered at the D0 open (hard rule 3), with costs, slippage
and the Roth T+1 settled-cash constraint. Walk-forward validation and `exit_backtests` are M4.2: this module
reports no rule performance of its own (backtests report out-of-sample results only).

  prices     adjusted bars: open/high/low = raw * adj_close / close (the basis analytics.outcomes uses for the
             D0 open), close = adj_close. Nothing is forward-filled; a day without a bar is skipped.
  entry      the adjusted D0 open (trade_outcomes.d0_date).
  rules      FixedHold(days), StopTarget(stop, target), TrailingStop(pct), AtrStop(mult, n) and MemberSale. Every
             rule also exits at the close after max_hold trading days (60, the "complete" horizon).
  fills      conservative for daily bars: an open through a stop or target fills at the open; a stop and a target
             both inside one day's range count as the stop; an intraday fill is at the level; a trailing high
             updates only after that day's stop check. ATR uses bars before D0 only.
  sale       MemberSale exits at the open on the D0 of the member's next sale filing (SELL / SELL_PARTIAL) of the
             same symbol: the day the sale became public, never its trade date.
  data_end   bars stop (delisted) before an exit: out at the last close. Free data lacks most delisted tickers,
             so results carry survivorship bias until a paid provider.
  open       the calendar ends before an exit: marked at the last close, left out of trade stats.
  costs      commission (dollars per order, $0 at Fidelity) and slippage per side by market-cap bucket (bps):
             buy at price * (1 + s), sell at price * (1 - s). The abnormal return subtracts SPY over the same
             window (D0 open to the exit's open, or to the close for close and intraday exits), without costs.
  portfolio  signals one per (filing, symbol) in D0 order; position size = size * equity at the previous close.
             Buys draw on settled cash only and sale proceeds settle the next trading day (T+1), so a good-faith
             violation can't happen; a signal without enough settled cash is skipped and counted. Each day: open
             exits, then D0 buys, then intraday and close exits.
  universe   all | watchlist (today's: hindsight) | ranked | a member set. ranked is point-in-time: at each
             quarter start, members ranked on the leaderboard's rules (>= MIN_N filings, shrunk 20-day excess
             above min_score) from filings whose D0 + 20 had passed by then.

--check verifies the engine on the real DB: zero-cost FixedHold(h) must reproduce trade_outcomes ret_h / abn_ret_h,
and a portfolio run must never leave settled cash negative. It writes nothing.
"""

import argparse
import logging
import sqlite3
import statistics
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import ClassVar

from analytics import leaderboard
from analytics.outcomes import BENCHMARK, HORIZONS, Calendar
from common import log as logs
from db import connect

log = logging.getLogger("analytics.exits")

SALES = ("SELL", "SELL_PARTIAL")
MAX_HOLD = 60
DEFAULT_SLIPPAGE_BPS = {"mega": 5, "large": 10, "mid": 20, "small": 40, "micro": 75, None: 75}
TOLERANCE = 1e-9


# --- prices -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Series:
    """A symbol's adjusted bars aligned to the calendar; None where a value is missing."""

    open: list
    high: list
    low: list
    close: list
    last: int  # index of the last bar with a close (-1 if none)


def _positive(*values) -> bool:
    return all(v is not None and v > 0 for v in values)


def make_series(rows: Iterable, pos: dict[str, int], n_days: int) -> Series:
    """rows: (date, open, high, low, close, adj_close)."""
    o, h, lo, c = ([None] * n_days for _ in range(4))
    for day, open_, high, low, close, adj in rows:
        i = pos.get(day)
        if i is None or not _positive(adj):
            continue
        c[i] = adj
        if not _positive(open_, close):
            continue
        f = adj / close
        o[i] = open_ * adj / close  # same arithmetic as outcomes.adj_open
        h[i] = max(high * f if _positive(high) else 0.0, o[i], adj)
        lo[i] = min(low * f if _positive(low) else float("inf"), o[i], adj)
    last = max((i for i, v in enumerate(c) if v is not None), default=-1)
    return Series(o, h, lo, c, last)


class Market:
    """SPY's trading calendar plus cached adjusted series per symbol."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        days = sorted(d for (d,) in conn.execute("SELECT date FROM prices WHERE ticker = ?", (BENCHMARK,)))
        self.cal = Calendar(days)
        self.pos = {d: i for i, d in enumerate(days)}
        self._cache: dict[str, Series] = {}
        self.spy = self.series(BENCHMARK)

    @property
    def n_days(self) -> int:
        return len(self.cal.days)

    def series(self, symbol: str) -> Series:
        if symbol not in self._cache:
            rows = self.conn.execute(
                "SELECT date, open, high, low, close, adj_close FROM prices WHERE ticker = ?", (symbol,))
            self._cache[symbol] = make_series(rows, self.pos, self.n_days)
        return self._cache[symbol]

    def quarter_start(self, i: int) -> int:
        """Index of the first trading day of day i's calendar quarter."""
        day = self.cal.days[i]
        month = (int(day[5:7]) - 1) // 3 * 3 + 1
        return bisect_left(self.cal.days, f"{day[:4]}-{month:02d}-01")


# --- rules --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FixedHold:
    days: int = 20
    name: ClassVar[str] = "fixed_hold"

    @property
    def max_hold(self) -> int:
        return self.days


@dataclass(frozen=True)
class StopTarget:
    stop: float | None = 0.10  # fraction below the entry
    target: float | None = None  # fraction above the entry
    max_hold: int = MAX_HOLD
    name: ClassVar[str] = "stop_target"


@dataclass(frozen=True)
class TrailingStop:
    pct: float = 0.10  # fraction below the highest high since entry
    max_hold: int = MAX_HOLD
    name: ClassVar[str] = "trailing_stop"


@dataclass(frozen=True)
class AtrStop:
    mult: float = 3.0  # chandelier: highest high since entry - mult * ATR(n) at entry
    n: int = 14
    max_hold: int = MAX_HOLD
    name: ClassVar[str] = "atr_stop"


@dataclass(frozen=True)
class MemberSale:
    max_hold: int = MAX_HOLD
    name: ClassVar[str] = "member_sale"


Rule = FixedHold | StopTarget | TrailingStop | AtrStop | MemberSale


def params(rule: Rule) -> dict:
    return asdict(rule)


def atr_before(s: Series, d0: int, n: int) -> float | None:
    """Average true range over the n bars before D0 (needs n + 1 bars), or None."""
    bars: list[int] = []
    i = d0 - 1
    while i >= 0 and len(bars) < n + 1:
        if s.close[i] is not None and s.high[i] is not None:
            bars.append(i)
        i -= 1
    if len(bars) < n + 1:
        return None
    bars.reverse()
    ranges = [max(s.high[b], s.close[a]) - min(s.low[b], s.close[a]) for a, b in zip(bars, bars[1:], strict=False)]
    return statistics.fmean(ranges)


def levels(rule: Rule, entry: float, high: float, atr: float | None) -> tuple[float | None, float | None]:
    """(stop, target) for the next check."""
    if isinstance(rule, StopTarget):
        return (entry * (1 - rule.stop) if rule.stop is not None else None,
                entry * (1 + rule.target) if rule.target is not None else None)
    if isinstance(rule, TrailingStop):
        return high * (1 - rule.pct), None
    if isinstance(rule, AtrStop):
        return (high - rule.mult * atr if atr is not None else None), None
    return None, None


@dataclass(frozen=True)
class Exit:
    idx: int
    price: float  # adjusted, before costs
    at: str  # open | intraday | close
    reason: str  # hold | stop | target | sale | data_end | open


def simulate(s: Series, d0: int, rule: Rule, sale: int | None = None) -> Exit | None:
    """When and where a position bought at the D0 open exits under `rule`; None without a D0 open.
    sale: index of the member's next sale D0 after d0 (MemberSale only)."""
    entry = s.open[d0]
    if entry is None:
        return None
    if not isinstance(rule, MemberSale) or (sale is not None and sale <= d0):
        sale = None
    atr = atr_before(s, d0, rule.n) if isinstance(rule, AtrStop) else None
    end = d0 + rule.max_hold
    n_days = len(s.close)
    high, due = entry, None
    for i in range(d0, n_days):
        if i > s.last:
            return Exit(s.last, s.close[s.last], "close", "data_end")
        o, close = s.open[i], s.close[i]
        if close is None:  # no bar today: an exit due today happens at the next open
            if i in (sale, end):
                due = due or ("sale" if i == sale else "hold")
            continue
        if due or i == sale:
            return Exit(i, o, "open", due or "sale") if o is not None else Exit(i, close, "close", due or "sale")
        stop, target = levels(rule, entry, high, atr)
        if i > d0 and o is not None:
            if stop is not None and o <= stop:
                return Exit(i, o, "open", "stop")
            if target is not None and o >= target:
                return Exit(i, o, "open", "target")
        if stop is not None and s.low[i] is not None and s.low[i] <= stop:
            return Exit(i, stop, "intraday", "stop")
        if target is not None and s.high[i] is not None and s.high[i] >= target:
            return Exit(i, target, "intraday", "target")
        if i >= end:
            return Exit(i, close, "close", "hold")
        if s.high[i] is not None:
            high = max(high, s.high[i])
    return Exit(n_days - 1, s.close[n_days - 1], "close", "open")


# --- costs and trade results --------------------------------------------------------------------------


@dataclass(frozen=True)
class Costs:
    commission: float = 0.0  # dollars per order
    slippage_bps: dict = field(default_factory=lambda: dict(DEFAULT_SLIPPAGE_BPS))  # per side, by mcap bucket

    def slippage(self, bucket: str | None) -> float:
        bps = self.slippage_bps.get(bucket, self.slippage_bps.get(None, 0))
        return bps / 10_000


ZERO_COSTS = Costs(slippage_bps={None: 0})


@dataclass(frozen=True)
class Signal:
    trade_id: int
    doc_id: str
    member_id: str | None
    symbol: str
    bucket: str | None  # trades.mcap_bucket
    d0: int  # calendar index


@dataclass(frozen=True)
class TradeResult:
    signal: Signal
    exit: Exit
    entry: float  # net of slippage
    exit_price: float  # net of slippage
    ret: float
    abn: float | None


def evaluate(market: Market, sig: Signal, rule: Rule, costs: Costs = ZERO_COSTS,
             sale: int | None = None) -> TradeResult | None:
    s = market.series(sig.symbol)
    ex = simulate(s, sig.d0, rule, sale)
    if ex is None:
        return None
    slip = costs.slippage(sig.bucket)
    entry, out = s.open[sig.d0] * (1 + slip), ex.price * (1 - slip)
    ret = out / entry - 1
    spy = market.spy
    spy_end = spy.open[ex.idx] if ex.at == "open" else spy.close[ex.idx]
    abn = ret - (spy_end / spy.open[sig.d0] - 1) if _positive(spy_end, spy.open[sig.d0]) else None
    return TradeResult(sig, ex, entry, out, ret, abn)


# --- inputs -------------------------------------------------------------------------------------------


def load_signals(market: Market) -> list[Signal]:
    """Copyable BUYs with a D0, one per (filing, symbol), in D0 order."""
    seen: set[tuple[str, str]] = set()
    result = []
    for r in market.conn.execute(
        """
        SELECT t.trade_id, t.doc_id, t.member_id, t.symbol, t.mcap_bucket, o.d0_date
        FROM trade_outcomes o JOIN trades t ON t.trade_id = o.trade_id
        WHERE o.copyable = 1 AND o.d0_date IS NOT NULL AND t.symbol IS NOT NULL
        ORDER BY o.d0_date, t.doc_id, t.trade_id
        """
    ):
        d0 = market.pos.get(r["d0_date"])
        if d0 is None or (r["doc_id"], r["symbol"]) in seen:
            continue
        seen.add((r["doc_id"], r["symbol"]))
        result.append(Signal(r["trade_id"], r["doc_id"], r["member_id"], r["symbol"], r["mcap_bucket"], d0))
    return result


def sale_days(market: Market) -> dict[tuple[str, str], list[int]]:
    """(member, symbol) -> sorted D0 indexes of that member's sales of it."""
    days: dict[tuple[str, str], set[int]] = defaultdict(set)
    for member, symbol, d0_date in market.conn.execute(
        f"""
        SELECT t.member_id, t.symbol, o.d0_date
        FROM trade_outcomes o JOIN trades t ON t.trade_id = o.trade_id
        WHERE t.action IN ({','.join('?' * len(SALES))}) AND t.member_id IS NOT NULL AND t.symbol IS NOT NULL
          AND o.d0_date IS NOT NULL
        """,
        SALES,
    ):
        if d0_date in market.pos:
            days[(member, symbol)].add(market.pos[d0_date])
    return {key: sorted(v) for key, v in days.items()}


def next_sale(sales: dict[tuple[str, str], list[int]], sig: Signal) -> int | None:
    found = sales.get((sig.member_id, sig.symbol), [])
    i = bisect_right(found, sig.d0)
    return found[i] if i < len(found) else None


def ranked_members(market: Market, day: int, min_score: float = 0.0) -> set[str]:
    """Members ranked as of trading day `day`, from filings whose D0 + 20 had passed by then."""
    cut = day - leaderboard.RANK_HORIZON
    if cut <= 0:
        return set()
    by_member = leaderboard.filings(market.conn, leaderboard.RANK_HORIZON, d0_before=market.cal.days[cut])
    stats = leaderboard.member_stats(by_member)
    return {m for m, s in stats.items() if s["n_filings"] >= leaderboard.MIN_N and s["shrunk_score"] > min_score}


def select(market: Market, signals: list[Signal], universe="ranked", min_score: float = 0.0) -> list[Signal]:
    """Signals from the universe: 'all', 'watchlist', 'ranked' (point-in-time per quarter) or a set of members."""
    if universe == "all":
        return list(signals)
    if universe == "watchlist":
        ids = {m for (m,) in market.conn.execute("SELECT member_id FROM watchlist WHERE active = 1")}
        return [s for s in signals if s.member_id in ids]
    if universe == "ranked":
        by_quarter: dict[int, set[str]] = {}
        result = []
        for s in signals:
            q = market.quarter_start(s.d0)
            if q not in by_quarter:
                by_quarter[q] = ranked_members(market, q, min_score)
            if s.member_id in by_quarter[q]:
                result.append(s)
        return result
    ids = set(universe)
    return [s for s in signals if s.member_id in ids]


# --- portfolio ----------------------------------------------------------------------------------------


@dataclass
class Result:
    rule: str
    params: dict
    signals: int = 0
    n_trades: int = 0  # closed trades (not still open at the calendar's end)
    mean_ret: float | None = None
    mean_abn_ret: float | None = None
    hit_rate: float | None = None  # share of closed trades with abnormal return > 0
    max_drawdown: float | None = None
    total_return: float | None = None
    spy_return: float | None = None  # SPY buy-and-hold over the same days
    skipped_cash: int = 0  # not enough settled cash
    skipped_held: int = 0  # symbol already held
    skipped_full: int = 0  # max_positions reached
    no_entry: int = 0  # no D0 open
    still_open: int = 0
    data_end: int = 0  # exited at the last bar (delisted)
    min_settled: float | None = None  # lowest settled cash after a buy (never below 0)
    trades: list[TradeResult] = field(default_factory=list)
    equity: list[tuple[str, float]] = field(default_factory=list)  # (date, equity at the close)


def run_portfolio(market: Market, signals: list[Signal], rule: Rule, costs: Costs | None = None, *,
                  sales: dict | None = None, capital: float = 100_000.0, size: float = 0.05,
                  max_positions: int = 20, start: str | None = None, end: str | None = None) -> Result:
    """Trade `signals` with D0 in [start, end] under `rule` with T+1 settled cash."""
    costs = costs or Costs()
    days = market.cal.days
    result = Result(rule.name, params(rule))
    chosen = [s for s in signals if (start is None or days[s.d0] >= start) and (end is None or days[s.d0] <= end)]
    result.signals = len(chosen)
    if isinstance(rule, MemberSale) and sales is None:
        sales = sale_days(market)
    buys: dict[int, list[TradeResult]] = defaultdict(list)
    for sig in chosen:
        tr = evaluate(market, sig, rule, costs, next_sale(sales, sig) if sales else None)
        if tr is None:
            result.no_entry += 1
        else:
            buys[sig.d0].append(tr)
    if not buys:
        return result

    first = min(buys)
    last = max(tr.exit.idx for trs in buys.values() for tr in trs)
    settled, pending = capital, []  # pending: (settles_on_idx, amount)
    positions: dict[str, tuple[float, TradeResult]] = {}
    equity_prev = peak = capital
    drawdown = 0.0

    def sell(i: int, opening: bool) -> None:
        for sym, (shares, tr) in list(positions.items()):
            if tr.exit.idx == i and (tr.exit.at == "open") == opening:
                pending.append((i + 1, shares * tr.exit_price - costs.commission))
                del positions[sym]

    def mark(sym: str, i: int) -> float:
        closes = market.series(sym).close
        while closes[i] is None and i > 0:
            i -= 1
        return closes[i] or 0.0

    for i in range(first, last + 1):
        settled += sum(a for d, a in pending if d <= i)
        pending = [(d, a) for d, a in pending if d > i]
        sell(i, opening=True)
        for tr in buys.get(i, []):
            sym = tr.signal.symbol
            if sym in positions:
                result.skipped_held += 1
                continue
            if len(positions) >= max_positions:
                result.skipped_full += 1
                continue
            amount = size * equity_prev
            if amount > settled:
                result.skipped_cash += 1
                continue
            settled -= amount
            result.min_settled = settled if result.min_settled is None else min(result.min_settled, settled)
            positions[sym] = ((amount - costs.commission) / tr.entry, tr)
            result.trades.append(tr)
        sell(i, opening=False)
        equity = settled + sum(a for _, a in pending) + sum(sh * mark(sym, i) for sym, (sh, _) in positions.items())
        result.equity.append((days[i], equity))
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
        equity_prev = equity

    closed = [tr for tr in result.trades if tr.exit.reason != "open"]
    abns = [tr.abn for tr in closed if tr.abn is not None]
    result.n_trades = len(closed)
    result.still_open = len(result.trades) - len(closed)
    result.data_end = sum(tr.exit.reason == "data_end" for tr in closed)
    if closed:
        result.mean_ret = statistics.fmean(tr.ret for tr in closed)
    if abns:
        result.mean_abn_ret = statistics.fmean(abns)
        result.hit_rate = sum(a > 0 for a in abns) / len(abns)
    result.max_drawdown = drawdown
    result.total_return = result.equity[-1][1] / capital - 1
    spy = market.spy
    if _positive(spy.open[first], spy.close[last]):
        result.spy_return = spy.close[last] / spy.open[first] - 1
    return result


# --- check --------------------------------------------------------------------------------------------


def check(conn: sqlite3.Connection) -> tuple[list[str], bool]:
    """Engine checks on the real DB. Prints counts and invariants only, no rule performance."""
    market = Market(conn)
    if not market.n_days:
        return ["Exits: no SPY prices yet (run prices.fetch, then analytics.outcomes)"], False
    cols = ", ".join(f"o.ret_{h}, o.abn_ret_{h}" for h in HORIZONS)
    rows = conn.execute(
        f"""
        SELECT t.trade_id, t.doc_id, t.member_id, t.symbol, o.d0_date, {cols}
        FROM trade_outcomes o JOIN trades t ON t.trade_id = o.trade_id
        WHERE o.copyable = 1 AND o.d0_date IS NOT NULL AND t.symbol IS NOT NULL
        """
    ).fetchall()
    compared, bad = 0, []
    for r in rows:
        d0 = market.pos.get(r["d0_date"])
        if d0 is None:
            continue
        sig = Signal(r["trade_id"], r["doc_id"], r["member_id"], r["symbol"], None, d0)
        for h in HORIZONS:
            if r[f"ret_{h}"] is None:
                continue
            compared += 1
            tr = evaluate(market, sig, FixedHold(h))
            ok = (tr is not None and tr.exit.idx == d0 + h and tr.exit.reason == "hold"
                  and abs(tr.ret - r[f"ret_{h}"]) < TOLERANCE
                  and tr.abn is not None and abs(tr.abn - r[f"abn_ret_{h}"]) < TOLERANCE)
            if not ok:
                bad.append(f"    trade {r['trade_id']} {r['symbol']} h={h}: outcomes {r[f'ret_{h}']:+.6f}, "
                           f"engine {'-' if tr is None else f'{tr.ret:+.6f} ({tr.exit.reason})'}")
    lines = [f"FixedHold(h) at zero cost vs trade_outcomes: {compared} trade-horizons compared, "
             f"{len(bad)} mismatched"]
    lines += bad[:10]

    signals = load_signals(market)
    start = market.cal.days[max(market.quarter_start(market.n_days - 1) - 4 * 63, 0)]
    picked = select(market, signals, "ranked")
    res = run_portfolio(market, picked, TrailingStop(0.10), start=start)
    cash_ok = res.min_settled is None or res.min_settled >= -TOLERANCE
    lines += [
        f"Portfolio ledger (ranked universe, trailing stop, D0 from {start}): {res.signals} signals, "
        f"{len(res.trades)} bought, skipped {res.skipped_cash} for settled cash / {res.skipped_held} held / "
        f"{res.skipped_full} full, {res.no_entry} without a D0 open, {res.data_end} exited at data end",
        f"  lowest settled cash after a buy: {res.min_settled if res.min_settled is not None else '-'} "
        f"({'ok' if cash_ok else 'NEGATIVE'})",
        "  No rule performance here: that's M4.2's out-of-sample walk-forward.",
    ]
    return lines, not bad and cash_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="verify the engine against trade_outcomes")
    args = parser.parse_args(argv)
    if not args.check:
        parser.print_help()
        return 0
    logs.setup()
    lines, ok = check(connect())
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
