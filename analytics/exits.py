"""Exit backtests (Milestones 4.1-4.2, F4): python -m analytics.exits [--report] [--check] [--universe U]

Simulates exit rules on daily bars for copyable BUYs, entered at the D0 open (hard rule 3), with costs, slippage
and the Roth T+1 settled-cash constraint, then validates them walk-forward. Only out-of-sample results are stored.

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

walk-forward (M4.2)
  quarters   test quarters have TRAIN_YEARS (2) of calendar before them and every D0 + 60 already passed.
  training   for each test quarter, the signals with D0 in the 2 years before it that exited, under every GRID rule,
             before the quarter's first day (no leakage; all rules scored on the same trades). Score = mean over
             filings of the net excess vs SPY. Best score wins, ties to the earlier (simpler) GRID rule; fewer than
             MIN_TRAIN_FILINGS (50) filings falls back to FixedHold(20).
  books      walk_forward: each quarter's signals under its chosen rule, all quarters in one continuous T+1 ledger
             (cash and positions carry over). hold_1 ... hold_60: the same signals held h trading days, one ledger
             each (the baselines at the leaderboard's horizons).
  rows       exit_backtests, replaced each run: per book, one row per test quarter (trades by D0 quarter, per-filing
             stats; the ledger's return, drawdown and SPY over the quarter's days) and an 'all' row for the span.

--check verifies the engine on the real DB: zero-cost FixedHold(h) must reproduce trade_outcomes ret_h / abn_ret_h,
and a portfolio run must never leave settled cash negative. It writes nothing.
"""

import argparse
import json
import logging
import sqlite3
import statistics
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
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
    trades: list[TradeResult] = field(default_factory=list)  # bought
    skipped: list[tuple[TradeResult, str]] = field(default_factory=list)  # (trade, cash | held | full)
    equity: list[tuple[str, float]] = field(default_factory=list)  # (date, equity at the close)


def ledger(market: Market, trades: list[TradeResult], costs: Costs | None = None, *, capital: float = 100_000.0,
           size: float = 0.05, max_positions: int = 20, rule: str = "mixed", rule_params: dict | None = None) -> Result:
    """Trades already evaluated (each under its own rule) through one book with T+1 settled cash."""
    costs = costs or Costs()
    days = market.cal.days
    result = Result(rule, rule_params or {})
    buys: dict[int, list[TradeResult]] = defaultdict(list)
    for tr in sorted(trades, key=lambda t: (t.signal.d0, t.signal.doc_id, t.signal.trade_id)):
        buys[tr.signal.d0].append(tr)
    if not buys:
        return result

    first = min(buys)
    last = max(tr.exit.idx for tr in trades)
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

    def skip(tr: TradeResult, reason: str) -> None:
        result.skipped.append((tr, reason))
        setattr(result, f"skipped_{reason}", getattr(result, f"skipped_{reason}") + 1)

    for i in range(first, last + 1):
        settled += sum(a for d, a in pending if d <= i)
        pending = [(d, a) for d, a in pending if d > i]
        sell(i, opening=True)
        for tr in buys.get(i, []):
            sym = tr.signal.symbol
            if sym in positions:
                skip(tr, "held")
                continue
            if len(positions) >= max_positions:
                skip(tr, "full")
                continue
            amount = size * equity_prev
            if amount > settled:
                skip(tr, "cash")
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


def run_portfolio(market: Market, signals: list[Signal], rule: Rule, costs: Costs | None = None, *,
                  sales: dict | None = None, capital: float = 100_000.0, size: float = 0.05,
                  max_positions: int = 20, start: str | None = None, end: str | None = None) -> Result:
    """Trade `signals` with D0 in [start, end] under `rule` with T+1 settled cash."""
    costs = costs or Costs()
    days = market.cal.days
    chosen = [s for s in signals if (start is None or days[s.d0] >= start) and (end is None or days[s.d0] <= end)]
    if isinstance(rule, MemberSale) and sales is None:
        sales = sale_days(market)
    trades, no_entry = [], 0
    for sig in chosen:
        tr = evaluate(market, sig, rule, costs, next_sale(sales, sig) if sales else None)
        if tr is None:
            no_entry += 1
        else:
            trades.append(tr)
    result = ledger(market, trades, costs, capital=capital, size=size, max_positions=max_positions,
                    rule=rule.name, rule_params=params(rule))
    result.signals, result.no_entry = len(chosen), no_entry
    return result


