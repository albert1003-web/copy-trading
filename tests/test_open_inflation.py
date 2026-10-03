from datetime import date, timedelta

import pytest

from analytics import open_inflation, outcomes
from analytics.stats import Sample, shrink

START = date(2026, 2, 23)  # a Monday; D0 for a filing available after the close on 2026-03-02 is bar 6
D0 = 6


def add_bars(conn, symbol, opens, skip=()):
    """Weekday bars from START with the given opens (close = adj_close = open)."""
    rows, day, i = [], START, 0
    while i < len(opens):
        if day.weekday() < 5:
            if i not in skip:
                rows.append((symbol, day.isoformat(), opens[i], opens[i], opens[i]))
            i += 1
        day += timedelta(days=1)
    conn.executemany("INSERT OR REPLACE INTO prices (ticker, date, open, close, adj_close) VALUES (?, ?, ?, ?, ?)",
                     rows)
    conn.commit()


def dip_at_2(n=30):
    """Flat at 100 except 95 on D0 + 2: waiting two days is 5.3% cheaper."""
    return [95.0 if i == D0 + 2 else 100.0 for i in range(n)]


@pytest.fixture
def attention(tmp_path):
    path = tmp_path / "attention.csv"
    path.write_text("member_id,name,reason\nM1,One,test\nX999,Nobody,not a member\n")
    return path


@pytest.fixture
def db(conn):
    conn.executescript("""
        INSERT INTO members (member_id, name, chamber) VALUES ('M1', 'One', 'house'), ('M2', 'Two', 'house'),
                                                              ('M3', 'Three', 'senate'), ('M4', 'Four', 'house');
    """)
    trades = ([("M1", "DIP", "BUY")] * 25 + [("M2", "RISE", "BUY")] * 25 + [("M3", "DIP", "BUY")] * 3
              + [("M2", "DIP", "SELL")])
    add_trades(conn, [(f"F{i}", *t) for i, t in enumerate(trades, 1)])
    conn.execute("UPDATE trades SET mcap_bucket = 'mega' WHERE symbol = 'RISE'")
    conn.commit()
    add_bars(conn, "SPY", [400.0] * 30)
    add_bars(conn, "DIP", dip_at_2())
    add_bars(conn, "RISE", [100.0 + i for i in range(30)])
    return conn


def add_trades(conn, trades):
    """(doc_id, member_id, symbol, action) rows; every filing available after the close on 2026-03-02."""
    conn.executemany(
        "INSERT OR IGNORE INTO filings (doc_id, chamber, filing_date, first_seen_at, available_at, available_basis) "
        "VALUES (?, 'house', '2026-03-02', '2026-03-05T00:00:00Z', '2026-03-02T21:00:00Z', 'filed')",
        {(t[0],) for t in trades})
    conn.executemany(
        "INSERT INTO trades (doc_id, line_no, member_id, symbol, ticker_status, action, asset_type, tx_date, "
        "disclosure_date) VALUES (?, ?, ?, ?, 'listed', ?, 'stock', '2026-02-25', '2026-03-02')",
        [(doc, i, *rest) for i, (doc, *rest) in enumerate(trades, 1)])
    conn.commit()


def run(conn, attention):
    outcomes.run(conn)
    return open_inflation.run(conn, attention_path=attention, now=lambda: "2026-10-01T22:00:00Z")


def infl(conn, trade_id):
    r = conn.execute("SELECT * FROM trade_outcomes WHERE trade_id = ?", (trade_id,)).fetchone()
    return dict(r)


def delays(conn):
    rows = conn.execute("SELECT * FROM entry_delays")
    return {(r["group_type"], r["group_key"]): (r["n"], r["best_k"]) for r in rows}


def test_open_inflation_uses_raw_opens_per_k(db, attention):
    opens = [100.0] * 30
    opens[D0 + 1], opens[D0 + 2], opens[D0 + 3], opens[D0 + 5] = 99.0, 98.0, 101.0, 97.0
    add_bars(db, "DIP", opens)
    run(db, attention)
    r = infl(db, 1)
    assert r["open_infl_1"] == pytest.approx(100 / 99 - 1)
    assert r["open_infl_2"] == pytest.approx(100 / 98 - 1)
    assert r["open_infl_3"] == pytest.approx(100 / 101 - 1)
    assert r["open_infl_5"] == pytest.approx(100 / 97 - 1)


