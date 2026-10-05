import pytest

from analytics import factors
from common.signals import amount_level, delay_level, levels


@pytest.mark.parametrize("days, level", [(None, "unknown"), (3, "<=7d"), (14, "8-14d"), (30, "15-30d"), (45, "31-45d"),
                                         (46, ">45d")])
def test_delay_levels(days, level):
    assert delay_level(days) == level


def test_amount_and_kind_levels():
    assert [amount_level(a) for a in (None, 1001, 15001, 50001, 250001)] == \
           ["<$15k", "<$15k", "$15k-50k", "$50k-250k", "$250k+"]
    base = {"mcap_bucket": None, "filing_delay_days": 10, "amount_min": 1001, "committee_relevant": 1,
            "asset_type": "stock", "ticker_status": "listed", "is_etf": 0}
    assert levels(base) == {"mcap": "unknown", "delay": "8-14d", "amount": "<$15k", "committee": "yes", "kind": "stock"}
    assert levels({**base, "is_etf": 1})["kind"] == "etf"
    assert levels({**base, "ticker_status": "unlisted"})["kind"] == "unlisted"
    assert levels({**base, "asset_type": "option"})["kind"] == "call"


def add(conn, doc, abn, *, trades=1, copyable=1, mcap=None, amount=1001, matured=True):
    conn.execute("INSERT OR IGNORE INTO filings (doc_id, chamber, first_seen_at) VALUES (?, 'house', 'x')", (doc,))
    for _ in range(trades):
        tid = conn.execute(
            "INSERT INTO trades (doc_id, line_no, asset_type, ticker_status, is_etf, amount_min, filing_delay_days, "
            "mcap_bucket) VALUES (?, (SELECT COUNT(*) FROM trades) + 1, 'stock', 'listed', 0, ?, 20, ?)",
            (doc, amount, mcap)).lastrowid
        conn.execute("INSERT INTO trade_outcomes (trade_id, copyable, abn_ret_20) VALUES (?, ?, ?)",
                     (tid, copyable, abn if matured else None))
    conn.commit()


def table(conn):
    return {(r["factor"], r["level"]): dict(r) for r in conn.execute("SELECT * FROM signal_factors")}


def test_factor_levels_are_per_filing_shrunk_and_replaced(conn):
    noise = lambda i: 0.05 if i % 2 else -0.05  # noqa: E731
    for i in range(40):
        add(conn, f"S{i}", 0.00 + noise(i), mcap="small" if i < 20 else "mega")
    for i in range(30):
        add(conn, f"B{i}", 0.03 + noise(i), amount=300_000, mcap="mega", trades=5)  # 5 trades, one observation
    add(conn, "SELL", 0.9, copyable=0)
    add(conn, "LATE", 0.9, matured=False)
    s = factors.run(conn, now=lambda: "2026-10-01T22:00:00Z")

    t = table(conn)
    assert s.filings == 70
    assert (t[("all", "all")]["n"], t[("all", "all")]["n_trades"]) == (70, 190)
    big, small = t[("amount", "$250k+")], t[("amount", "<$15k")]
    assert (big["n"], big["n_trades"]) == (30, 150)
    assert big["mean"] == pytest.approx(0.03) and small["mean"] == pytest.approx(0.0)
    pooled = t[("all", "all")]["shrunk"]
    assert small["shrunk"] < pooled < big["shrunk"] <= 0.03  # pulled toward the pooled mean
    assert big["effect"] > 0 > small["effect"]
    # a factor with one level, or no real spread, adds nothing
    assert t[("kind", "stock")]["effect"] == pytest.approx(0)
    assert t[("committee", "no")]["effect"] == pytest.approx(0)
    assert ("kind", "stock") in t and ("committee", "no") in t

    conn.execute("UPDATE trade_outcomes SET copyable = 0")
    conn.commit()
    factors.run(conn)
    assert table(conn) == {}
    assert factors.report(conn)[0].startswith("Signal factors: none yet")
