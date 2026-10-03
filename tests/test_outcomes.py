from datetime import date, timedelta

import pytest

from analytics import outcomes
from analytics.outcomes import Calendar

START = date(2026, 2, 23)  # a Monday
D0 = "2026-03-03"  # filing available after the close on Mon 2026-03-02
D0_INDEX = 6


def weekdays(n: int, start: date = START) -> list[str]:
    days, day = [], start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def add_bars(conn, symbol, n, close=lambda i: 100.0 + i, factor=lambda i: 1.0, start=START, skip=()):
    """n weekday bars; open = close - 0.5, adj_close = close * factor(i)."""
    conn.executemany(
        "INSERT OR REPLACE INTO prices (ticker, date, open, close, adj_close) VALUES (?, ?, ?, ?, ?)",
        [(symbol, d, close(i) - 0.5, close(i), close(i) * factor(i))
         for i, d in enumerate(weekdays(n, start)) if i not in skip],
    )
    conn.commit()


TRADES = [
    # trade_id, symbol, status, action, asset_type, asset_name, description, tx_date
    (1, "AAPL", "listed", "BUY", "stock", "Apple Inc. (AAPL)", None, "2026-03-01"),  # Sunday
    (2, "AAPL", "listed", "SELL", "stock", "Apple Inc. (AAPL)", None, "2026-02-25"),
    (3, "AAPL", "listed", "BUY", "option", "AAPL Option Type: Put", None, "2026-02-25"),
    (4, "AAPL", "listed", "BUY", "option", "AAPL", "Purchased 10 call options", "2026-02-25"),
    (5, "SPY", "listed", "BUY", "other", "SPDR S&P 500 (SPY)", None, "2026-02-25"),
    (6, "GONE", "unlisted", "BUY", "stock", "Gone Corp (GONE)", None, "2026-02-25"),
    (7, "AAPL", "listed", "BUY", "stock", "Apple Inc. (AAPL)", None, "2026-03-20"),  # typo: after the filing
    (8, None, "none", "BUY", "other", "Some Fund", None, "2026-02-25"),
]


@pytest.fixture
def db(conn):
    conn.executescript("""
        INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at, available_at, available_basis) VALUES
          ('F1', 'house', '2026-03-02', '2026-03-05T00:00:00Z', '2026-03-02T21:00:00Z', 'filed'),
          ('LATE', 'house', '2026-12-01', '2026-12-01T15:00:00Z', '2026-12-01T15:00:00Z', 'seen');
    """)
    conn.executemany(
        "INSERT INTO trades (trade_id, doc_id, line_no, symbol, ticker_status, action, asset_type, asset_name, "
        "description, tx_date, disclosure_date) VALUES (?, 'F1', ?, ?, ?, ?, ?, ?, ?, ?, '2026-03-02')",
        [(t[0], t[0], *t[1:]) for t in TRADES],
    )
    conn.execute("INSERT INTO trades (trade_id, doc_id, line_no, symbol, ticker_status, action, tx_date) "
                 "VALUES (9, 'LATE', 1, 'AAPL', 'listed', 'BUY', '2026-11-20')")
    conn.commit()
    return conn


def row(conn, trade_id):
    r = conn.execute("SELECT * FROM trade_outcomes WHERE trade_id = ?", (trade_id,)).fetchone()
    return dict(r) if r else None


# --- D0 ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("available_at, d0", [
    ("2026-03-02T21:00:00Z", "2026-03-03"),  # filed: after the close -> next trading day
    ("2026-03-03T13:00:00Z", "2026-03-03"),  # 08:00 EST, before the open -> same day
    ("2026-03-03T14:31:00Z", "2026-03-06"),  # 09:31 EST, after the open -> next trading day (04/05 are holidays)
    ("2026-03-06T22:00:00Z", "2026-03-09"),  # Friday evening -> Monday
    ("2026-03-07T15:00:00Z", "2026-03-09"),  # Saturday -> Monday
    ("2026-03-09T13:00:00Z", "2026-03-09"),  # 09:00 EDT (DST began 03-08) -> same day
    ("2026-03-09T14:00:00Z", None),  # 10:00 EDT on the last bar -> pending
    ("2026-02-01T15:00:00Z", None),  # before the calendar
])
def test_d0_is_the_first_open_after_available(available_at, d0):
    cal = Calendar(["2026-03-02", "2026-03-03", "2026-03-06", "2026-03-09"])
    i = cal.d0(available_at)
    assert (cal.days[i] if i is not None else None) == d0


# --- returns ----------------------------------------------------------------------------------------


