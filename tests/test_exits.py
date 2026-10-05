import json
from datetime import date, timedelta
from itertools import count

import pytest

from analytics import exits, leaderboard, outcomes
from analytics.exits import (
    ZERO_COSTS,
    AtrStop,
    Costs,
    FixedHold,
    Market,
    MemberSale,
    Series,
    Signal,
    StopTarget,
    TrailingStop,
    simulate,
)

START = date(2026, 2, 23)  # a Monday
_ids = count(1)


def weekdays(n: int, start: date = START) -> list[str]:
    days, day = [], start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def series(bars) -> Series:
    """bars: (open, high, low, close) per day, or None for a day without a bar."""
    o, h, lo, c = ([b[k] if b else None for b in bars] for k in range(4))
    return Series(o, h, lo, c, max(i for i, b in enumerate(bars) if b))


def flat(n: int, price: float = 100.0) -> list:
    return [(price, price, price, price)] * n


def add_bars(conn, symbol, bars, start=START, factor=lambda i: 1.0):
    """bars: (open, high, low, close) per weekday (None = no bar); adj_close = close * factor(i)."""
    conn.executemany(
        "INSERT OR REPLACE INTO prices (ticker, date, open, high, low, close, adj_close) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(symbol, d, *b, b[3] * factor(i)) for i, (d, b) in enumerate(zip(weekdays(len(bars), start), bars,
                                                                            strict=True)) if b],
    )
    conn.commit()


def sig(symbol="X", d0=0, member="A", bucket=None, doc=None) -> Signal:
    trade_id = next(_ids)
    return Signal(trade_id, doc or f"D{trade_id}", member, symbol, bucket, d0)


# --- rules on daily bars ------------------------------------------------------------------------------


def test_fixed_hold_exits_at_the_close():
    s = series([(100, 101, 99, 100), (100, 103, 99, 102), (102, 106, 101, 105)])
    assert simulate(s, 0, FixedHold(2)) == exits.Exit(2, 105, "close", "hold")


def test_no_d0_open_means_no_trade():
    s = series([None, (100, 101, 99, 100)])
    assert simulate(s, 0, FixedHold(1)) is None


def test_stop_gap_down_fills_at_the_open():
    s = series([(100, 101, 99, 100), (85, 86, 80, 82)])
    assert simulate(s, 0, StopTarget(stop=0.10)) == exits.Exit(1, 85, "open", "stop")


def test_stop_and_target_in_one_day_counts_as_the_stop():
    s = series([(100, 101, 99, 100), (100, 115, 88, 110)])
    ex = simulate(s, 0, StopTarget(stop=0.10, target=0.10))
    assert (ex.idx, ex.price, ex.at, ex.reason) == (1, pytest.approx(90), "intraday", "stop")


def test_target_fills_at_the_level_or_a_gap_up_open():
    s = series([(100, 101, 99, 100), (101, 111, 99, 108)])
    ex = simulate(s, 0, StopTarget(stop=None, target=0.10))
    assert (ex.idx, ex.price, ex.at, ex.reason) == (1, pytest.approx(110), "intraday", "target")
    gap = series([(100, 101, 99, 100), (115, 118, 112, 116)])
    assert simulate(gap, 0, StopTarget(stop=None, target=0.10)) == exits.Exit(1, 115, "open", "target")


def test_stop_on_d0_itself_counts_but_not_a_d0_open_gap():
    s = series([(100, 100, 89, 95), (95, 96, 94, 95)])
    ex = simulate(s, 0, StopTarget(stop=0.10))
    assert (ex.idx, ex.reason, ex.at) == (0, "stop", "intraday")


def test_max_hold_caps_every_rule():
    s = series(flat(10))
    assert simulate(s, 0, StopTarget(stop=0.5, max_hold=3)) == exits.Exit(3, 100, "close", "hold")
    assert simulate(s, 0, MemberSale(max_hold=4)) == exits.Exit(4, 100, "close", "hold")


def test_trailing_high_updates_after_the_days_check():
    s = series([
        (100, 100, 99, 100),
        (100, 120, 91, 118),  # stop still 90 (high 100): a high-first update would stop out at 108
        (117, 118, 107, 110),  # stop 108 now
    ])
    ex = simulate(s, 0, TrailingStop(0.10))
    assert (ex.idx, ex.price, ex.reason) == (2, pytest.approx(108), "stop")


