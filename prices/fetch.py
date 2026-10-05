"""Daily prices (Milestone 2.3): python -m prices.fetch [--all] [--symbol X] [--gaps] [--refetch-partial]

Fetches daily OHLCV for every traded symbol plus SPY into `prices` (ticker = our symbol form, e.g. BRK.B).

  universe   trades with a symbol (listed | renamed | unlisted), open positions, and SPY. Each symbol is needed
             from 10 days before its earliest trade (trade-date returns are context) through today.
  nightly    only "active" symbols: a filing available in the last 150 days (horizons still maturing to 60
             trading days), open positions, SPY, and symbols never fetched. --all refreshes every symbol.
  full fetch a new symbol, or a known one (active or not) with a newly found older trade: price_coverage.needed_from
             holds the start last fetched, so a backfilled older trade is noticed. --refetch-partial redoes
             symbols whose history starts after their first disclosure (repairs histories cut short before that fix).
  increments a symbol with bars is fetched from its last bar minus 5 days. If an overlapping bar's close or
             adj_close changed (a later split or dividend re-bases Yahoo's history), its whole history is
             refetched and replaced, so a symbol never mixes adjustment bases.
  coverage   price_coverage per symbol: ok | partial (starts after its first disclosure, or ends a week before
             SPY's last bar: delisted) | missing (Yahoo has nothing). --gaps prints the report.

Prices: adj_close is split- and dividend-adjusted (use it for returns). open/high/low/close are Yahoo's
split-adjusted, dividend-unadjusted values (use open for entry-price analysis; ratios over a few days are
unaffected). Free data drops delisted tickers: conclusions are subject to survivorship bias until we switch
to a paid provider.
"""

import argparse
import logging
import math
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from common import log as logs
from db import connect

log = logging.getLogger("prices.fetch")

BENCHMARK = "SPY"
PRICED_STATUSES = ("listed", "renamed", "unlisted")
CONTEXT_DAYS = 10  # bars before the earliest trade date
HISTORY_FLOOR = date(2019, 12, 1)  # filer typos in tx_date shouldn't pull decades of history
OVERLAP_DAYS = 5
ACTIVE_DAYS = 150
STALE_DAYS = 7  # calendar days behind SPY's last bar (about 5 trading days) before a symbol counts as partial
START_SLACK_DAYS = 4  # a disclosure on a Friday night has its first bar on Monday
DRIFT = 5e-4  # relative change in an overlapping bar that means Yahoo re-based the history
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 2.0


@dataclass(frozen=True)
class Bar:
    date: str  # ISO
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None