# --- walk-forward (M4.2) ------------------------------------------------------------------------------

GRID: tuple[Rule, ...] = (  # small on purpose: every extra config is another chance to fit noise
    FixedHold(5), FixedHold(10), FixedHold(20), FixedHold(60),
    StopTarget(stop=0.08), StopTarget(stop=0.15), StopTarget(stop=0.08, target=0.20),
    TrailingStop(0.10), TrailingStop(0.20),
    AtrStop(mult=2), AtrStop(mult=3),
    MemberSale(),
)
BASELINES: tuple[FixedHold, ...] = tuple(FixedHold(h) for h in HORIZONS)  # the leaderboard's horizons
FALLBACK = FixedHold(20)
TRAIN_YEARS = 2
MIN_TRAIN_FILINGS = 50


def label(rule: Rule) -> str:
    """e.g. trailing_stop(pct=0.1)"""
    shown = {k: v for k, v in params(rule).items() if not (k == "max_hold" and v == MAX_HOLD)}
    return f"{rule.name}({', '.join(f'{k}={v}' for k, v in shown.items())})"


@dataclass(frozen=True)
class Quarter:
    start: int  # index of its first trading day
    end: int  # index of its last trading day


def calendar_quarters(market: Market) -> list[Quarter]:
    starts = sorted({market.quarter_start(i) for i in range(market.n_days)})
    return [Quarter(s, (starts[k + 1] if k + 1 < len(starts) else market.n_days) - 1) for k, s in enumerate(starts)]


def years_before(day: str, years: int) -> str:
    return f"{int(day[:4]) - years}{day[4:]}"


def test_quarters(market: Market) -> list[Quarter]:
    """Quarters with TRAIN_YEARS of calendar before them whose last D0 + MAX_HOLD has passed (all trades complete)."""
    days = market.cal.days
    return [q for q in calendar_quarters(market)
            if years_before(days[q.start], TRAIN_YEARS) >= days[0] and q.end + MAX_HOLD < market.n_days]


def window(market: Market, start: int, end: int) -> str:
    return f"{market.cal.days[start]}..{market.cal.days[end]}"


def filing_means(trades: Iterable[TradeResult]) -> tuple[int, float | None, float | None, float | None]:
    """(n_filings, mean return, mean excess, hit rate) with one observation per filing."""
    by_doc: dict[str, list[TradeResult]] = defaultdict(list)
    for tr in trades:
        if tr.abn is not None:
            by_doc[tr.signal.doc_id].append(tr)
    if not by_doc:
        return 0, None, None, None
    rets = [statistics.fmean(t.ret for t in ts) for ts in by_doc.values()]
    abns = [statistics.fmean(t.abn for t in ts) for ts in by_doc.values()]
    return len(abns), statistics.fmean(rets), statistics.fmean(abns), sum(a > 0 for a in abns) / len(abns)


class Evaluated:
    """evaluate() for every (signal, rule), computed once and reused across training windows."""

    def __init__(self, market: Market, costs: Costs):
        self.market, self.costs = market, costs
        self.sales = sale_days(market)
        self._cache: dict[tuple[int, Rule], TradeResult | None] = {}

    def __call__(self, sig: Signal, rule: Rule) -> TradeResult | None:
        key = (sig.trade_id, rule)
        if key not in self._cache:
            sale = next_sale(self.sales, sig) if isinstance(rule, MemberSale) else None
            self._cache[key] = evaluate(self.market, sig, rule, self.costs, sale)
        return self._cache[key]


