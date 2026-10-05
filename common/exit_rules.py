"""Exit rules and the daily-bar exit simulator, shared by analytics/exits.py (backtests) and alerts/positions.py
(watching the positions you logged). Pure functions, no database: stages keep talking through tables.

  rules      FixedHold(days), StopTarget(stop, target), TrailingStop(pct), AtrStop(mult, n), MemberSale. Every rule
             also exits at the close after max_hold trading days (60).
  fills      conservative for daily bars: an open through a stop or target fills at the open; a stop and a target
             both inside one day's range count as the stop; an intraday fill is at the level; a trailing high
             updates only after that day's stop check. ATR uses bars before the entry day only.
  labels     label(rule) is the stored form (my_positions.exit_rule, exit_rules.label), e.g. trailing_stop(pct=0.1);
             parse_rule() reverses it (None for anything else, e.g. older free text); describe() is plain English.
"""

import re
import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import ClassVar

MAX_HOLD = 60

# --- prices -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Series:
    """A symbol's adjusted bars aligned to the calendar; None where a value is missing."""

    open: list
    high: list
    low: list
    close: list
    last: int  # index of the last bar with a close (-1 if none)


def positive(*values) -> bool:
    return all(v is not None and v > 0 for v in values)


def make_series(rows: Iterable, pos: dict[str, int], n_days: int) -> Series:
    """rows: (date, open, high, low, close, adj_close)."""
    o, h, lo, c = ([None] * n_days for _ in range(4))
    for day, open_, high, low, close, adj in rows:
        i = pos.get(day)
        if i is None or not positive(adj):
            continue
        c[i] = adj
        if not positive(open_, close):
            continue
        f = adj / close
        o[i] = open_ * adj / close  # same arithmetic as outcomes.adj_open
        h[i] = max(high * f if positive(high) else 0.0, o[i], adj)
        lo[i] = min(low * f if positive(low) else float("inf"), o[i], adj)
    last = max((i for i, v in enumerate(c) if v is not None), default=-1)
    return Series(o, h, lo, c, last)




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


def simulate(s: Series, d0: int, rule: Rule, sale: int | None = None, *, entry: float | None = None,
             bought_intraday: bool = False) -> Exit | None:
    """When and where a position bought on day d0 exits under `rule` (reason 'open' if it hasn't yet); None without
    an entry price. sale: index of the member's next sale D0 after d0 (MemberSale only).

    Backtests buy at the D0 open. A real position (alerts/positions.py) passes its fill as `entry` and
    bought_intraday=True: the buy day's low and high may have come before the fill, so on that day only the close
    is checked, and the trailing high starts at max(fill, close)."""
    if entry is None:
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
        if bought_intraday and i == d0:
            if stop is not None and close <= stop:
                return Exit(i, close, "close", "stop")
            if target is not None and close >= target:
                return Exit(i, close, "close", "target")
            if i >= end:
                return Exit(i, close, "close", "hold")
            high = max(high, close)
            continue
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


def label(rule: Rule) -> str:
    """e.g. trailing_stop(pct=0.1)"""
    shown = {k: v for k, v in params(rule).items() if not (k == "max_hold" and v == MAX_HOLD)}
    return f"{rule.name}({', '.join(f'{k}={v}' for k, v in shown.items())})"


RULES = {cls.name: cls for cls in (FixedHold, StopTarget, TrailingStop, AtrStop, MemberSale)}
LABEL = re.compile(r"^\s*([a-z_]+)\((.*)\)\s*$")


def _value(text: str):
    if text == "None":
        return None
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    raise ValueError(text)


def parse_rule(text: str | None) -> Rule | None:
    """The rule a label() names, or None (not a label: the position isn't watched)."""
    match = LABEL.match(text or "")
    if not match or match.group(1) not in RULES:
        return None
    kwargs = {}
    try:
        for part in filter(None, (x.strip() for x in match.group(2).split(","))):
            key, sep, value = part.partition("=")
            if not sep:
                return None
            kwargs[key.strip()] = _value(value.strip())
        return RULES[match.group(1)](**kwargs)
    except (TypeError, ValueError):
        return None


def describe(rule: Rule) -> str:
    """Plain English, for emails and the app."""
    if isinstance(rule, FixedHold):
        return f"Hold {rule.days} trading day{'s' if rule.days != 1 else ''}, then sell at the close"
    if isinstance(rule, StopTarget):
        parts = []
        if rule.stop is not None:
            parts.append(f"falls {rule.stop:.0%} below")
        if rule.target is not None:
            parts.append(f"rises {rule.target:.0%} above")
        text = f"Sell if it {' or '.join(parts)} your buy price" if parts else "Hold"
    elif isinstance(rule, TrailingStop):
        text = f"Trailing stop: sell if it falls {rule.pct:.0%} below its highest price since you bought"
    elif isinstance(rule, AtrStop):
        text = (f"Volatility stop: sell if it falls {rule.mult:g}x its {rule.n}-day average true range below its "
                f"highest price since you bought")
    else:
        text = "Sell when the member you copied discloses a sale of it"
    return f"{text} (or after {rule.max_hold} trading days)"
