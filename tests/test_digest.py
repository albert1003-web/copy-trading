from datetime import UTC, date, datetime, timedelta

import pytest

from agents import digest, runner

SINCE = "2026-10-01T22:30:00Z"
UNTIL = "2026-10-02T22:30:00Z"
NOW = datetime(2026, 10, 2, 22, 30, tzinfo=UTC)


def weekdays(start: date, end: date) -> list[str]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


DAYS = weekdays(date(2026, 8, 3), date(2026, 10, 2))  # SPY's calendar; the last trading day is 2026-10-02


@pytest.fixture
def db(conn):
    conn.executemany("INSERT INTO members (member_id, name, chamber) VALUES (?, ?, 'house')",
                     [("W1", "Wanda Watched"), ("O1", "Otto Other"), ("N1", "Nina New")])
    conn.execute("INSERT INTO watchlist (member_id, added_at) VALUES ('W1', '2026-09-01T00:00:00Z')")
    for i, d in enumerate(DAYS):
        conn.execute("INSERT INTO prices (ticker, date, open, close, adj_close) VALUES ('SPY', ?, ?, ?, ?)",
                     (d, 500 + i, 500 + i, 500 + i))
        conn.execute("INSERT INTO prices (ticker, date, open, close, adj_close) VALUES ('NVDA', ?, ?, ?, ?)",
                     (d, 100 + i, 100 + i, 100 + i))
    filings = [  # doc, member, first seen, basis
        ("D1", "W1", "2026-10-02T15:00:00Z", "seen"),   # in the window, watched
        ("D2", "O1", "2026-10-02T16:00:00Z", "seen"),   # in the window, not watched
        ("D3", "W1", "2026-09-30T15:00:00Z", "seen"),   # before the window
        ("D4", "W1", "2026-10-02T17:00:00Z", "filed"),  # backfilled: never "new"
        ("D5", "W1", "2026-08-01T15:00:00Z", "seen"),   # old filing behind the outcomes below
    ]
    for doc, member, seen, basis in filings:
        conn.execute(
            "INSERT INTO filings (doc_id, member_id, chamber, filing_date, first_seen_at, available_basis, "
            "doc_format) VALUES (?, ?, 'house', ?, ?, ?, 'electronic')", (doc, member, seen[:10], seen, basis))
    trades = [  # id, doc, member, symbol, action
        (1, "D1", "W1", "NVDA", "BUY"), (2, "D2", "O1", "AAPL", "BUY"), (3, "D3", "W1", "MSFT", "BUY"),
        (4, "D5", "W1", "NVDA", "BUY"), (5, "D5", "W1", "NVDA", "BUY"), (6, "D5", "W1", "TSLA", "SELL"),
    ]
    for tid, doc, member, symbol, action in trades:
        conn.execute(
            "INSERT INTO trades (trade_id, doc_id, member_id, ticker, symbol, action, asset_type, tx_date, "
            "amount_min, amount_max, line_no, filing_delay_days) "
            "VALUES (?, ?, ?, ?, ?, ?, 'stock', '2026-09-20', 1001, 15000, ?, 12)",
            (tid, doc, member, symbol, symbol, action, tid))
    conn.execute("INSERT INTO alerts (trade_id, sent_at, score, rule) VALUES (1, '2026-10-02T15:05:00Z', 72, "
                 "'watchlist_buy')")
    conn.execute("INSERT INTO alerts (trade_id, sent_at, score, rule) VALUES (3, '2026-09-30T15:05:00Z', 60, "
                 "'watchlist_buy')")
    # D5's buys reached D0 + 20 on the last trading day (inside the window); its sale isn't copyable.
    d0 = DAYS[-1 - 20]
    for tid, abn, copyable in ((4, 0.05, 1), (5, 0.07, 1), (6, 0.5, 0)):
        conn.execute("INSERT INTO trade_outcomes (trade_id, d0_date, ret_20, abn_ret_20, copyable) "
                     "VALUES (?, ?, ?, ?, ?)", (tid, d0, abn + 0.01, abn, copyable))
    positions = [  # id, ticker, buy date, price, rule
        (1, "NVDA", DAYS[-11], 100.0, "trailing_stop(pct=0.1)"),
        (2, "NVDA", DAYS[-5], 100.0, "fixed_hold(days=3)"),
        (3, "nvda", DAYS[-3], 120.0, "sell when it feels right"),
    ]
    for pid, ticker, buy, price, rule in positions:
        conn.execute("INSERT INTO my_positions (position_id, ticker, buy_date, buy_price, shares, exit_rule) "
                     "VALUES (?, ?, ?, ?, 10, ?)", (pid, ticker, buy, price, rule))
    conn.execute("INSERT INTO exit_alerts (position_id, rule, triggered_on, reason, price, sent_at) "
                 "VALUES (2, 'fixed_hold(days=3)', ?, 'hold', 150.0, '2026-10-02T21:00:00Z')", (DAYS[-2],))
    for as_of, rows in (("2026-10-01", [("O1", 1, 0.02), ("W1", 12, 0.003)]),
                        ("2026-10-02", [("N1", 1, 0.03), ("O1", 11, 0.004), ("W1", 9, 0.006)])):
        for member, rank, score in rows:
            conn.execute("INSERT INTO member_scores (member_id, as_of, rank, shrunk_score, horizon) "
                         "VALUES (?, ?, ?, ?, 20)", (member, as_of, rank, score))
    conn.execute("INSERT INTO agent_runs (run_id, agent, started_at, status) VALUES (5, 'watchlist_review', "
                 "'2026-10-02T00:00:00Z', 'ok')")
    conn.execute("INSERT INTO agent_proposals (run_id, position, kind, member_id, title, rationale) "
                 "VALUES (5, 0, 'watchlist_add', 'N1', 'Add Nina New to the watchlist', 'r')")
    conn.commit()
    return conn