def choose(ev: Evaluated, signals: list[Signal], q: Quarter) -> tuple[Rule, dict[Rule, float], int, str]:
    """(rule, training scores, training filings, train window) for test quarter q.

    Training trades: D0 in the TRAIN_YEARS before q, and exited (under every grid rule) before q's first day, so
    nothing from inside q leaks in and every rule is scored on the same trades."""
    days = ev.market.cal.days
    since = years_before(days[q.start], TRAIN_YEARS)
    train = []
    for sig in signals:
        if not (since <= days[sig.d0] < days[q.start]):
            continue
        results = [ev(sig, rule) for rule in GRID]
        if all(tr is not None and tr.abn is not None and tr.exit.idx < q.start for tr in results):
            train.append(sig)
    first = bisect_left(days, since)
    span = window(ev.market, first, q.start - 1)
    n = len({s.doc_id for s in train})
    if n < MIN_TRAIN_FILINGS:
        return FALLBACK, {}, n, span
    scores = {rule: filing_means(ev(s, rule) for s in train)[2] for rule in GRID}
    best = max(GRID, key=lambda r: (scores[r], -GRID.index(r)))  # ties: the earlier (simpler) rule
    return best, scores, n, span


@dataclass
class Book:
    name: str  # walk_forward | hold_<h>
    trades: list[TradeResult]  # every test signal under the book's rule(s): the per-filing stats use these
    result: Result  # the T+1 ledger over them: portfolio return, drawdown, cash skips
    picks: dict[Quarter, tuple[Rule, int, str, bool]] = field(default_factory=dict)  # rule, train n, window, fallback


def walk_forward(market: Market, universe="ranked", costs: Costs | None = None, **book) -> list[Book]:
    """The walk_forward book (each test quarter traded with the rule chosen on the years before it) and one
    fixed-hold book per leaderboard horizon, all over the same test quarters and signals."""
    costs = costs or Costs()
    ev = Evaluated(market, costs)
    signals = select(market, load_signals(market), universe)
    quarters = test_quarters(market)
    if not quarters:
        return []
    in_test = [(q, [s for s in signals if q.start <= s.d0 <= q.end]) for q in quarters]

    picks, chosen = {}, []
    for q, sigs in in_test:
        rule, _scores, n, span = choose(ev, signals, q)
        picks[q] = (rule, n, span, n < MIN_TRAIN_FILINGS)
        chosen += [tr for s in sigs if (tr := ev(s, rule)) is not None]
    books = [Book("walk_forward", chosen, ledger(market, chosen, costs, **book), picks)]
    for rule in BASELINES:
        trades = [tr for _, sigs in in_test for s in sigs if (tr := ev(s, rule)) is not None]
        books.append(Book(f"hold_{rule.days}", trades, ledger(market, trades, costs, rule=rule.name,
                                                              rule_params=params(rule), **book)))
    return books


