"""Sector, industry and market-cap bucket per traded symbol (Milestone 2.2).

refresh()  fetches reference data per symbol into `securities` (Yahoo quote info: name, type, sector, industry,
           market cap). Network, so it runs in the nightly pipeline stage: symbols never fetched first, then
           ones older than 90 days (30 for symbols Yahoo didn't know), at most 300 per run.
assign()   copies sector/industry onto trades and sets trades.mcap_bucket. No network; runs with every enrich.

The bucket is the company's size at disclosure: today's market cap scaled by the price change since then,
market_cap * close(disclosure date) / close(when market_cap was fetched). Closes are split-adjusted, and the
ratio works for multi-class companies (BRK.B) where share counts don't. Without prices it's today's cap.
ETFs and funds get sector 'ETF' and no bucket.
"""

import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

log = logging.getLogger("enrich.securities")

PRICED_STATUSES = ("listed", "renamed", "unlisted")
FUND_TYPES = {"ETF", "MUTUALFUND"}
FRESH_DAYS = {"ok": 90, "missing": 30}
MAX_PER_RUN = 300
MAX_CONSECUTIVE_FAILURES = 5
BUCKETS = [(200e9, "mega"), (10e9, "large"), (2e9, "mid"), (300e6, "small"), (0, "micro")]


class InfoSource(Protocol):
    def info(self, symbol: str) -> dict:
        """Quote info for a symbol (Yahoo's keys); {} or a dict without quoteType if unknown."""


class YahooInfo:
    def info(self, symbol: str) -> dict:
        import yfinance as yf

        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        return yf.Ticker(symbol.replace(".", "-")).info or {}


@dataclass
class Summary:
    due: int = 0
    fetched: int = 0
    ok: int = 0
    missing: int = 0
    failed: int = 0


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def due_symbols(conn: sqlite3.Connection, today: date) -> list[str]:
    """Symbols to (re)fetch: never fetched first, then the stalest."""
    marks = ",".join("?" * len(PRICED_STATUSES))
    rows = conn.execute(
        f"""
        SELECT u.symbol, s.status, s.updated_at FROM (
          SELECT DISTINCT symbol FROM trades WHERE symbol IS NOT NULL AND ticker_status IN ({marks})
          UNION SELECT DISTINCT UPPER(ticker) FROM my_positions WHERE status = 'open'
        ) u LEFT JOIN securities s ON s.symbol = u.symbol
        ORDER BY s.updated_at IS NOT NULL, s.updated_at, u.symbol
        """,
        PRICED_STATUSES,
    ).fetchall()
    due = []
    for r in rows:
        if r["updated_at"] is None:
            due.append(r["symbol"])
            continue
        age = today - date.fromisoformat(r["updated_at"][:10])
        if age >= timedelta(days=FRESH_DAYS.get(r["status"], 30)):
            due.append(r["symbol"])
    return due


def record(info: dict) -> tuple:
    """(name, quote_type, sector, industry, market_cap, shares_outstanding, status) from quote info."""
    quote_type = info.get("quoteType")
    if not quote_type or quote_type == "NONE":
        return None, None, None, None, None, None, "missing"
    fund = quote_type in FUND_TYPES
    return (
        info.get("longName") or info.get("shortName"),
        quote_type,
        "ETF" if fund else info.get("sector"),
        info.get("category") if fund else info.get("industry"),
        None if fund else info.get("marketCap"),
        info.get("sharesOutstanding"),
        "ok",
    )


def refresh(
    conn: sqlite3.Connection,
    source: InfoSource,
    *,
    today: date | None = None,
    limit: int = MAX_PER_RUN,
    now: Callable[[], str] = utc_now,
    pause: Callable[[], None] = lambda: time.sleep(1.0),
) -> Summary:
    due = due_symbols(conn, today or date.today())
    summary = Summary(due=len(due))
    consecutive = 0
    for i, symbol in enumerate(due[:limit]):
        if i:
            pause()
        try:
            info = source.info(symbol)
        except Exception as e:  # a network or parsing error for one symbol; retried next night
            summary.failed += 1
            consecutive += 1
            log.warning("Quote info for %s failed: %s", symbol, e)
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                log.error("Stopping after %d failures in a row; will retry next night", consecutive)
                break
            continue
        consecutive = 0
        row = record(info)
        conn.execute(
            """
            INSERT INTO securities (symbol, name, quote_type, sector, industry, market_cap, shares_outstanding,
                                    status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol) DO UPDATE SET
              name = excluded.name, quote_type = excluded.quote_type, sector = excluded.sector,
              industry = excluded.industry, market_cap = excluded.market_cap,
              shares_outstanding = excluded.shares_outstanding, status = excluded.status,
              updated_at = excluded.updated_at
            """,
            (symbol, *row, now()),
        )
        conn.commit()
        summary.fetched += 1
        summary.ok += row[-1] == "ok"
        summary.missing += row[-1] == "missing"
    return summary


def bucket(market_cap: float | None) -> str | None:
    if not market_cap or market_cap <= 0:
        return None
    return next(name for floor, name in BUCKETS if market_cap >= floor)


def assign(conn: sqlite3.Connection) -> Counter:
    """Sets trades.sector, industry and mcap_bucket from `securities` and `prices`. Returns bucket counts."""
    rows = conn.execute(
        """
        SELECT t.trade_id, s.sector, s.industry, s.market_cap,
               (SELECT p.close FROM prices p WHERE p.ticker = t.symbol AND p.date <= t.disclosure_date
                ORDER BY p.date DESC LIMIT 1) AS close_then,
               (SELECT p.close FROM prices p WHERE p.ticker = t.symbol AND p.date <= substr(s.updated_at, 1, 10)
                ORDER BY p.date DESC LIMIT 1) AS close_ref
        FROM trades t LEFT JOIN securities s ON s.symbol = t.symbol AND s.status = 'ok'
        """
    ).fetchall()
    counts: Counter = Counter()
    updates = []
    for r in rows:
        cap = r["market_cap"]
        if cap and r["close_then"] and r["close_ref"]:
            cap = cap * r["close_then"] / r["close_ref"]
        size = bucket(cap)
        counts[size or "none"] += 1
        updates.append((r["sector"], r["industry"], size, r["trade_id"]))
    conn.executemany("UPDATE trades SET sector = ?, industry = ?, mcap_bucket = ? WHERE trade_id = ?", updates)
    return counts