def test_facts_cover_the_window(db):
    f = digest.facts(db, SINCE, UNTIL)

    assert f["filings"]["by_chamber"] == {"house": 2}  # D1, D2; not D3 (earlier) or D4 (backfilled)
    [watched] = f["filings"]["watched"]
    assert watched["doc_id"] == "D1" and watched["trades"][0]["alert_score"] == 72
    assert f["alerts"] == {"trades_by_rule": {"watchlist_buy": 1}, "scanned_filings": 0}

    trailing, held, loose = f["positions"]
    assert trailing["last_close"] == 100 + len(DAYS) - 1 and trailing["days_held"] == 10
    assert trailing["return"] == pytest.approx((100 + len(DAYS) - 1) / 100 - 1)
    assert trailing["spy_return"] == pytest.approx((500 + len(DAYS) - 1) / (500 + len(DAYS) - 11) - 1)
    assert trailing["max_hold"] == 60 and trailing["status"].startswith("watching: Trailing stop")
    assert held["status"] == f"exit triggered {DAYS[-2]} (hold)"
    assert loose["ticker"] == "NVDA" and loose["status"] == "not watched" and loose["max_hold"] is None

    assert [(e["ticker"], e["reason"]) for e in f["exits"]] == [("NVDA", "hold")]

    [outcome] = f["outcomes"]  # one line per filing and symbol; the sale isn't copyable
    assert (outcome["symbol"], outcome["horizon"], outcome["n_trades"]) == ("NVDA", 20, 2)
    assert outcome["abn_ret"] == pytest.approx(0.06) and outcome["end_date"] == DAYS[-1]

    lb = f["leaderboard"]
    assert [e["name"] for e in lb["entered_top"]] == ["Nina New", "Wanda Watched"]
    assert lb["left_top"] == [{"name": "Otto Other", "rank_before": 1, "rank": 11}]
    assert lb["watched"][0] == {
        "name": "Wanda Watched", "rank": 9, "score": 0.006, "rank_before": 12, "score_before": 0.003}

    assert [p["title"] for p in f["proposals"]] == ["Add Nina New to the watchlist"]


