"""Ticker validation and filing delay for every trade.

trades.ticker stays exactly as parsed. Enrichment writes:
  symbol         what prices/alerts use; NULL when there is nothing to trade
  ticker_status  listed    on the Nasdaq/NYSE symbol lists (share classes normalized: BRK-B -> BRK.B)
                 renamed   resolved through enrich/ticker_aliases.csv (old,new,date,until,note); with an `until`
                           date the alias covers only trades dated before it
                 delisted  the alias's new symbol is "-": the company is gone and its ticker was reused by another
                           security (FB is now an ETF), so pricing it as filed would be wrong; symbol = NULL
                 unlisted  a ticker that isn't on the lists (OTC ADRs, delisted); symbol = the ticker as filed
                 none      no ticker (bonds, funds, private holdings)
  is_etf         from the symbol lists
  filing_delay_days  calendar days from the trade to the disclosure
"""

import csv
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

ALIASES = Path(__file__).with_name("ticker_aliases.csv")
CLASS_SEPARATOR = re.compile(r"[-/ ]")
DELISTED = "-"  # ticker_aliases.csv new value for a gone company whose ticker another security now uses


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return [row for row in csv.DictReader(f) if row.get("new")]


def aliases(path: Path = ALIASES) -> dict[str, str]:
    return {row["old"].strip().upper(): row["new"].strip().upper() for row in _rows(path)}


def alias_until(path: Path = ALIASES) -> dict[str, str]:
    """old -> ISO date: the alias covers only trades dated before it (the day another security took the ticker).
    Without one, it covers every trade."""
    return {row["old"].strip().upper(): row["until"].strip() for row in _rows(path) if (row.get("until") or "").strip()}


def resolve(ticker: str | None, listed: dict[str, bool], renamed: dict[str, str], *, tx_date: str | None = None,
            until: dict[str, str] | None = None) -> tuple[str | None, str, bool]:
    """(symbol, ticker_status, is_etf) for a ticker as filed."""
    if not ticker or not ticker.strip():
        return None, "none", False
    filed = ticker.strip().upper()
    cutoff = (until or {}).get(filed)
    if filed in renamed and not (cutoff and tx_date and tx_date >= cutoff):
        # an explicit rename wins, even if the old symbol was later reused
        new = renamed[filed]
        if new == DELISTED:
            return None, "delisted", False
        return new, "renamed", listed.get(new, False)
    for candidate in (filed, CLASS_SEPARATOR.sub(".", filed)):
        if candidate in listed:
            return candidate, "listed", listed[candidate]
    return filed, "unlisted", False


def delay_days(tx_date: str | None, disclosure_date: str | None) -> int | None:
    if not tx_date or not disclosure_date:
        return None
    return (date.fromisoformat(disclosure_date) - date.fromisoformat(tx_date)).days


def assign(conn: sqlite3.Connection, listed: dict[str, bool], renamed: dict[str, str],
           until: dict[str, str] | None = None) -> Counter:
    """Recomputes symbol, ticker_status, is_etf and filing_delay_days for every trade. Returns status counts."""
    counts: Counter = Counter()
    updates = []
    for row in conn.execute("SELECT trade_id, ticker, tx_date, disclosure_date FROM trades").fetchall():
        symbol, status, etf = resolve(row["ticker"], listed, renamed, tx_date=row["tx_date"], until=until)
        counts[status] += 1
        updates.append((symbol, status, int(etf), delay_days(row["tx_date"], row["disclosure_date"]), row["trade_id"]))
    conn.executemany(
        "UPDATE trades SET symbol = ?, ticker_status = ?, is_etf = ?, filing_delay_days = ? WHERE trade_id = ?",
        updates,
    )
    return counts