def test_atr_uses_only_bars_before_d0():
    pre = [(100, 101, 99, 100)] * 15  # true range 2 every day
    calm = series(pre + [(100, 101, 99, 100), (100, 101, 99, 100)])
    wild = series(pre + [(100, 140, 60, 100), (100, 101, 99, 100)])
    assert exits.atr_before(calm, 15, 14) == pytest.approx(2.0)
    assert exits.atr_before(wild, 15, 14) == pytest.approx(2.0)
    assert exits.atr_before(calm, 10, 14) is None  # not enough history

    s = series(pre + [(100, 100, 99, 100), (100, 100, 93.5, 99)])  # stop = 100 - 3 * 2 = 94
    ex = simulate(s, 15, AtrStop(mult=3))
    assert (ex.idx, ex.price, ex.reason) == (16, pytest.approx(94), "stop")


def test_member_sale_exits_at_the_sale_d0_open():
    s = series(flat(3) + [(104, 105, 103, 104)] + flat(3))
    assert simulate(s, 0, MemberSale(), sale=3) == exits.Exit(3, 104, "open", "sale")
    assert simulate(s, 2, MemberSale(max_hold=2), sale=1).reason == "hold"  # a sale at or before D0 is ignored
    assert simulate(s, 0, FixedHold(5), sale=3).reason == "hold"  # other rules ignore sales


def test_missing_bar_on_the_exit_day_exits_at_the_next_open():
    s = series([(100, 100, 100, 100), None, (103, 104, 102, 103)])
    assert simulate(s, 0, FixedHold(1)) == exits.Exit(2, 103, "open", "hold")


def test_delisted_exits_at_the_last_close_and_calendar_end_stays_open():
    s = series([(100, 100, 100, 100), (98, 99, 97, 97), None, None, None])
    assert simulate(s, 0, FixedHold(4)) == exits.Exit(1, 97, "close", "data_end")
    assert simulate(series(flat(3)), 0, FixedHold(5)) == exits.Exit(2, 100, "close", "open")


# --- with a database ----------------------------------------------------------------------------------


def test_zero_cost_fixed_hold_matches_outcomes(conn):
    n = 80
    spy = [(400 + i - 0.5, 400 + i + 1, 400 + i - 1, 400 + i) for i in range(n)]
    aapl = [(100 + 0.3 * i - 0.5, 100 + 0.3 * i + 1, 100 + 0.3 * i - 1.5, 100 + 0.3 * i) for i in range(n)]
    add_bars(conn, "SPY", spy)
    add_bars(conn, "AAPL", aapl, factor=lambda i: 0.98 if i < 20 else 1.0)  # a dividend on day 20
    conn.execute("INSERT INTO filings (doc_id, chamber, first_seen_at, available_at, available_basis) "
                 "VALUES ('F1', 'house', '2026-03-05T00:00:00Z', '2026-03-02T21:00:00Z', 'filed')")
    conn.execute("INSERT INTO trades (trade_id, doc_id, line_no, symbol, ticker_status, action, asset_type) "
                 "VALUES (1, 'F1', 1, 'AAPL', 'listed', 'BUY', 'stock')")
    conn.commit()
    outcomes.run(conn)
    row = dict(conn.execute("SELECT * FROM trade_outcomes WHERE trade_id = 1").fetchone())

    market = Market(conn)
    [signal] = exits.load_signals(market)
    assert market.cal.days[signal.d0] == row["d0_date"] == "2026-03-03"
    for h in outcomes.HORIZONS:
        tr = exits.evaluate(market, signal, FixedHold(h))
        assert tr.ret == pytest.approx(row[f"ret_{h}"], abs=1e-12)
        assert tr.abn == pytest.approx(row[f"abn_ret_{h}"], abs=1e-12)
    lines, ok = exits.check(conn)
    assert ok and "5 trade-horizons compared, 0 mismatched" in lines[0]