def test_missing_bar_and_immature_k_are_null_and_other_columns_untouched(db, attention):
    db.execute("DELETE FROM prices WHERE ticker = 'DIP'")
    add_bars(db, "DIP", dip_at_2(), skip={D0 + 3})
    db.execute("DELETE FROM prices WHERE date > (SELECT date FROM prices WHERE ticker = 'SPY' "
               "ORDER BY date LIMIT 1 OFFSET ?)", (D0 + 4,))
    db.commit()
    outcomes.run(db)
    before = infl(db, 1)
    open_inflation.run(db, attention_path=attention)
    after = infl(db, 1)
    assert after["open_infl_2"] == pytest.approx(100 / 95 - 1)
    assert after["open_infl_3"] is None  # missing bar
    assert after["open_infl_5"] is None  # D0 + 5 hasn't happened
    assert {k: v for k, v in after.items() if not k.startswith("open_infl")} == \
           {k: v for k, v in before.items() if not k.startswith("open_infl")}


def test_groups_and_best_entry_delay(db, attention):
    s = run(db, attention)
    d = delays(db)
    assert d[("member", "M1")] == (25, 2)  # waiting 2 days was cheaper
    assert d[("member", "M2")] == (25, 0)  # price only rose: buy at the D0 open
    assert d[("member", "M3")] == (3, None)  # below MIN_N
    assert d[("attention", "high")] == (25, 2) and d[("attention", "other")][0] == 28
    assert d[("mcap", "mega")] == (25, 0) and d[("mcap", "unknown")] == (28, 2)
    assert d[("all", "all")][0] == 53  # the SELL isn't copyable
    assert s.unknown_attention == ["X999"]

    row = db.execute("SELECT * FROM open_inflation_stats WHERE group_type = 'member' AND group_key = 'M1' "
                     "AND k = 2").fetchone()
    assert row["n"] == 25 and row["share_pos"] == 1.0 and row["median"] == pytest.approx(100 / 95 - 1)


def test_trades_in_one_filing_count_once(db, attention):
    add_trades(db, [("BIG", "M4", "DIP", "BUY")] * 30)  # 30 trades, one filing: not a sample of 30
    run(db, attention)
    row = db.execute("SELECT * FROM entry_delays WHERE group_type = 'member' AND group_key = 'M4'").fetchone()
    assert (row["n"], row["n_trades"], row["best_k"]) == (1, 30, None)
    assert delays(db)[("all", "all")][0] == 54


def test_reruns_replace_the_stats(db, attention):
    run(db, attention)
    db.execute("UPDATE trades SET symbol = NULL, ticker_status = 'none' WHERE member_id = 'M3'")
    db.commit()
    run(db, attention)
    assert ("member", "M3") not in delays(db)
    assert db.execute("SELECT COUNT(*) FROM open_inflation_stats WHERE group_key = 'M3'").fetchone()[0] == 0


def test_no_spy_is_an_error(conn, attention):
    assert open_inflation.run(conn, attention_path=attention).errors


def test_shrink_pulls_small_noisy_groups_toward_the_pooled_mean():
    groups = {"big": Sample(400, 0.10, 1.0), "small": Sample(3, 1.00, 1.0), "mid": Sample(100, -0.05, 1.0)}
    shrunk = shrink(groups)
    mu = (400 * 0.10 + 3 * 1.00 + 100 * -0.05) / 503
    assert abs(shrunk["small"] - mu) < abs(1.00 - mu) / 2
    assert shrunk["big"] == pytest.approx(0.10, abs=0.03)
    assert shrink({"only": Sample(5, 0.2, 1.0)}) == {"only": 0.2}


def test_shipped_attention_list_parses():
    ids = open_inflation.load_attention(open_inflation.ATTENTION_CSV)
    assert "P000197" in ids and len(ids) >= 5
