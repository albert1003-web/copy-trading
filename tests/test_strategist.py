import json
from datetime import UTC, date, datetime

import pytest

from agents import runner, strategist
from agents.proposals import NOTE, WATCHLIST_ADD

NOW = datetime(2026, 10, 9, 23, 0, tzinfo=UTC)  # a Friday evening


@pytest.fixture
def db(conn):
    conn.executemany("INSERT INTO members (member_id, name, chamber) VALUES (?, ?, 'house')",
                     [("A1", "Ann Able"), ("B1", "Ben Best"), ("C1", "Cat Cole")])
    conn.execute("INSERT INTO watchlist (member_id, added_at) VALUES ('A1', '2026-09-01T00:00:00Z')")
    for as_of, rows in (("2026-10-01", [("A1", 1, 0.02), ("B1", None, 0.01), ("C1", 2, 0.005)]),
                        ("2026-10-09", [("A1", 2, 0.018), ("B1", 1, 0.03), ("C1", None, 0.004)])):
        for member, rank, score in rows:
            conn.execute("INSERT INTO member_scores (member_id, as_of, rank, shrunk_score, mean_abn_ret, hit_rate, "
                         "n_filings, horizon) VALUES (?, ?, ?, ?, ?, 0.6, 25, 20)",
                         (member, as_of, rank, score, score + 0.001))
    # Two alerted filings with matured 5- and 20-day outcomes (one filing has two trades: averaged).
    conn.execute("INSERT INTO filings (doc_id, member_id, chamber, first_seen_at) VALUES ('F1', 'A1', 'house', "
                 "'2026-07-01T00:00:00Z'), ('F2', 'A1', 'house', '2026-09-01T00:00:00Z')")
    # A backfilled filing alerted late (as during setup): never counted in alert performance.
    conn.execute("INSERT INTO filings (doc_id, member_id, chamber, first_seen_at, available_basis) "
                 "VALUES ('OLD', 'A1', 'house', '2026-10-01T00:00:00Z', 'filed')")
    conn.execute("INSERT INTO trades (trade_id, doc_id, member_id, symbol, action, line_no) "
                 "VALUES (9, 'OLD', 'A1', 'BE', 'BUY', 1)")
    conn.execute("INSERT INTO trade_outcomes (trade_id, d0_date, abn_ret_5, abn_ret_20, copyable) "
                 "VALUES (9, '2026-01-02', 0.5, 0.5, 1)")
    conn.execute("INSERT INTO alerts (trade_id, sent_at, score, rule) VALUES (9, '2026-09-01T00:00:00Z', 90, "
                 "'watchlist_buy')")
    for tid, doc, d0, abn5, abn20 in ((1, "F1", "2026-07-02", 0.01, 0.04), (2, "F1", "2026-07-02", 0.03, 0.00),
                                      (3, "F2", "2026-09-02", -0.02, None)):
        conn.execute("INSERT INTO trades (trade_id, doc_id, member_id, symbol, action, line_no) "
                     "VALUES (?, ?, 'A1', 'NVDA', 'BUY', ?)", (tid, doc, tid))
        conn.execute("INSERT INTO trade_outcomes (trade_id, d0_date, abn_ret_5, abn_ret_20, copyable) "
                     "VALUES (?, ?, ?, ?, 1)", (tid, d0, abn5, abn20))
        conn.execute("INSERT INTO alerts (trade_id, sent_at, score, rule) VALUES (?, '2026-10-07T15:00:00Z', 70, "
                     "'watchlist_buy')", (tid,))
    conn.execute("INSERT INTO exit_rules (label, description, position, recommended, confidence, reason, "
                 "computed_at) VALUES ('fixed_hold(days=5)', 'Hold 5 trading days', 0, 1, 'low', 'r', 'x')")
    for book, abn in (("hold_20", -0.01), ("walk_forward", -0.007)):
        conn.execute("INSERT INTO exit_backtests (rule, book, test_window, n_filings, mean_abn_ret, hit_rate) "
                     "VALUES ('x', ?, 'q', 10, 0.5, 0.4)", (book,))
        conn.execute("INSERT INTO exit_backtests (rule, book, test_window, n_filings, mean_abn_ret, hit_rate) "
                     "VALUES ('x', ?, 'all', 200, ?, 0.4)", (book, abn))
    conn.commit()
    return conn


def set_nightly(conn, day):
    conn.execute("INSERT INTO source_state (source, checked_at) VALUES ('pipeline.nightly', ?) "
                 "ON CONFLICT (source) DO UPDATE SET checked_at = excluded.checked_at", (day,))
    conn.commit()


def test_week_targets():
    assert strategist.target_week(date(2026, 10, 9)) == "2026-W41"   # Friday: that week
    assert strategist.target_week(date(2026, 10, 8)) == "2026-W40"   # Thursday: the week before
    assert strategist.target_week(date(2026, 10, 12)) == "2026-W41"  # Monday: catches up last week