def test_render_includes_every_section(db):
    text = digest.render(digest.facts(db, SINCE, UNTIL))
    for heading in ("NEW FILINGS", "ALERTS SENT", "OPEN POSITIONS", "EXITS TRIGGERED", "OUTCOMES REACHED",
                    "LEADERBOARD", "WAITING FOR YOUR REVIEW"):
        assert heading in text
    assert "Wanda Watched" in text and "alert score 72" in text and "(2 trades)" in text
    assert "Entered the top 10: Nina New (#1)" in text and "Watched: Wanda Watched #9 (was #12)" in text
    assert "Left the top 10: Otto Other (#1 -> #11)" in text


def test_a_quiet_day_is_short(conn):
    text = digest.render(digest.facts(conn, SINCE, UNTIL))
    assert "None from watched members." in text and "None logged." in text and "No leaderboard yet." in text
    assert len(text.splitlines()) < 30


class FakeAgent:
    def __init__(self, status="ok", summary="Wanda bought NVDA; nothing else needs you.", error=None):
        self.status, self.summary, self.error, self.calls = status, summary, error, []

    def __call__(self, conn, **kwargs):
        self.calls.append(kwargs)
        run_id = runner.start_run(conn, kwargs["agent"], {}, runner.MODEL)
        if self.status == "ok":
            runner.finish_run(conn, run_id, self.summary, [])
        else:
            runner.fail_run(conn, run_id, self.error, runner.Trace())
        return runner.RunResult(run_id, self.status, self.summary if self.status == "ok" else "", error=self.error)


def set_nightly(conn, day):
    conn.execute("INSERT INTO source_state (source, checked_at) VALUES ('pipeline.nightly', ?) "
                 "ON CONFLICT (source) DO UPDATE SET checked_at = excluded.checked_at", (day,))
    conn.commit()


def digest_runs(conn):
    return [dict(r) for r in conn.execute(
        "SELECT run_id, status, model, output FROM agent_runs WHERE agent = 'daily_digest' ORDER BY run_id")]


def test_narrative_goes_on_top_of_the_template(db):
    set_nightly(db, "2026-10-02")
    agent = FakeAgent()
    s = digest.run(db, now=lambda: NOW, agent_runner=agent)

    assert s.narrative and s.fallback_reason is None
    [run] = digest_runs(db)
    assert run["status"] == "ok" and run["output"].startswith("Wanda bought NVDA")
    assert "OPEN POSITIONS" in run["output"]
    call = agent.calls[0]
    assert call["backend"] == runner.CLAUDE_CODE and call["web_search_uses"] == 0
    assert '"filings"' in call["prompt"]  # the facts go to the model as JSON


def test_a_failed_narrative_falls_back_to_the_template(db):
    set_nightly(db, "2026-10-02")
    s = digest.run(db, now=lambda: NOW, agent_runner=FakeAgent(status="failed", error="claude not found"))

    assert not s.narrative and s.fallback_reason == "claude not found"
    failed, fallback = digest_runs(db)
    assert failed["status"] == "failed"
    assert fallback["status"] == "ok" and fallback["model"] is None
    assert fallback["output"].startswith("(Written from the template: claude not found)")


def test_template_only_makes_no_model_call(db):
    agent = FakeAgent()
    s = digest.run(db, now=lambda: NOW, template_only=True, agent_runner=agent)
    assert agent.calls == [] and s.text.startswith("Since ")
    assert len(digest_runs(db)) == 1


def test_dry_run_writes_nothing(db):
    digest.run(db, now=lambda: NOW, dry_run=True, agent_runner=FakeAgent())
    assert digest_runs(db) == []
    assert db.execute("SELECT 1 FROM source_state WHERE source = 'agents.digest'").fetchone() is None


def test_due_once_per_nightly_and_windows_chain(db):
    assert not digest.due(db)  # no nightly yet
    set_nightly(db, "2026-10-01")
    assert digest.due(db)
    first = digest.run(db, now=lambda: NOW - timedelta(days=1), template_only=True)
    assert first.since == "2026-09-30T22:30:00Z"  # the first digest covers 24 hours
    assert not digest.due(db)

    set_nightly(db, "2026-10-02")
    assert digest.due(db)
    second = digest.run(db, now=lambda: NOW, template_only=True)
    assert second.since == first.until and not digest.due(db)
