"""Ticker validation and filing delay for every trade.

trades.ticker stays exactly as parsed. Enrichment writes:
  symbol         what prices/alerts use; NULL when there is nothing to trade
  ticker_status  listed    on the Nasdaq/NYSE symbol lists (share classes normalized: BRK-B -> BRK.B)
                 renamed   resolved through enrich/ticker_aliases.csv (old,new,date,note)
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


def aliases(path: Path = ALIASES) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {row["old"].strip().upper(): row["new"].strip().upper() for row in csv.DictReader(f) if row.get("new")}


def resolve(ticker: str | None, listed: dict[str, bool], renamed: dict[str, str]) -> tuple[str | None, str, bool]:
    """(symbol, ticker_status, is_etf) for a ticker as filed."""
    if not ticker or not ticker.strip():
        return None, "none", False
    filed = ticker.strip().upper()
    if filed in renamed:  # an explicit rename wins, even if the old symbol was later reused
        new = renamed[filed]
        return new, "renamed", listed.get(new, False)
    for candidate in (filed, CLASS_SEPARATOR.sub(".", filed)):
        if candidate in listed:
            return candidate, "listed", listed[candidate]
    return filed, "unlisted", False


def delay_days(tx_date: str | None, disclosure_date: str | None) -> int | None:
    if not tx_date or not disclosure_date:
        return None
    return (date.fromisoformat(disclosure_date) - date.fromisoformat(tx_date)).days


def assign(conn: sqlite3.Connection, listed: dict[str, bool], renamed: dict[str, str]) -> Counter:
    """Recomputes symbol, ticker_status, is_etf and filing_delay_days for every trade. Returns status counts."""
    counts: Counter = Counter()
    updates = []
    for row in conn.execute("SELECT trade_id, ticker, tx_date, disclosure_date FROM trades").fetchall():
        symbol, status, etf = resolve(row["ticker"], listed, renamed)
        counts[status] += 1
        updates.append((symbol, status, int(etf), delay_days(row["tx_date"], row["disclosure_date"]), row["trade_id"]))
    conn.executemany(
        "UPDATE trades SET symbol = ?, ticker_status = ?, is_etf = ?, filing_delay_days = ? WHERE trade_id = ?",
        updates,
    )
    return counts