class Source(Protocol):
    def history(self, symbols: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        """Daily bars from start to end (inclusive) per symbol; symbols without data map to []."""


class YahooSource:
    """yfinance in batches, with a pause between them."""

    def __init__(self, pause: Callable[[], None] = lambda: time.sleep(BATCH_PAUSE_SECONDS)):
        self.pause = pause

    def history(self, symbols: list[str], start: date, end: date) -> dict[str, list[Bar]]:
        import yfinance as yf

        logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # it logs every unknown symbol as an error
        yahoo = {to_yahoo(s): s for s in symbols}
        frame = yf.download(list(yahoo), start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                            auto_adjust=False, actions=False, group_by="ticker", threads=False, progress=False)
        result: dict[str, list[Bar]] = {s: [] for s in symbols}
        if frame is None or frame.empty:
            return result
        for ysym, symbol in yahoo.items():
            if ysym not in frame.columns.get_level_values(0):
                continue
            result[symbol] = bars_from_frame(frame[ysym])
        return result


def to_yahoo(symbol: str) -> str:
    """Our symbols use the exchanges' form (BRK.B); Yahoo uses BRK-B."""
    return symbol.replace(".", "-")


def bars_from_frame(frame) -> list[Bar]:
    """Bars from one ticker's DataFrame (columns Open, High, Low, Close, Adj Close, Volume); empty rows dropped."""
    bars = []
    for day, row in frame.iterrows():
        close = _num(row.get("Close"))
        if close is None:
            continue
        volume = _num(row.get("Volume"))
        bars.append(Bar(day.date().isoformat(), _num(row.get("Open")), _num(row.get("High")), _num(row.get("Low")),
                        close, _num(row.get("Adj Close")), int(volume) if volume is not None else None))
    return bars


def _num(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


# --- what to fetch ----------------------------------------------------------------------------------


@dataclass
class Need:
    symbol: str
    fetch_from: date  # earliest bar wanted (trade-date context)
    first_disclosure: date | None  # D0 needs bars from here on (None for SPY / positions only)
    active: bool = False


def universe(conn: sqlite3.Connection, today: date) -> dict[str, Need]:
    needs: dict[str, Need] = {}
    active_since = (today - timedelta(days=ACTIVE_DAYS)).isoformat()
    rows = conn.execute(
        f"""
        SELECT t.symbol, MIN(t.tx_date) AS first_tx, MIN(t.disclosure_date) AS first_disclosure,
               MAX(COALESCE(f.available_at, f.first_seen_at)) AS last_available
        FROM trades t JOIN filings f ON f.doc_id = t.doc_id
        WHERE t.symbol IS NOT NULL AND t.ticker_status IN ({','.join('?' * len(PRICED_STATUSES))})
        GROUP BY t.symbol
        """,
        PRICED_STATUSES,
    ).fetchall()
    for r in rows:
        dates = [date.fromisoformat(d) for d in (r["first_tx"], r["first_disclosure"]) if d]
        if not dates:
            continue
        fetch_from = max(min(dates) - timedelta(days=CONTEXT_DAYS), HISTORY_FLOOR)
        disclosed = date.fromisoformat(r["first_disclosure"]) if r["first_disclosure"] else None
        needs[r["symbol"]] = Need(r["symbol"], fetch_from, disclosed,
                                  active=(r["last_available"] or "") >= active_since)
    for r in conn.execute("SELECT UPPER(ticker) AS symbol, MIN(buy_date) AS since FROM my_positions "
                          "WHERE status = 'open' GROUP BY UPPER(ticker)").fetchall():
        start = date.fromisoformat(r["since"]) - timedelta(days=CONTEXT_DAYS)
        need = needs.setdefault(r["symbol"], Need(r["symbol"], start, None))
        need.fetch_from, need.active = min(need.fetch_from, start), True
    earliest = min((n.fetch_from for n in needs.values()), default=today - timedelta(days=CONTEXT_DAYS))
    needs[BENCHMARK] = Need(BENCHMARK, earliest, None, active=True)
    return needs


@dataclass
class Stored:
    first_date: str
    last_date: str


def stored(conn: sqlite3.Connection) -> dict[str, Stored]:
    return {r["ticker"]: Stored(r["first_date"], r["last_date"]) for r in conn.execute(
        "SELECT ticker, MIN(date) AS first_date, MAX(date) AS last_date FROM prices GROUP BY ticker")}


def plan(needs: dict[str, Need], have: dict[str, Stored], tried: dict[str, str | None], *,
         all_symbols: bool, refetch: set[str] = frozenset()) -> dict[str, date]:
    """symbol -> fetch start. `tried` is price_coverage.needed_from: the fetch start of the last check.
    An older trade (e.g. from a backfill) triggers a full fetch even for an inactive symbol; `refetch` forces one."""
    starts: dict[str, date] = {}
    for symbol, need in needs.items():
        known = symbol in tried
        earlier_need = bool(known and tried[symbol] and need.fetch_from < date.fromisoformat(tried[symbol]))
        if not (all_symbols or need.active or not known or earlier_need or symbol in refetch):
            continue
        bars = have.get(symbol)
        if bars is None or earlier_need or symbol in refetch:
            starts[symbol] = need.fetch_from  # a full fetch: nothing stored yet, or a new, older trade
        else:
            starts[symbol] = date.fromisoformat(bars.last_date) - timedelta(days=OVERLAP_DAYS)
    return starts


def batches(starts: dict[str, date], size: int = BATCH_SIZE) -> Iterable[tuple[date, list[str]]]:
    """Symbols grouped by similar start date; each batch fetches from its earliest start."""
    ordered = sorted(starts, key=lambda s: (starts[s], s))
    for i in range(0, len(ordered), size):
        chunk = ordered[i:i + size]
        yield min(starts[s] for s in chunk), chunk


# --- storage ------------------------------------------------------------------------------------------


def rebased(conn: sqlite3.Connection, symbol: str, bars: list[Bar]) -> bool:
    """True if a fetched bar disagrees with the stored one for the same day (Yahoo re-based the history)."""
    if not bars:
        return False
    old = {r["date"]: r for r in conn.execute(
        "SELECT date, close, adj_close FROM prices WHERE ticker = ? AND date BETWEEN ? AND ?",
        (symbol, bars[0].date, bars[-1].date))}
    # The latest stored bar may be a partial day (fetched during market hours); a real re-base moves every
    # earlier bar too.
    old.pop(max(old, default=None), None)
    for bar in bars:
        prev = old.get(bar.date)
        if prev is None:
            continue
        for new, before in ((bar.close, prev["close"]), (bar.adj_close, prev["adj_close"])):
            if new is not None and before and abs(new / before - 1) > DRIFT:
                return True
    return False


def save(conn: sqlite3.Connection, symbol: str, bars: list[Bar], *, replace: bool = False) -> None:
    if replace:
        conn.execute("DELETE FROM prices WHERE ticker = ?", (symbol,))
    conn.executemany(
        """
        INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker, date) DO UPDATE SET
          open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,
          adj_close = excluded.adj_close, volume = excluded.volume
        """,
        [(symbol, b.date, b.open, b.high, b.low, b.close, b.adj_close, b.volume) for b in bars],
    )


def update_coverage(conn: sqlite3.Connection, needs: dict[str, Need], checked: set[str], now: str) -> None:
    """Recomputes price_coverage for every symbol in the universe. needed_from (the start plan() compares against)
    and checked_at only move for fetched symbols: recording an unfetched start would hide the gap for good."""
    have = stored(conn)
    counts = dict(conn.execute("SELECT ticker, COUNT(*) FROM prices GROUP BY ticker").fetchall())
    spy_last = have[BENCHMARK].last_date if BENCHMARK in have else None
    for symbol, need in needs.items():
        bars = have.get(symbol)
        note = None
        if bars is None:
            status, note = "missing", "no data from the price source"
        else:
            status = "ok"
            first = date.fromisoformat(bars.first_date)
            if need.first_disclosure and first > need.first_disclosure + timedelta(days=START_SLACK_DAYS):
                status, note = "partial", f"starts {bars.first_date}, after first disclosure {need.first_disclosure}"
            elif spy_last and date.fromisoformat(bars.last_date) < date.fromisoformat(spy_last) - timedelta(
                    days=STALE_DAYS):
                status, note = "partial", f"ends {bars.last_date} (delisted?)"
        conn.execute(
            """
            INSERT INTO price_coverage (symbol, needed_from, first_date, last_date, n_rows, status, checked_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol) DO UPDATE SET
              first_date = excluded.first_date, last_date = excluded.last_date,
              n_rows = excluded.n_rows, status = excluded.status, note = excluded.note,
              needed_from = CASE WHEN ? THEN excluded.needed_from ELSE price_coverage.needed_from END,
              checked_at = CASE WHEN ? THEN excluded.checked_at ELSE price_coverage.checked_at END
            """,
            (symbol, need.fetch_from.isoformat(), bars.first_date if bars else None, bars.last_date if bars else None,
             counts.get(symbol, 0), status, now, note, symbol in checked, symbol in checked),
        )


# --- orchestration ------------------------------------------------------------------------------------


@dataclass
class Summary:
    symbols: int = 0
    fetched: int = 0
    rows: int = 0
    rebased: int = 0
    empty: int = 0
    failed_batches: int = 0
    coverage: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    conn: sqlite3.Connection,
    source: Source,
    *,
    all_symbols: bool = False,
    only: list[str] | None = None,
    refetch_partial: bool = False,
    today: date | None = None,
    now: Callable[[], str] = utc_now,
) -> Summary:
    today = today or date.today()
    summary = Summary()
    needs = universe(conn, today)
    if only:
        needs = {s: needs.get(s) or Need(s, today - timedelta(days=365), None) for s in (x.upper() for x in only)}
        for need in needs.values():
            need.active = True
    tried = dict(conn.execute("SELECT symbol, needed_from FROM price_coverage").fetchall())
    refetch = set()
    if refetch_partial:  # one-off repair: histories that start after the symbol's first disclosure
        refetch = {s for (s,) in conn.execute(
            "SELECT symbol FROM price_coverage WHERE status = 'partial' AND first_date > needed_from")} & set(needs)
    starts = plan(needs, stored(conn), tried, all_symbols=all_symbols, refetch=refetch)
    summary.symbols = len(starts)

    to_replace: dict[str, date] = {}
    batch_list = list(batches(starts))
    for start, symbols in batch_list:
        try:
            result = source.history(symbols, start, today)
        except Exception as e:  # one bad batch shouldn't stop the others
            summary.failed_batches += 1
            log.warning("Price batch from %s (%d symbols) failed: %s", start, len(symbols), e)
            continue
        for symbol in symbols:
            bars = [b for b in result.get(symbol) or [] if b.date >= starts[symbol].isoformat()]  # batch start <= own
            summary.fetched += 1
            if not bars:
                summary.empty += 1
                continue
            if rebased(conn, symbol, bars):
                to_replace[symbol] = needs[symbol].fetch_from
                continue
            save(conn, symbol, bars)
            summary.rows += len(bars)
        conn.commit()

    for start, symbols in batches(to_replace):
        try:
            result = source.history(symbols, start, today)
        except Exception as e:
            summary.failed_batches += 1
            log.warning("Refetching re-based history failed: %s", e)
            continue
        for symbol in symbols:
            bars = [b for b in result.get(symbol) or [] if b.date >= to_replace[symbol].isoformat()]
            if bars:  # one transaction per symbol: delete + insert, so it never mixes adjustment bases
                save(conn, symbol, bars, replace=True)
                conn.commit()
                summary.rebased += 1
                summary.rows += len(bars)
                log.info("%s: history re-based by the source; replaced %d bars", symbol, len(bars))

    if batch_list and summary.failed_batches == len(batch_list):
        summary.errors.append("prices: every batch failed (price source down?)")
    elif BENCHMARK in starts and not stored(conn).get(BENCHMARK):
        summary.errors.append(f"prices: no {BENCHMARK} data (price source down?)")

    update_coverage(conn, needs, set(starts), now())
    conn.commit()
    summary.coverage = dict(conn.execute("SELECT status, COUNT(*) FROM price_coverage GROUP BY status").fetchall())
    return summary


# --- gap report ---------------------------------------------------------------------------------------


def gaps(conn: sqlite3.Connection, limit: int = 40) -> list[str]:
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM price_coverage GROUP BY status").fetchall())
    if not counts:
        return ["Prices: not fetched yet. Run: python -m prices.fetch --all"]
    total = conn.execute(
        f"SELECT COUNT(*) FROM trades WHERE ticker_status IN ({','.join('?' * len(PRICED_STATUSES))})",
        PRICED_STATUSES).fetchone()[0]
    covered = conn.execute(
        "SELECT COUNT(*) FROM trades t JOIN price_coverage c ON c.symbol = t.symbol WHERE c.status = 'ok'"
    ).fetchone()[0]
    share = f" ({100 * covered / total:.0f}%)" if total else ""
    lines = [f"Prices: {sum(counts.values())} symbols: ok {counts.get('ok', 0)}, partial {counts.get('partial', 0)}, "
             f"missing {counts.get('missing', 0)}; {covered} of {total} trades with a symbol fully covered{share}"]
    rows = conn.execute(
        """
        SELECT c.symbol, c.status, c.note, COUNT(t.trade_id) AS trades
        FROM price_coverage c LEFT JOIN trades t ON t.symbol = c.symbol
        WHERE c.status <> 'ok' GROUP BY c.symbol ORDER BY trades DESC, c.symbol LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if rows:
        lines.append(f"  Gaps, most-traded first (top {limit}):")
        lines += [f"    {r['symbol']:8} {r['status']:8} {r['trades']:4} trades  {r['note'] or ''}" for r in rows]
    lines.append("  Free data drops delisted tickers: results carry survivorship bias until a paid provider.")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--all", action="store_true", help="refresh every symbol, not just active ones")
    parser.add_argument("--symbol", action="append", help="fetch only this symbol (repeatable)")
    parser.add_argument("--gaps", action="store_true", help="print the gap report and exit")
    parser.add_argument("--refetch-partial", action="store_true",
                        help="refetch the full history of symbols whose stored bars start late (one-off repair)")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    if not args.gaps:
        s = run(conn, YahooSource(), all_symbols=args.all, only=args.symbol, refetch_partial=args.refetch_partial)
        log.info("Prices: %d symbols fetched (%d empty), %d bars, %d re-based, %d failed batch(es); coverage %s",
                 s.fetched, s.empty, s.rows, s.rebased, s.failed_batches, s.coverage)
        for error in s.errors:
            log.error("%s", error)
        if s.errors:
            return 1
    print("\n".join(gaps(conn)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
