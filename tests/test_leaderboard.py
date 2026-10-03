from datetime import date
from itertools import count

import pytest

from analytics import leaderboard
from analytics.leaderboard import HORIZONS

TODAY = date(2026, 10, 1)
_ids = count(1)


@pytest.fixture
def db(conn):
    return conn


def add(conn, member, doc, ret, abn, *, d0="2026-03-03", trades=1, copyable=1, matured=HORIZONS):
    """One filing with `trades` identical trades; every matured horizon gets ret/abn, the rest NULL."""
    if member:
        conn.execute("INSERT OR IGNORE INTO members (member_id, name, chamber) VALUES (?, ?, 'house')",
                     (member, member.title()))
    conn.execute("INSERT OR IGNORE INTO filings (doc_id, chamber, first_seen_at) VALUES (?, 'house', '2026-03-03')",
                 (doc,))
    cols = [c for h in HORIZONS for c in (f"ret_{h}", f"abn_ret_{h}")]
    for _ in range(trades):
        trade_id = next(_ids)
        conn.execute("INSERT INTO trades (trade_id, doc_id, line_no, member_id) VALUES (?, ?, ?, ?)",
                     (trade_id, doc, trade_id, member))
        values = [v if h in matured else None for h in HORIZONS for v in (ret, abn)]
        conn.execute(f"INSERT INTO trade_outcomes (trade_id, d0_date, copyable, {', '.join(cols)}) "
                     f"VALUES (?, ?, ?{', ?' * len(cols)})", (trade_id, d0, copyable, *values))
    conn.commit()


def scores(conn, as_of=None):
    as_of = as_of or TODAY.isoformat()
    return {r["member_id"]: dict(r) for r in conn.execute("SELECT * FROM member_scores WHERE as_of = ?", (as_of,))}


def test_averages_vs_spy_are_per_filing(db):
    add(db, "A", "A1", 0.10, 0.04, trades=10)  # SPY +6%: one observation, not ten
    add(db, "A", "A2", -0.02, -0.05)  # SPY +3%
    add(db, "A", "A3", 0.50, 0.50, copyable=0)  # a sale: not a buy we could copy
    add(db, "A", "A4", 0.20, 0.20, matured=(1,))  # only h=1 has happened
    add(db, None, "X1", 0.90, 0.90)  # no member
    leaderboard.run(db, today=TODAY)

    a = scores(db)["A"]
    assert (a["n_filings"], a["n_trades"], a["horizon"]) == (2, 11, 20)
    assert a["mean_ret"] == pytest.approx(0.04)
    assert a["mean_spy_ret"] == pytest.approx(0.045)
    assert a["mean_abn_ret"] == pytest.approx(-0.005)
    assert a["median_abn_ret"] == pytest.approx(-0.005)
    assert a["hit_rate"] == 0.5
    assert a["rank"] is None  # below MIN_N
    h1 = db.execute("SELECT n_filings FROM member_horizon_stats WHERE member_id = 'A' AND horizon = 1").fetchone()
    assert h1[0] == 3


def test_ranking_needs_min_filings_and_shrinks_lucky_members(db):
    noise = lambda i: 0.20 if i % 2 else -0.20  # noqa: E731  (filing returns are noisy)
    for m in range(10):
        for i in range(20):
            add(db, f"O{m}", f"O{m}-{i}", noise(i), noise(i))
    for i in range(25):
        add(db, "BIG1", f"B1-{i}", 0.05, 0.02 + noise(i))
        add(db, "BIG2", f"B2-{i}", 0.00, -0.01 + noise(i))
    for i, abn in enumerate((0.10, 0.30, 0.50)):
        add(db, "LUCKY", f"L-{i}", abn, abn)
    s = leaderboard.run(db, today=TODAY)

    board = scores(db)
    assert board["BIG1"]["rank"] == 1 and board["BIG2"]["rank"] == 12
    assert board["LUCKY"]["rank"] is None
    assert board["LUCKY"]["mean_abn_ret"] == pytest.approx(0.30)
    assert board["LUCKY"]["shrunk_score"] < 0.30 / 2  # 3 lucky filings are mostly noise
    assert sorted(r["rank"] for r in board.values() if r["rank"]) == list(range(1, 13))
    assert (s.members, s.ranked) == (13, 12)


def test_one_huge_filing_doesnt_flatten_every_score(db):
    noise = lambda i: 0.05 if i % 2 else -0.05  # noqa: E731
    for m, edge in enumerate((-0.04, -0.02, 0.0, 0.02, 0.04)):
        for i in range(30):
            add(db, f"M{m}", f"M{m}-{i}", edge + noise(i), edge + noise(i))
    add(db, "M2", "M2-huge", 3.68, 3.68)  # a +368% filing, as seen live
    leaderboard.run(db, today=TODAY)
    board = scores(db)
    assert board["M2"]["mean_abn_ret"] > 0.1  # the displayed mean keeps it
    assert len({round(r["shrunk_score"], 6) for r in board.values()}) == 5  # members still differ
    # ranked on its ordinary filings: one outlier doesn't vault M2 to the top
    assert [board[f"M{m}"]["rank"] for m in (4, 3, 2, 1, 0)] == [1, 2, 3, 4, 5]


def test_consistency_counts_years_with_enough_filings(db):
    for i in range(3):
        add(db, "C", f"C22-{i}", 0.05, 0.02, d0="2022-05-02")
        add(db, "C", f"C23-{i}", -0.05, -0.02, d0="2023-05-01")
        add(db, "D", f"D22-{i}", 0.05, 0.02, d0="2022-05-02")
    for i in range(2):  # too few filings in 2024 to count
        add(db, "C", f"C24-{i}", 0.05, 0.02, d0="2024-05-01")
        add(db, "D", f"D24-{i}", 0.05, 0.02, d0="2024-05-01")
    leaderboard.run(db, today=TODAY)
    board = scores(db)
    assert board["C"]["consistency"] == 0.5
    assert board["D"]["consistency"] is None  # one qualifying year


def test_snapshots_replace_the_same_day_and_keep_earlier_ones(db):
    add(db, "A", "A1", 0.10, 0.04)
    leaderboard.run(db, today=TODAY)
    leaderboard.run(db, today=TODAY)
    assert db.execute("SELECT COUNT(*) FROM member_scores").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM member_horizon_stats").fetchone()[0] == len(HORIZONS)

    add(db, "A", "A2", 0.00, -0.02)
    leaderboard.run(db, today=date(2026, 10, 2))
    assert scores(db)["A"]["n_filings"] == 1
    assert scores(db, "2026-10-02")["A"]["n_filings"] == 2


def test_member_scores_match_the_rank_horizon(db):
    add(db, "A", "A1", 0.10, 0.04)
    leaderboard.run(db, today=TODAY)
    h20 = dict(db.execute("SELECT * FROM member_horizon_stats WHERE member_id = 'A' AND horizon = 20").fetchone())
    a = scores(db)["A"]
    for col in ("n_filings", "n_trades", "mean_ret", "mean_spy_ret", "mean_abn_ret", "hit_rate", "shrunk_score"):
        assert a[col] == h20[col]


def test_empty_database_writes_nothing(conn):
    s = leaderboard.run(conn, today=TODAY)
    assert s.members == 0 and not s.errors
    assert leaderboard.report(conn)[0].startswith("Leaderboard: none yet")