def test_slippage_by_market_cap_bucket(conn):
    add_bars(conn, "SPY", flat(5, 400))
    add_bars(conn, "X", [(100, 100, 100, 100), (100, 110, 100, 110)] + flat(3, 110))
    costs = Costs()
    assert costs.slippage("mega") == pytest.approx(0.0005)
    assert costs.slippage(None) == costs.slippage("unheard-of") == pytest.approx(0.0075)

    tr = exits.evaluate(Market(conn), sig(bucket="small"), FixedHold(1), costs)
    assert tr.entry == pytest.approx(100 * 1.004)
    assert tr.ret == pytest.approx(110 * 0.996 / (100 * 1.004) - 1)
    assert tr.abn == pytest.approx(tr.ret)  # SPY flat; no costs on the benchmark


def add_outcome(conn, member, symbol, action, d0, *, abn=None, tx_date=None, copyable=None):
    trade_id = next(_ids)
    doc = f"D{trade_id}"
    conn.execute("INSERT OR IGNORE INTO members (member_id, name, chamber) VALUES (?, ?, 'house')", (member, member))
    conn.execute("INSERT INTO filings (doc_id, chamber, first_seen_at) VALUES (?, 'house', ?)", (doc, d0))
    conn.execute("INSERT INTO trades (trade_id, doc_id, line_no, member_id, symbol, action, tx_date) "
                 "VALUES (?, ?, 1, ?, ?, ?, ?)", (trade_id, doc, member, symbol, action, tx_date))
    copyable = int(action == "BUY") if copyable is None else copyable
    conn.execute("INSERT INTO trade_outcomes (trade_id, d0_date, copyable, ret_20, abn_ret_20) VALUES (?, ?, ?, ?, ?)",
                 (trade_id, d0, copyable, abn, abn))
    return trade_id


def test_member_sale_uses_the_sale_disclosure_d0(conn):
    days = weekdays(30)
    add_bars(conn, "SPY", flat(30, 400))
    add_bars(conn, "X", flat(30))
    add_outcome(conn, "A", "X", "BUY", days[5], tx_date=days[2])
    add_outcome(conn, "A", "X", "SELL", days[3])  # disclosed before the buy
    add_outcome(conn, "B", "X", "SELL", days[7])  # another member
    add_outcome(conn, "A", "Y", "SELL", days[8])  # another symbol
    add_outcome(conn, "A", "X", "SELL_PARTIAL", days[12], tx_date=days[1])  # traded first, disclosed later
    conn.commit()

    market = Market(conn)
    sales = exits.sale_days(market)
    [buy] = exits.load_signals(market)
    assert exits.next_sale(sales, buy) == 12
    tr = exits.evaluate(market, buy, MemberSale(), sale=exits.next_sale(sales, buy))
    assert (tr.exit.idx, tr.exit.at, tr.exit.reason) == (12, "open", "sale")


def test_sale_proceeds_settle_the_next_trading_day(conn):
    add_bars(conn, "SPY", flat(10, 400))
    for symbol in ("X", "Y", "Z"):
        add_bars(conn, symbol, flat(10))
    market = Market(conn)
    signals = [sig("X", d0=0), sig("Y", d0=1), sig("Z", d0=2)]
    res = exits.run_portfolio(market, signals, FixedHold(1), ZERO_COSTS, capital=1000, size=1.0)
    # X sells at day 1's close; its cash settles on day 2, so Y (day 1) is skipped and Z (day 2) is bought.
    assert [tr.signal.symbol for tr in res.trades] == ["X", "Z"]
    assert (res.skipped_cash, res.min_settled) == (1, 0.0)
    assert res.total_return == pytest.approx(0.0)


def test_held_symbols_and_full_books_are_skipped(conn):
    add_bars(conn, "SPY", flat(10, 400))
    for symbol in ("X", "Y"):
        add_bars(conn, symbol, flat(10))
    market = Market(conn)
    signals = [sig("X", d0=0), sig("X", d0=0), sig("Y", d0=0)]
    res = exits.run_portfolio(market, signals, FixedHold(5), ZERO_COSTS, size=0.1, max_positions=1)
    assert (len(res.trades), res.skipped_held, res.skipped_full) == (1, 1, 1)


def test_max_drawdown_follows_the_equity_path(conn):
    add_bars(conn, "SPY", flat(6, 400))
    add_bars(conn, "X", [(100, 100, 100, 100), (120, 120, 120, 120), (90, 90, 90, 90), (95, 95, 95, 95)]
             + flat(2, 95))
    res = exits.run_portfolio(Market(conn), [sig("X", d0=0)], FixedHold(3), ZERO_COSTS, capital=100, size=1.0)
    assert [e for _, e in res.equity] == pytest.approx([100, 120, 90, 95])
    assert res.max_drawdown == pytest.approx(0.25)
    assert (res.n_trades, res.mean_ret, res.hit_rate) == (1, pytest.approx(-0.05), 0.0)


