from datetime import date, timedelta

import pandas as pd
import pytest

from prices import fetch
from prices.fetch import Bar

TODAY = date(2026, 10, 1)
NOW = "2026-10-01T22:00:00Z"


def series(start: date, end: date, price: float = 100.0, adj: float = 1.0) -> list[Bar]:
    """Weekday bars from start to end."""
    bars, day = [], start
    while day <= end:
        if day.weekday() < 5:
            bars.append(Bar(day.isoformat(), price, price + 1, price - 1, price, price * adj, 1000))
        day += timedelta(days=1)
    return bars


class FakeSource:
    def __init__(self, data: dict[str, list[Bar]]):
        self.data = data
        self.calls: list[tuple[list[str], date, date]] = []
        self.fail = False

    def history(self, symbols, start, end):
        self.calls.append((list(symbols), start, end))
        if self.fail:
            raise RuntimeError("down")
        return {s: [b for b in self.data.get(s, []) if start.isoformat() <= b.date <= end.isoformat()]
                for s in symbols}

    def fetched(self, symbol):
        return [(start, end) for symbols, start, end in self.calls if symbol in symbols]


@pytest.fixture
def db(conn):
    conn.executescript("""
        INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at, available_at) VALUES
          ('OLD', 'house', '2026-03-02', '2026-03-03T00:00:00Z', '2026-03-02T21:00:00Z'),
          ('NEW', 'house', '2026-09-28', '2026-09-29T00:00:00Z', '2026-09-29T00:00:00Z');
        INSERT INTO trades (doc_id, line_no, symbol, ticker_status, tx_date, disclosure_date) VALUES
          ('OLD', 1, 'AAPL', 'listed', '2026-02-10', '2026-03-02'),
          ('OLD', 2, 'GONE', 'unlisted', '2026-02-10', '2026-03-02'),
          ('OLD', 3, 'IPO', 'listed', '2026-02-10', '2026-03-02'),
          ('OLD', 4, NULL, 'none', '2026-02-10', '2026-03-02'),
          ('NEW', 1, 'NVDA', 'listed', '2026-09-01', '2026-09-28'),
          ('NEW', 2, 'AAPL', 'listed', '1926-09-01', '2026-09-28');
        INSERT INTO my_positions (ticker, buy_date, buy_price, shares) VALUES ('msft', '2026-09-15', 1, 1);
    """)
    conn.commit()
    return conn


@pytest.fixture
def source():
    return FakeSource({
        "AAPL": series(date(2019, 1, 1), TODAY),
        "NVDA": series(date(2026, 1, 1), TODAY),
        "MSFT": series(date(2026, 1, 1), TODAY),
        "SPY": series(date(2019, 1, 1), TODAY),
        "GONE": series(date(2026, 1, 1), date(2026, 6, 30)),  # delisted
        "IPO": series(date(2026, 4, 15), TODAY),  # first bar after its first disclosure
    })


