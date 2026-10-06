from datetime import UTC, date, datetime, timedelta

import pytest

from agents import journal, runner

NOW = datetime(2026, 10, 2, 23, 0, tzinfo=UTC)


def weekdays(start: date, end: date) -> list[str]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


@pytest.fixture
def db(conn):
    conn.execute("INSERT INTO members (member_id, name, chamber) VALUES ('A1', 'Ann Able', 'house')")
    for i, d in enumerate(weekdays(date(2026, 8, 3), date(2026, 10, 2))):
        conn.execute("INSERT INTO prices (ticker, date, open, close) VALUES ('SPY', ?, ?, ?)", (d, 500 + i, 500 + i))
        conn.execute("INSERT INTO prices (ticker, date, open, close) VALUES ('NVDA', ?, ?, ?)", (d, 100 + i, 100 + i))
        conn.execute("INSERT INTO prices (ticker, date, open, close) VALUES ('AMD', ?, ?, ?)", (d, 200 - i, 200 - i))
    conn.execute("INSERT INTO filings (doc_id, member_id, chamber, first_seen_at) VALUES "
                 "('F1', 'A1', 'house', '2026-09-08T14:00:00Z'), ('F2', 'A1', 'house', '2026-09-20T14:00:00Z')")
    conn.execute("INSERT INTO trades (trade_id, doc_id, member_id, symbol, action, line_no) VALUES "
                 "(1, 'F1', 'A1', 'NVDA', 'BUY', 1), (2, 'F2', 'A1', 'AMD', 'BUY', 1)")
    conn.execute("INSERT INTO trade_outcomes (trade_id, d0_date, d0_open, ret_20, abn_ret_20, copyable) VALUES "
                 "(1, '2026-09-08', 125, 0.10, 0.06, 1), (2, '2026-09-21', 190, NULL, NULL, 1)")
    conn.execute("INSERT INTO alerts (trade_id, sent_at, score, suggested_exit, rule) VALUES "
                 "(1, '2026-09-08T14:05:00Z', 72, 'If you buy: hold 5 days', 'watchlist_buy'), "
                 "(2, '2026-09-20T14:05:00Z', 61, NULL, 'watchlist_buy')")
    # NVDA from the alert, bought 2 trading days after D0; AMD bought in August, sold in September.
    conn.execute("INSERT INTO my_positions (trade_id, ticker, buy_date, buy_price, shares, exit_rule) "
                 "VALUES (1, 'nvda', '2026-09-10', 130.0, 10, 'fixed_hold(days=5)')")
    conn.execute("INSERT INTO my_positions (ticker, buy_date, buy_price, shares, sell_date, sell_price, status) "
                 "VALUES ('AMD', '2026-08-14', 190.0, 4, '2026-09-15', 170.0, 'closed')")
    conn.commit()
    return conn


def close(conn, ticker, day):
    return conn.execute("SELECT close FROM prices WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                        (ticker, day)).fetchone()[0]


def test_month_helpers():
    assert journal.month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert journal.previous_month(date(2026, 1, 5)) == "2025-12"


def test_due_once_a_month(db):
    assert journal.due(db) is None
    db.execute("INSERT INTO source_state (source, checked_at) VALUES ('pipeline.nightly', '2026-10-01')")
    assert journal.due(db) == "2026-09"
    journal.run(db, month="2026-09", template_only=True, now=lambda: NOW)
    assert journal.due(db) is None


def test_facts(db):
    f = journal.facts(db, "2026-09")
    amd, nvda = f["positions"]

    aug31, sep30 = close(db, "AMD", "2026-08-31"), close(db, "NVDA", "2026-09-30")
    assert amd["status"] == "sold" and amd["month_return"] == pytest.approx(170 / aug31 - 1)
    assert amd["month_pnl"] == pytest.approx(4 * (170 - aug31))
    assert amd["since_buy"] == pytest.approx(170 / 190 - 1) and amd["alert"] is None
    assert amd["month_spy"] == pytest.approx(close(db, "SPY", "2026-09-15") / close(db, "SPY", "2026-08-31") - 1)

    assert nvda["status"] == "open" and nvda["month_return"] == pytest.approx(sep30 / 130 - 1)
    alert = nvda["alert"]
    assert alert["trading_days_after_d0"] == 2 and alert["entry_vs_d0_open"] == pytest.approx(130 / 125 - 1)
    assert alert["system_abn_20"] == 0.06 and alert["suggested_exit"] == "If you buy: hold 5 days"

    book = {b["symbols"]: b for b in f["system_book"]}
    assert book["NVDA"]["followed"] and book["NVDA"]["abn_20"] == 0.06
    assert not book["AMD"]["followed"] and book["AMD"]["abn_20"] is None
    assert book["AMD"]["to_date_approx"] == pytest.approx(close(db, "AMD", "2026-09-30") / (200 - 35) - 1)
    t = f["totals"]
    assert (t["positions"], t["sold"], t["alerts"], t["followed_alerts"], t["system_matured"]) == (2, 1, 2, 1, 1)
    assert f["spy"]["since"] == "2026-08-14"

    text = journal.render(f)
    assert "bought 2 trading day(s) after D0" in text and "[followed]" in text and "not matured" in text


def test_no_positions_logged(db):
    db.execute("DELETE FROM my_positions")
    text = journal.render(journal.facts(db, "2026-09"))
    assert "none logged for this month" in text and "AMD" in text  # the system's alerts are still reviewed


def test_narrative_on_top(db):
    calls = []

    def agent(conn, **kwargs):
        calls.append(kwargs)
        run_id = runner.start_run(conn, kwargs["agent"], kwargs["extra_inputs"], runner.MODEL)
        runner.finish_run(conn, run_id, "You bought NVDA two days late.", [])
        return runner.RunResult(run_id, "ok", "You bought NVDA two days late.")

    s = journal.run(db, month="2026-09", now=lambda: NOW, agent_runner=agent)
    assert s.text.startswith("You bought NVDA two days late.") and "YOUR TRADES" in s.text
    assert calls[0]["extra_inputs"] == {"month": "2026-09"}