def book_rows(market: Market, book: Book, quarters: list[Quarter], capital: float = 100_000.0) -> list[dict]:
    """One row per test quarter plus 'all'. Trade stats are per filing over every test signal with D0 in the
    window (the same filings for every book, so books differ only by their exits); return, drawdown and cash skips
    come from the book's T+1 ledger over the window's days."""
    days, spy = market.cal.days, market.spy
    equity = book.result.equity
    dates = [d for d, _ in equity]

    def equity_before(day: str, default: float) -> float:
        i = bisect_left(dates, day)
        return equity[i - 1][1] if i else default

    def spy_change(start: int, end: int) -> float | None:
        base = spy.close[start - 1] if start > 0 else spy.open[start]
        return spy.close[end] / base - 1 if _positive(base, spy.close[end]) else None

    rows = []
    for q in quarters + [None]:
        if q is None:  # the whole out-of-sample span, through the last exit
            start = quarters[0].start
            end = market.pos[dates[-1]] if dates else quarters[-1].end
        else:
            start, end = q.start, q.end
        lo, hi = (start, end) if q else (0, market.n_days)

        def in_q(tr: TradeResult, lo=lo, hi=hi) -> bool:
            return lo <= tr.signal.d0 <= hi

        taken = [tr for tr in book.trades if in_q(tr)]
        n_filings, mean_ret, mean_abn, hit = filing_means(taken)
        e0 = equity_before(days[start], capital)
        span = [e for d, e in equity if days[start] <= d <= days[end]]
        peak, dd = e0, 0.0
        for e in span:
            peak = max(peak, e)
            dd = max(dd, 1 - e / peak)
        if q is not None and q in book.picks:
            rule, _n, train, fallback = book.picks[q]
            name, rule_params = rule.name, {**params(rule), **({"fallback": True} if fallback else {})}
        elif book.name == "walk_forward":
            name, rule_params, train = "walk_forward", {"grid": [label(r) for r in GRID],
                                                        "train_years": TRAIN_YEARS}, None
        else:
            name, rule_params, train = book.result.rule, book.result.params, None
        rows.append({
            "book": book.name, "rule": name, "params": json.dumps(rule_params), "train_window": train,
            "test_window": window(market, start, end), "n_trades": len(taken), "n_filings": n_filings,
            "mean_ret": mean_ret, "mean_abn_ret": mean_abn, "hit_rate": hit, "max_drawdown": dd,
            "total_return": (span[-1] / e0 - 1) if span else None, "spy_return": spy_change(start, end),
            "skipped_cash": sum(1 for tr, why in book.result.skipped if why == "cash" and in_q(tr)),
        })
    return rows


@dataclass
class Summary:
    quarters: int = 0
    first_quarter: str | None = None
    last_quarter: str | None = None
    rows: int = 0
    fallbacks: int = 0
    beats: int = 0  # baselines the walk_forward book beat on mean excess per filing (whole span)
    errors: list[str] = field(default_factory=list)


ROW_COLUMNS = ("book", "rule", "params", "train_window", "test_window", "n_trades", "n_filings", "mean_ret",
               "mean_abn_ret", "hit_rate", "max_drawdown", "total_return", "spy_return", "skipped_cash")


def run(conn: sqlite3.Connection, universe: str = "ranked", *, now=None) -> Summary:
    """Walk-forward validation into exit_backtests (replaced whole)."""
    summary = Summary()
    market = Market(conn)
    if not market.n_days:
        summary.errors.append("exits: no SPY prices yet (run prices.fetch, then analytics.outcomes)")
        return summary
    quarters = test_quarters(market)
    books = walk_forward(market, universe)
    rows = [r for book in books for r in book_rows(market, book, quarters)]
    stamp = (now or (lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))()
    with conn:  # one transaction: the latest run only
        conn.execute("DELETE FROM exit_backtests")
        conn.executemany(
            f"INSERT INTO exit_backtests ({', '.join(ROW_COLUMNS)}, universe, computed_at) "
            f"VALUES ({', '.join('?' * (len(ROW_COLUMNS) + 2))})",
            [(*(r[c] for c in ROW_COLUMNS), universe, stamp) for r in rows])
    summary.quarters, summary.rows = len(quarters), len(rows)
    if quarters:
        summary.first_quarter = market.cal.days[quarters[0].start]
        summary.last_quarter = market.cal.days[quarters[-1].end]
    if books:
        summary.fallbacks = sum(fb for *_, fb in books[0].picks.values())
        alls = all_rows(rows)
        wf = alls.get("walk_forward")
        summary.beats = sum(1 for name, v in alls.items()
                            if name != "walk_forward" and wf is not None and v is not None and wf > v)
    return summary


def all_rows(rows: list[dict]) -> dict[str, float | None]:
    """book -> mean excess per filing over the whole span (each book's last row is its 'all' row)."""
    last: dict[str, dict] = {}
    for r in rows:
        last[r["book"]] = r
    return {book: r["mean_abn_ret"] for book, r in last.items()}