def test_returns_use_the_adjusted_open_and_subtract_spy(db):
    add_bars(db, "SPY", 80, close=lambda i: 400.0 + 2 * i)
    # A dividend goes ex on bar 9: earlier adj_closes are scaled down by 0.98
    add_bars(db, "AAPL", 80, close=lambda i: 100.0 + i, factor=lambda i: 0.98 if i < 9 else 1.0)
    s = outcomes.run(db, now=lambda: "2026-10-01T22:00:00Z")

    r = row(db, 1)
    assert r["d0_date"] == D0 and r["d0_open"] == 100.0 + D0_INDEX - 0.5  # raw open
    entry = (100.0 + D0_INDEX - 0.5) * 0.98
    spy_entry = 400.0 + 2 * D0_INDEX - 0.5
    for h in outcomes.HORIZONS:
        j = D0_INDEX + h
        ret = (100.0 + j) * (0.98 if j < 9 else 1.0) / entry - 1
        assert r[f"ret_{h}"] == pytest.approx(ret)
        assert r[f"abn_ret_{h}"] == pytest.approx(ret - ((400.0 + 2 * j) / spy_entry - 1))
    assert r["complete"] == 1 and r["computed_at"] == "2026-10-01T22:00:00Z"

    # pre-disclosure move: Friday 02-27 (bar 4) close -> D0 open; context only
    tx_ret = entry / ((100.0 + 4) * 0.98) - 1
    assert r["tx_ret"] == pytest.approx(tx_ret)
    assert r["tx_abn_ret"] == pytest.approx(tx_ret - (spy_entry / (400.0 + 8) - 1))
    assert row(db, 7)["tx_ret"] is None  # tx_date after D0 (filer typo)

    assert s.in_scope == 8 and s.with_outcome == 7 and s.pending == 1  # trade 8 has no symbol; 9 is pending
    assert row(db, 8) is None and row(db, 9) is None


def test_horizons_fill_as_they_mature_and_reruns_are_idempotent(db):
    add_bars(db, "SPY", D0_INDEX + 8)
    add_bars(db, "AAPL", D0_INDEX + 8)
    outcomes.run(db)
    r = row(db, 1)
    assert r["ret_1"] is not None and r["ret_5"] is not None and r["ret_10"] is None
    assert r["complete"] == 0

    add_bars(db, "SPY", D0_INDEX + 61)
    add_bars(db, "AAPL", D0_INDEX + 61)
    outcomes.run(db, now=lambda: "x")
    first = row(db, 1)
    assert first["ret_60"] is not None and first["complete"] == 1
    outcomes.run(db, now=lambda: "x")
    assert row(db, 1) == first
    assert db.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0] == 7


def test_win_labels_only_for_buys_we_could_copy(db):
    add_bars(db, "SPY", 80, close=lambda i: 100.0)  # flat: abnormal = raw return
    add_bars(db, "AAPL", 80, close=lambda i: 100.0 + i)  # rising
    outcomes.run(db)
    assert row(db, 1)["win_20"] == 1  # BUY stock
    assert row(db, 2)["win_20"] is None and row(db, 2)["abn_ret_20"] > 0  # SELL: returns, no label
    assert row(db, 3)["win_20"] is None  # bought puts
    assert row(db, 4)["win_20"] == 1  # bought calls
    spy = row(db, 5)
    assert spy["abn_ret_20"] == 0 and spy["win_20"] == 0  # abn == 0 is not a win


def test_missing_prices_leave_nulls_but_still_complete(db):
    add_bars(db, "SPY", 80)
    add_bars(db, "AAPL", 80, skip={D0_INDEX + 5})  # one missing bar: that horizon stays NULL
    add_bars(db, "GONE", 5)  # delisted before D0
    s = outcomes.run(db)
    gone = row(db, 6)
    assert gone["d0_open"] is None and gone["ret_1"] is None and gone["complete"] == 1
    assert s.no_d0_price == 1
    aapl = row(db, 1)
    assert aapl["ret_5"] is None and aapl["ret_10"] is not None
    assert s.filled["5"] == 1 and s.filled["10"] == 6  # 5 AAPL trades (all hit the gap at h=5) + the SPY trade


def test_no_spy_is_an_error(db):
    add_bars(db, "AAPL", 80)
    s = outcomes.run(db)
    assert s.errors and db.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0] == 0


def test_upsert_keeps_open_inflation_and_drops_out_of_scope_rows(db):
    add_bars(db, "SPY", 80)
    add_bars(db, "AAPL", 80)
    outcomes.run(db)
    db.execute("UPDATE trade_outcomes SET open_infl_1 = 0.01 WHERE trade_id = 1")
    db.execute("UPDATE trades SET symbol = NULL, ticker_status = 'none' WHERE trade_id = 2")
    db.commit()
    s = outcomes.run(db)
    assert row(db, 1)["open_infl_1"] == 0.01
    assert row(db, 2) is None and s.removed == 1
