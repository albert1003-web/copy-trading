import json
from datetime import date, timedelta

import pytest

from agents import researcher, runner

TODAY = date(2026, 10, 5)


@pytest.fixture
def db(conn):
    conn.executescript("""
        INSERT INTO members (member_id, name, chamber) VALUES ('P1', 'Pat Member', 'house');
        INSERT INTO filings (doc_id, member_id, chamber, filing_date, first_seen_at) VALUES
          ('NEW', 'P1', 'house', '2026-10-02', '2026-10-02T15:00:00Z'),
          ('OLD', 'P1', 'house', '2025-06-01', '2025-06-01T15:00:00Z');
        INSERT INTO trades (trade_id, doc_id, member_id, symbol, action, tx_date, committee_relevant, sector,
                            industry, mcap_bucket, line_no) VALUES
          (1, 'NEW', 'P1', 'LMT', 'BUY', '2026-09-15', 1, 'Industrials', 'Aerospace & Defense', 'large', 1),
          (2, 'NEW', 'P1', 'LMT', 'BUY', '2026-09-15', 1, 'Industrials', 'Aerospace & Defense', 'large', 2),
          (3, 'NEW', 'P1', 'XYZ', 'BUY', '2026-09-15', 0, NULL, NULL, NULL, 3),
          (4, 'OLD', 'P1', 'LMT', 'SELL', '2025-05-20', 0, NULL, NULL, NULL, 1);
        INSERT INTO securities (symbol, name, sector, industry, market_cap, status, updated_at) VALUES
          ('LMT', 'Lockheed Martin', 'Industrials', 'Aerospace & Defense', 1.1e11, 'ok', '2026-10-01');
        INSERT INTO committee_memberships (member_id, congress, committee_id, committee_name) VALUES
          ('P1', 119, 'HSAS', 'Armed Services'), ('P1', 118, 'HSBA', 'Financial Services');
        INSERT INTO member_scores (member_id, as_of, rank, shrunk_score, n_filings, horizon) VALUES
          ('P1', '2026-10-01', 9, 0.002, 30, 20), ('P1', '2026-10-02', 4, 0.012, 31, 20);
    """)
    day = date(2026, 9, 1)
    i = 0
    while day <= date(2026, 10, 2):
        if day.weekday() < 5:
            conn.execute("INSERT INTO prices (ticker, date, close) VALUES ('LMT', ?, ?)", (day.isoformat(), 400 + i))
            i += 1
        day += timedelta(days=1)
    conn.commit()
    return conn


def buys(conn, scores=(72, 72, 55)):
    rows = conn.execute(
        "SELECT t.*, m.name AS member_name, m.chamber FROM trades t JOIN members m USING (member_id) "
        "WHERE t.doc_id = 'NEW' ORDER BY t.line_no").fetchall()
    return [(dict(r), s) for r, s in zip(rows, scores, strict=True)]


def earnings_soon(symbol, today):
    return {"next": "2026-10-21", "last": "2026-07-21"}


def test_facts(db):
    high = [b for b in buys(db) if b[1] >= researcher.MIN_SCORE]
    f = researcher.facts(db, high, earnings=earnings_soon, today=TODAY)

    assert f["congress"] == 119 and f["committees"] == ["Armed Services"]  # the trade's Congress only
    assert f["leaderboard"]["rank"] == 4 and f["leaderboard"]["as_of"] == "2026-10-02"
    [lmt] = f["symbols"]  # one entry per symbol
    assert lmt["company"] == "Lockheed Martin" and lmt["committee_relevant"]
    assert lmt["earnings"]["next"] == "2026-10-21" and lmt["earnings"]["inside_horizon"]
    assert lmt["earnings"]["trading_days_away"] == 12
    last = 400 + 23  # 24 weekdays from Sept 1 to Oct 2
    assert lmt["last_close"] == last and lmt["since_trade_date"] == pytest.approx(last / (400 + 10) - 1)
    assert lmt["move_5d"] == pytest.approx(last / (last - 5) - 1)
    assert lmt["prior_trades"] == 1 and lmt["prior_last"] == {"action": "SELL", "tx_date": "2025-05-20"}

    lines = researcher.render(f)
    assert lines[0] == "Committees (119th Congress): Armed Services"
    assert any("next earnings 2026-10-21 (~12 trading days: inside the 20-day horizon)" in x for x in lines)
    assert any("(context only)" in x for x in lines) and any("earlier LMT trades: 1" in x for x in lines)


def test_earnings_failure_is_not_fatal(db):
    def broken(symbol, today):
        raise ConnectionError("yahoo down")

    f = researcher.facts(db, buys(db)[:1], earnings=broken, today=TODAY)
    assert f["symbols"][0]["earnings"]["next"] is None
    assert any("next earnings date unavailable" in x for x in researcher.render(f))


class FakeAgent:
    def __init__(self, status="ok", summary="Lockheed won a $2B contract on Sept 30.\nSources:\nhttps://x.example"):
        self.status, self.summary, self.calls = status, summary, []

    def __call__(self, conn, **kwargs):
        self.calls.append(kwargs)
        run_id = runner.start_run(conn, kwargs["agent"], {**(kwargs.get("extra_inputs") or {})}, runner.MODEL)
        if self.status == "ok":
            runner.finish_run(conn, run_id, self.summary, [])
            return runner.RunResult(run_id, "ok", self.summary)
        runner.fail_run(conn, run_id, "timed out", runner.Trace())
        return runner.RunResult(run_id, "failed", error="Claude Code timed out after 180 s")


def test_below_the_threshold_nothing_runs(db):
    agent = FakeAgent()
    assert researcher.brief(db, [(t, 55) for t, _ in buys(db)], agent_runner=agent, earnings=earnings_soon) is None
    assert agent.calls == []


def test_brief_with_narrative(db):
    agent = FakeAgent()
    b = researcher.brief(db, buys(db), agent_runner=agent, earnings=earnings_soon)
    assert b.narrative.startswith("Lockheed won") and b.error is None and not b.reused
    assert b.fact_lines[0].startswith("Committees")
    call = agent.calls[0]
    assert call["timeout"] == researcher.TIMEOUT_SECONDS and call["web_search_uses"] == researcher.WEB_SEARCHES
    assert call["extra_inputs"] == {"doc_id": "NEW", "symbols": ["LMT"]}
    assert json.loads(call["prompt"].split("\n", 1)[1].rsplit("\n\n", 1)[0])["committees"] == ["Armed Services"]


def test_failed_narrative_keeps_the_facts(db):
    b = researcher.brief(db, buys(db), agent_runner=FakeAgent(status="failed"), earnings=earnings_soon)
    assert b.narrative is None and "timed out" in b.error and b.fact_lines


def test_a_retry_reuses_the_brief(db):
    agent = FakeAgent()
    first = researcher.brief(db, buys(db), agent_runner=agent, earnings=earnings_soon)
    again = researcher.brief(db, buys(db), agent_runner=agent, earnings=earnings_soon)
    assert len(agent.calls) == 1 and again.reused and again.narrative == first.narrative


def test_congress_of():
    assert researcher.congress_of(date(2025, 1, 2)) == 118
    assert researcher.congress_of(date(2025, 1, 3)) == 119
    assert researcher.congress_of(date(2026, 12, 31)) == 119