def report(conn: sqlite3.Connection) -> list[str]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM exit_backtests ORDER BY run_id")]
    if not rows:
        return ["Exit backtests: none yet. Run: python -m analytics.exits (after analytics.outcomes)"]
    by_book: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_book[r["book"]].append(r)
    alls = {book: rs[-1] for book, rs in by_book.items()}

    def pct(v):
        return "      -" if v is None else f"{v:+7.2%}"

    first = alls["walk_forward"]
    lines = [f"Exit rules, out of sample ({rows[0]['universe']} universe, D0 {first['test_window']}; trained on the "
             f"{TRAIN_YEARS} years before each quarter; net of slippage; one observation per filing)",
             "  book            filings  avg ret   excess    hit  portfolio  S&P 500  max DD  cash skips"]
    for book, r in alls.items():
        hit = "    -" if r["hit_rate"] is None else f"{r['hit_rate']:5.0%}"
        lines.append(f"  {book:14} {r['n_filings'] or 0:8}  {pct(r['mean_ret'])}  {pct(r['mean_abn_ret'])}  {hit}  "
                     f"{pct(r['total_return'])}  {pct(r['spy_return'])}  {r['max_drawdown']:6.1%}  "
                     f"{r['skipped_cash'] or 0:6}")
    wf = first["mean_abn_ret"]
    holds = [(b, r["mean_abn_ret"]) for b, r in alls.items() if b != "walk_forward"]
    beaten = [b for b, v in holds if wf is not None and v is not None and wf > v]
    lines.append(f"  Verdict: walk-forward beat {len(beaten)} of {len(holds)} fixed-hold baselines on mean excess per "
                 f"filing{' (' + ', '.join(beaten) + ')' if beaten else ''}.")
    lines.append("  Filings/excess/hit: every test signal under each book's exits (the same filings for all books). "
                 "Portfolio: the T+1 ledger (5% positions, idle cash earns nothing), so it isn't comparable with a "
                 "fully invested S&P 500.")

    hold20 = {r["test_window"]: r for r in by_book.get("hold_20", [])}
    lines.append("  Per quarter: chosen rule, excess per filing (walk-forward vs hold 20):")
    picks: dict[str, int] = defaultdict(int)
    for r in by_book["walk_forward"][:-1]:
        p = json.loads(r["params"] or "{}")
        fallback = p.pop("fallback", False)
        p.pop("max_hold", None)
        name = f"{r['rule']}({', '.join(f'{k}={v}' for k, v in p.items())})" + (" fallback" if fallback else "")
        picks[name] += 1
        base = hold20.get(r["test_window"], {}).get("mean_abn_ret")
        lines.append(f"    {r['test_window'][:10]}  {name:36} {r['n_filings'] or 0:4} filings  "
                     f"{pct(r['mean_abn_ret'])} vs {pct(base)}")
    lines.append("  Times chosen: " + ", ".join(f"{k} x{v}" for k, v in sorted(picks.items(), key=lambda kv: -kv[1])))
    lines.append("  Free data drops delisted tickers: results carry survivorship bias until a paid provider.")
    return lines


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
    parser.add_argument("--report", action="store_true", help="print the latest walk-forward results and exit")
    parser.add_argument("--universe", choices=("ranked", "all", "watchlist"), default="ranked")
    args = parser.parse_args(argv)
    logs.setup()
    conn = connect()
    if args.check:
        lines, ok = check(conn)
        print("\n".join(lines))
        return 0 if ok else 1
    if not args.report:
        s = run(conn, args.universe)
        log.info("Exit backtests: %d test quarters (%s to %s), %d rows, %d fallback quarter(s); walk-forward beat %d "
                 "of %d fixed holds", s.quarters, s.first_quarter, s.last_quarter, s.rows, s.fallbacks, s.beats,
                 len(BASELINES))
        for error in s.errors:
            log.error("%s", error)
        if s.errors:
            return 1
    print("\n".join(report(conn)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