def test_ranked_universe_uses_only_matured_filings(conn):
    days = weekdays(260, date(2025, 1, 1))
    add_bars(conn, "SPY", flat(260, 400), start=date(2025, 1, 1))
    early, late = days[10], days[-25]
    for i in range(leaderboard.MIN_N):
        wobble = 0.001 * (i % 3)
        add_outcome(conn, "A", "X", "BUY", early, abn=0.05 + wobble)  # matured long before
        add_outcome(conn, "C", "X", "BUY", early, abn=-0.02 + wobble)  # matured, but negative
        add_outcome(conn, "B", "X", "BUY", late, abn=0.08 + wobble)  # D0 + 20 not passed by day -5
    add_outcome(conn, "A", "Z", "BUY", days[-5], copyable=1)
    add_outcome(conn, "B", "Z", "BUY", days[-5], copyable=1)
    conn.commit()

    market = Market(conn)
    assert exits.ranked_members(market, len(days) - 5) == {"A"}
    assert set(leaderboard.filings(conn, 20, d0_before=days[100])) == {"A", "C"}
    assert set(leaderboard.filings(conn, 20)) == {"A", "B", "C"}

    signals = [s for s in exits.load_signals(market) if s.symbol == "Z"]
    assert {s.member_id for s in exits.select(market, signals, "all")} == {"A", "B"}
    assert {s.member_id for s in exits.select(market, signals, {"B"})} == {"B"}
    conn.execute("INSERT INTO watchlist (member_id, added_at) VALUES ('B', '2026-01-01')")
    assert {s.member_id for s in exits.select(market, signals, "watchlist")} == {"B"}
    # ranked is decided at the quarter start, from filings matured by then
    q = market.quarter_start(len(days) - 5)
    assert exits.ranked_members(market, q) == {"A"}
    assert {s.member_id for s in exits.select(market, signals, "ranked")} == {"A"}


# --- walk-forward (M4.2) ------------------------------------------------------------------------------

WF_START = date(2022, 12, 26)  # a Monday; 830 weekdays (no holidays) reach late February 2026
WF_DAYS = weekdays(830, WF_START)


def path_bars(d0: int, after) -> list:
    """Wide-range flat bars before D0 (ATR ~20, so ATR stops sit far away), then after(k) closes from D0 on."""
    bars, prev = [], 100.0
    for i in range(len(WF_DAYS)):
        if i < d0:
            bars.append((100, 110, 90, 100))
            continue
        k = i - d0
        close = after(k)
        open_ = 100.0 if k == 0 else prev
        bars.append((open_, max(open_, close), min(open_, close), close))
        prev = close
    return bars


def crash(k):  # -2% a day to -50%: an 8% stop is the best exit
    return max(100 * (1 - 0.02 * (k + 1)), 50)


def dip_then_soar(k):  # -9% on D0 (stops out an 8% stop), then +3 a day: holding 60 days wins big
    return 91 + 3 * k


@pytest.fixture
def wf(conn, monkeypatch):
    monkeypatch.setattr(exits, "MIN_TRAIN_FILINGS", 2)
    add_bars(conn, "SPY", flat(len(WF_DAYS), 400), start=WF_START)

    def signal(symbol, d0, after, member="A"):
        add_bars(conn, symbol, path_bars(d0, after), start=WF_START)
        add_outcome(conn, member, symbol, "BUY", WF_DAYS[d0])
        conn.commit()

    return signal


def test_test_quarters_need_two_years_and_completed_trades(conn):
    add_bars(conn, "SPY", flat(len(WF_DAYS), 400), start=WF_START)
    market = Market(conn)
    starts = [market.cal.days[q.start] for q in exits.test_quarters(market)]
    assert starts == ["2025-01-01", "2025-04-01", "2025-07-01"]  # 2025Q4's last D0 + 60 is past the calendar
    assert all(q.end + exits.MAX_HOLD < market.n_days for q in exits.test_quarters(market))


