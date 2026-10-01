"""Which trades and filings qualify for an alert.

Only filings first seen at or after `since`, from members on the active watchlist:
  watchlist_buy  a buy with a symbol: a stock (or other listed asset), or bought calls on a stock
  held_sale      a sale (full or partial) of a symbol we hold in an open my_positions row
  scanned        a scanned filing (no trade rows yet), once per filing
Trades that already have an `alerts` row, and filings with a `filing_alerts` row, are skipped.
"""

import sqlite3

from alerts.score import is_call

WATCHLIST_BUY = "watchlist_buy"
HELD_SALE = "held_sale"
SALES = ("SELL", "SELL_PARTIAL")


def held_symbols(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT DISTINCT UPPER(ticker) FROM my_positions WHERE status = 'open'").fetchall()
    return {r[0] for r in rows}


def trades(conn: sqlite3.Connection, since: str) -> list[tuple[str, sqlite3.Row]]:
    """(rule, trade row) for every trade that should be alerted, oldest filing first."""
    held = held_symbols(conn)
    rows = conn.execute(
        """
        SELECT t.*, f.first_seen_at, f.source_url, f.filing_date, m.name AS member_name, m.party, m.state, m.chamber
        FROM trades t
        JOIN filings f ON f.doc_id = t.doc_id
        JOIN members m ON m.member_id = t.member_id
        JOIN watchlist w ON w.member_id = t.member_id AND w.active = 1
        WHERE f.first_seen_at >= ?
          AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.trade_id = t.trade_id)
        ORDER BY f.first_seen_at, t.doc_id, t.line_no
        """,
        (since,),
    ).fetchall()
    found = []
    for row in rows:
        if not row["symbol"]:
            continue
        if row["action"] == "BUY" and (row["asset_type"] != "option" or is_call(row)):
            found.append((WATCHLIST_BUY, row))
        elif row["action"] in SALES and row["symbol"].upper() in held:
            found.append((HELD_SALE, row))
    return found


def scanned_filings(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.*, m.name AS member_name, m.party, m.state, m.chamber
        FROM filings f
        JOIN members m ON m.member_id = f.member_id
        JOIN watchlist w ON w.member_id = f.member_id AND w.active = 1
        WHERE f.doc_format = 'scanned' AND f.first_seen_at >= ?
          AND NOT EXISTS (SELECT 1 FROM filing_alerts fa WHERE fa.doc_id = f.doc_id)
        ORDER BY f.first_seen_at, f.doc_id
        """,
        (since,),
    ).fetchall()