def coverage(conn):
    return {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM price_coverage")}


def test_universe_and_needed_ranges(db):
    needs = fetch.universe(db, TODAY)
    assert set(needs) == {"AAPL", "GONE", "IPO", "NVDA", "MSFT", "SPY"}
    assert needs["AAPL"].fetch_from == fetch.HISTORY_FLOOR  # a 1926 typo doesn't pull a century
    assert needs["NVDA"].fetch_from == date(2026, 8, 22) and needs["NVDA"].first_disclosure == date(2026, 9, 28)
    assert needs["MSFT"].fetch_from == date(2026, 9, 5) and needs["MSFT"].active
    assert needs["SPY"].fetch_from == fetch.HISTORY_FLOOR
    assert needs["NVDA"].active and needs["AAPL"].active
    assert not needs["GONE"].active  # its only filing is older than 150 days


def test_first_run_fetches_everything_and_records_coverage(db, source):
    s = fetch.run(db, source, today=TODAY, now=lambda: NOW)
    assert s.errors == [] and s.symbols == 6
    cov = coverage(db)
    assert {k: v["status"] for k, v in cov.items()} == {
        "AAPL": "ok", "NVDA": "ok", "MSFT": "ok", "SPY": "ok", "GONE": "partial", "IPO": "partial"}
    assert "delisted" in cov["GONE"]["note"] and "starts 2026-04-15" in cov["IPO"]["note"]
    assert db.execute("SELECT MIN(date) FROM prices WHERE ticker = 'NVDA'").fetchone()[0] == "2026-08-24"
    assert cov["NVDA"]["n_rows"] == db.execute("SELECT COUNT(*) FROM prices WHERE ticker = 'NVDA'").fetchone()[0]


def test_nightly_run_is_incremental_and_skips_inactive_symbols(db, source):
    fetch.run(db, source, today=TODAY, now=lambda: NOW)
    rows = db.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    source.calls.clear()

    s = fetch.run(db, source, today=TODAY, now=lambda: NOW)
    assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == rows  # idempotent
    assert source.fetched("GONE") == [] and source.fetched("IPO") == []  # inactive, already known
    assert source.fetched("AAPL") == [(TODAY - timedelta(days=5), TODAY)]
    assert s.rebased == 0

    source.calls.clear()
    fetch.run(db, source, all_symbols=True, today=TODAY, now=lambda: NOW)
    assert source.fetched("GONE") == [(date(2026, 6, 25), TODAY)]


def test_new_older_trade_triggers_a_full_fetch(db, source):
    fetch.run(db, source, today=TODAY, now=lambda: NOW)
    db.execute("INSERT INTO trades (doc_id, line_no, symbol, ticker_status, tx_date, disclosure_date) "
               "VALUES ('NEW', 3, 'NVDA', 'listed', '2026-05-01', '2026-09-28')")
    source.calls.clear()
    fetch.run(db, source, today=TODAY, now=lambda: NOW)
    assert source.fetched("NVDA") == [(date(2026, 4, 21), TODAY)]
    assert db.execute("SELECT MIN(date) FROM prices WHERE ticker = 'NVDA'").fetchone()[0] == "2026-04-21"


def test_rebased_history_is_replaced_whole(db, source):
    fetch.run(db, source, today=TODAY, now=lambda: NOW)
    # a dividend re-bases every adj_close before it
    source.data["AAPL"] = series(date(2019, 1, 1), TODAY, adj=0.98)
    s = fetch.run(db, source, today=TODAY, now=lambda: NOW)
    assert s.rebased == 1
    assert [tuple(r) for r in db.execute("SELECT DISTINCT adj_close FROM prices WHERE ticker = 'AAPL'")] == [(98.0,)]


def test_a_partial_day_bar_is_not_a_rebase(db, source):
    yesterday = TODAY - timedelta(days=1)
    source.data["AAPL"] = series(date(2019, 1, 1), yesterday)
    fetch.run(db, source, today=yesterday, now=lambda: NOW)
    db.execute("UPDATE prices SET close = 90, adj_close = 90 WHERE ticker = 'AAPL' AND date = ?",
               (yesterday.isoformat(),))  # stored mid-session
    source.data["AAPL"] = series(date(2019, 1, 1), TODAY)
    s = fetch.run(db, source, today=TODAY, now=lambda: NOW)
    assert s.rebased == 0
    assert db.execute("SELECT close FROM prices WHERE ticker = 'AAPL' AND date = ?",
                      (yesterday.isoformat(),)).fetchone()[0] == 100


def test_missing_symbol_and_source_outage(db, source):
    del source.data["NVDA"]
    s = fetch.run(db, source, today=TODAY, now=lambda: NOW)
    assert coverage(db)["NVDA"]["status"] == "missing" and s.empty == 1 and s.errors == []

    source.fail = True
    s = fetch.run(db, source, today=TODAY, now=lambda: NOW)
    assert s.errors and s.failed_batches == 1 and s.fetched == 0


def test_gap_report(db, source):
    assert "not fetched yet" in fetch.gaps(db)[0]
    fetch.run(db, source, today=TODAY, now=lambda: NOW)
    report = "\n".join(fetch.gaps(db))
    assert "ok 4, partial 2, missing 0" in report
    assert "3 of 5 trades with a symbol fully covered" in report  # AAPL x2, NVDA; GONE and IPO are partial
    assert "GONE" in report and "survivorship" in report


def test_bars_from_yahoo_frame_drops_empty_rows_and_maps_symbols():
    frame = pd.DataFrame(
        {"Open": [1.0, float("nan")], "High": [2.0, float("nan")], "Low": [0.5, float("nan")],
         "Close": [1.5, float("nan")], "Adj Close": [1.4, float("nan")], "Volume": [100.0, float("nan")]},
        index=pd.to_datetime(["2026-09-30", "2026-10-01"]),
    )
    assert fetch.bars_from_frame(frame) == [Bar("2026-09-30", 1.0, 2.0, 0.5, 1.5, 1.4, 100)]
    assert fetch.to_yahoo("BRK.B") == "BRK-B"


def test_older_trade_in_an_old_filing_still_triggers_a_full_fetch(db, source):
    """A backfill adds an older trade for a known but inactive symbol: the next nightly must fetch the earlier
    history, and coverage must not record the new start as tried until it has been fetched."""
    fetch.run(db, source, today=TODAY, now=lambda: NOW)
    source.data["GONE"] = series(date(2025, 1, 1), date(2026, 6, 30))
    db.execute("INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at, available_at) "
               "VALUES ('OLDER', 'house', '2025-12-01', '2025-12-02T00:00:00Z', '2025-12-01T21:00:00Z')")
    db.execute("INSERT INTO trades (doc_id, line_no, symbol, ticker_status, tx_date, disclosure_date) "
               "VALUES ('OLDER', 1, 'GONE', 'listed', '2025-11-20', '2025-12-01')")
    source.calls.clear()
    fetch.run(db, source, today=TODAY, now=lambda: NOW)
    assert source.fetched("GONE") == [(date(2025, 11, 10), TODAY)]
    assert db.execute("SELECT MIN(date) FROM prices WHERE ticker = 'GONE'").fetchone()[0] == "2025-11-10"
    assert coverage(db)["GONE"]["needed_from"] == "2025-11-10"


def test_coverage_keeps_the_tried_start_for_symbols_not_fetched(db, source):
    fetch.run(db, source, today=TODAY, now=lambda: NOW)
    db.execute("UPDATE price_coverage SET needed_from = '2026-03-01' WHERE symbol = 'IPO'")
    fetch.update_coverage(db, fetch.universe(db, TODAY), checked=set(), now=NOW)
    assert coverage(db)["IPO"]["needed_from"] == "2026-03-01"


def test_refetch_partial_repairs_truncated_starts(db, source):
    fetch.run(db, source, today=TODAY, now=lambda: NOW)
    db.execute("DELETE FROM prices WHERE ticker = 'AAPL' AND date < '2026-04-01'")  # cut short by the old bug
    db.commit()
    fetch.update_coverage(db, fetch.universe(db, TODAY), checked=set(), now=NOW)
    assert coverage(db)["AAPL"]["status"] == "partial"
    source.calls.clear()
    fetch.run(db, source, refetch_partial=True, today=TODAY, now=lambda: NOW)
    assert source.fetched("AAPL") == [(fetch.HISTORY_FLOOR, TODAY)]
    assert coverage(db)["AAPL"]["status"] == "ok"
    assert len(source.fetched("IPO")) == 1  # partial too (a late listing): refetched once, harmlessly