def test_training_picks_the_best_exit_and_ignores_trades_that_end_inside_the_quarter(wf, conn):
    for i, d0 in enumerate((50, 150, 300)):
        wf(f"C{i}", d0, crash)
    q1 = WF_DAYS.index("2025-01-01")
    wf("LEAK", q1 - 10, dip_then_soar)  # its 60-day exit lands inside 2025Q1: it must not count
    market = Market(conn)
    ev = exits.Evaluated(market, ZERO_COSTS)
    signals = exits.load_signals(market)
    q = exits.test_quarters(market)[0]
    rule, scores, n, span = exits.choose(ev, signals, q)
    assert (n, span) == (3, "2023-01-02..2024-12-31")
    assert rule == StopTarget(stop=0.08)  # ties with stop+target (same -8%): the earlier grid rule wins
    assert scores[StopTarget(stop=0.08)] == pytest.approx(-0.08)
    assert scores[StopTarget(stop=0.08, target=0.20)] == scores[rule]


def test_a_trade_that_exits_before_the_quarter_does_count(wf, conn):
    for i, d0 in enumerate((50, 150, 300)):
        wf(f"C{i}", d0, crash)
    wf("SOAR", 400, dip_then_soar)  # exits long before 2025, so it counts: riding the soar now wins on average
    market = Market(conn)
    rule, _scores, n, _span = exits.choose(exits.Evaluated(market, ZERO_COSTS), exits.load_signals(market),
                                           exits.test_quarters(market)[0])
    assert (n, rule) == (4, TrailingStop(0.10))  # caps the crashes near -10%, holds the soar to day 60


def test_too_few_training_filings_fall_back_to_hold_20(wf, conn, monkeypatch):
    monkeypatch.setattr(exits, "MIN_TRAIN_FILINGS", 50)
    wf("C0", 50, crash)
    market = Market(conn)
    rule, scores, n, _ = exits.choose(exits.Evaluated(market, ZERO_COSTS), exits.load_signals(market),
                                      exits.test_quarters(market)[0])
    assert (rule, scores, n) == (exits.FALLBACK, {}, 1)


def test_walk_forward_book_is_one_continuous_t1_ledger(wf, conn):
    for i, d0 in enumerate((50, 150, 300)):
        wf(f"C{i}", d0, crash)
    q2 = WF_DAYS.index("2025-04-01")
    wf("LATE", q2 - 3, crash)  # bought at the end of 2025Q1, stopped out in 2025Q2
    wf("NEXT", q2, crash)  # 2025Q2's first day: LATE's position is still open, no settled cash left
    market = Market(conn)
    books = exits.walk_forward(market, "all", ZERO_COSTS, capital=1000, size=1.0)
    wf_book = books[0]
    assert [b.name for b in books] == ["walk_forward", "hold_1", "hold_5", "hold_10", "hold_20", "hold_60"]
    assert [tr.signal.symbol for tr in wf_book.result.trades] == ["LATE"]
    assert [(tr.signal.symbol, why) for tr, why in wf_book.result.skipped] == [("NEXT", "cash")]
    assert {tr.signal.symbol for tr in wf_book.trades} == {"LATE", "NEXT"}  # stats still cover both signals


def test_run_writes_out_of_sample_rows_per_book_and_quarter(wf, conn):
    for i, d0 in enumerate((50, 150, 300)):
        wf(f"C{i}", d0, crash)
    q1 = WF_DAYS.index("2025-01-01")
    wf("T1", q1 + 5, crash)
    s = exits.run(conn, "all", now=lambda: "2026-03-06T22:00:00Z")
    assert (s.quarters, s.rows, s.fallbacks, s.errors) == (3, 6 * 4, 0, [])
    rows = [dict(r) for r in conn.execute("SELECT * FROM exit_backtests ORDER BY run_id")]
    first = rows[0]
    assert (first["book"], first["rule"], first["train_window"]) == ("walk_forward", "stop_target",
                                                                     "2023-01-02..2024-12-31")
    assert json.loads(first["params"]) == {"stop": 0.08, "target": None, "max_hold": 60}
    slip = Costs().slippage(None)  # run() charges the default 75 bps per side for an unknown size
    net = lambda price: price * (1 - slip) / (100 * (1 + slip)) - 1  # noqa: E731
    assert first["n_filings"] == 1 and first["mean_abn_ret"] == pytest.approx(net(92))
    assert first["universe"] == "all" and first["computed_at"] == "2026-03-06T22:00:00Z"
    hold20 = [r for r in rows if r["book"] == "hold_20"][0]
    assert hold20["train_window"] is None and hold20["mean_abn_ret"] == pytest.approx(net(crash(20)))
    assert s.beats == 4  # the -8% stop beats holding 5-60 days of a crash, but not 1 day (-4%)
    exits.run(conn, "all")
    assert conn.execute("SELECT COUNT(*) FROM exit_backtests").fetchone()[0] == 24  # replaced, not appended
    assert "Verdict: walk-forward beat 4 of 5" in "\n".join(exits.report(conn))