def test_due_once_a_week(db):
    assert strategist.due(db) is None
    set_nightly(db, "2026-10-08")
    assert strategist.due(db) == "2026-W40"  # first review: last completed week
    strategist.run(db, week="2026-W40", template_only=True, now=lambda: NOW)
    assert strategist.due(db) is None
    set_nightly(db, "2026-10-09")
    assert strategist.due(db) == "2026-W41"


def test_facts(db):
    f = strategist.facts(db, "2026-W41", date(2026, 10, 9))
    lb = f["leaderboard"]
    assert lb["as_of"] == "2026-10-09" and lb["compared_with"] == "2026-10-01"  # the latest 7+ days before
    top = {m["name"]: m for m in lb["top"]}
    assert set(top) == {"Ann Able", "Ben Best"} and top["Ann Able"]["watched"]

    a = f["alerts"]
    assert a["all_5d"]["filings"] == 2  # per filing, not per trade
    assert a["all_5d"]["mean_abn_ret"] == pytest.approx((0.02 + -0.02) / 2)
    assert a["all_20d"] == {"filings": 1, "mean_abn_ret": pytest.approx(0.02), "hit_rate": 1.0}
    assert len(a["this_week"]) == 2

    books = {b["book"]: b for b in f["exits"]["books"]}
    assert books["walk_forward"]["mean_abn_ret"] == -0.007 and books["walk_forward"]["n_filings"] == 200
    assert f["exits"]["recommended"]["label"] == "fixed_hold(days=5)"


def test_leaderboard_changes_vs_a_week_earlier(db):
    db.execute("UPDATE member_scores SET as_of = '2026-10-02' WHERE as_of = '2026-10-01'")
    lb = strategist.facts(db, "2026-W41", date(2026, 10, 9))["leaderboard"]
    assert lb["compared_with"] == "2026-10-02"
    assert [m["name"] for m in lb["newly_ranked"]] == ["Ben Best"]
    assert lb["dropped"] == [{"name": "Cat Cole", "rank_before": 2}]
    ann = next(m for m in lb["top"] if m["name"] == "Ann Able")
    assert (ann["rank"], ann["rank_before"]) == (2, 1)
    text = strategist.render(strategist.facts(db, "2026-W41", date(2026, 10, 9)))
    assert "(was #1)" in text and "Dropped out of the ranking: Cat Cole" in text


class FakeAgent:
    def __init__(self, proposals=()):
        self.proposals, self.calls = list(proposals), []

    def __call__(self, conn, **kwargs):
        self.calls.append(kwargs)
        run_id = runner.start_run(conn, kwargs["agent"], kwargs.get("extra_inputs") or {}, runner.MODEL)
        from agents import proposals as props

        found, _ = props.validate(conn, self.proposals)
        runner.finish_run(conn, run_id, "Ben is the new leader.", found)
        return runner.RunResult(run_id, "ok", "Ben is the new leader.", found)


def test_review_with_proposals_and_next_week_comparison(db):
    evidence = [{"claim": "#1, +3.0%", "source": "SELECT * FROM member_scores"}]
    agent = FakeAgent([{"kind": WATCHLIST_ADD, "member_id": "B1", "title": "Add Ben", "rationale": "r",
                        "evidence": evidence}])
    s = strategist.run(db, week="2026-W41", now=lambda: NOW, agent_runner=agent)
    assert s.narrative and s.proposals == 1 and s.text.startswith("Ben is the new leader.")
    assert agent.calls[0]["effort"] == "high" and agent.calls[0]["web_search_uses"] == 0
    inputs = json.loads(db.execute("SELECT inputs FROM agent_runs WHERE run_id = ?", (s.run_id,)).fetchone()[0])
    assert inputs["snapshot"]["books"] == {"hold_20": -0.01, "walk_forward": -0.007}
    kinds = [r[0] for r in db.execute("SELECT kind FROM agent_proposals WHERE run_id = ?", (s.run_id,))]
    assert kinds == [WATCHLIST_ADD]

    # Next week: the books moved, and the same proposal again is only a note.
    db.execute("UPDATE exit_backtests SET mean_abn_ret = -0.005 WHERE book = 'walk_forward' AND test_window = 'all'")
    again = strategist.run(db, week="2026-W42", now=lambda: NOW, agent_runner=FakeAgent(agent.proposals))
    assert "walk_forward  200 filings, mean excess -0.50% (last week -0.70%)" in again.text
    kinds = [r[0] for r in db.execute("SELECT kind FROM agent_proposals WHERE run_id = ?", (again.run_id,))]
    assert kinds == [NOTE]


def test_template_fallback(db):
    def broken(conn, **kwargs):
        run_id = runner.start_run(conn, kwargs["agent"], {}, runner.MODEL)
        runner.fail_run(conn, run_id, "timed out", runner.Trace())
        return runner.RunResult(run_id, "failed", error="timed out")

    s = strategist.run(db, week="2026-W41", now=lambda: NOW, agent_runner=broken)
    assert not s.narrative and s.text.startswith("(Written from the template: timed out)")
    assert "LEADERBOARD" in s.text and "EXITS" in s.text