# --- recommendation (M4.3) ----------------------------------------------------------------------------


def test_every_grid_rule_round_trips_through_its_label():
    for rule in exits.GRID + exits.BASELINES:
        assert exits.parse_rule(exits.label(rule)) == rule
        assert exits.describe(rule)
    assert exits.parse_rule("trailing_stop(pct=0.1, max_hold=30)") == TrailingStop(0.1, max_hold=30)
    for text in (None, "", "sell when it feels right", "fixed_hold(5)", "nope(days=5)", "fixed_hold(days=x)"):
        assert exits.parse_rule(text) is None
    assert exits.describe(FixedHold(1)) == "Hold 1 trading day, then sell at the close"
    assert exits.describe(StopTarget(stop=0.08, target=0.2)).startswith("Sell if it falls 8% below or rises 20% above")


@pytest.mark.parametrize("wf, holds, level", [
    ((0.01, 150), [-0.01, -0.02, -0.005, 0.0, 0.02], "high"),  # beats 4 of 5, positive, 150 filings
    ((-0.001, 150), [-0.01, -0.02, -0.005, 0.0, 0.02], "medium"),  # beats 3, but negative
    ((0.01, 40), [-0.01, -0.02, -0.005, -0.03, -0.04], "low"),  # too few filings
    ((-0.007, 236), [-0.0013, -0.0022, -0.0063, -0.0099, -0.0073], "low"),  # the M4.2 result: beat 2 of 5
])
def test_confidence_from_the_out_of_sample_record(wf, holds, level):
    alls = {"walk_forward": {"mean_abn_ret": wf[0], "n_filings": wf[1]}}
    alls |= {f"hold_{h}": {"mean_abn_ret": v, "n_filings": wf[1]} for h, v in zip((1, 5, 10, 20, 60), holds,
                                                                                  strict=True)}
    got, why = exits.confidence(alls)
    assert got == level and "simple holds" in why
    assert exits.confidence({}) == ("low", "no out-of-sample record yet")


def test_recommendation_trains_on_exited_trades_only(wf, conn):
    for i, d0 in enumerate((50, 450, 550, 700)):  # day 50 is more than 2 years before the calendar's end
        wf(f"C{i}", d0, crash)
    wf("OPEN", len(WF_DAYS) - 20, dip_then_soar)  # bought 20 days before the calendar ends: still open under hold 60
    market = Market(conn)
    rule, scores, n, span = exits.recommend(exits.Evaluated(market, ZERO_COSTS), exits.load_signals(market))
    assert (rule, n) == (StopTarget(stop=0.08), 3)  # the 3 recent crashes; the open trade would favor holding
    assert span.endswith(WF_DAYS[-1])


def test_run_writes_the_exit_rules_with_one_recommendation(wf, conn):
    for i, d0 in enumerate((50, 150, 300, 600, 700)):
        wf(f"C{i}", d0, crash)
    s = exits.run(conn, "all", now=lambda: "2026-03-06T22:00:00Z")
    rows = [dict(r) for r in conn.execute("SELECT * FROM exit_rules ORDER BY position")]
    assert [r["label"] for r in rows] == [exits.label(r) for r in exits.GRID]
    [rec] = [r for r in rows if r["recommended"]]
    assert rec["label"] == s.recommended == "stop_target(stop=0.08, target=None)"
    assert rec["confidence"] == s.confidence and rec["reason"] and rec["train_score"] is not None
    assert all(r["confidence"] is None for r in rows if not r["recommended"])
    assert "Recommended from now on: stop_target" in "\n".join(exits.report(conn))
