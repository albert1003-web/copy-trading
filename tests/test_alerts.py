import smtplib

import pytest

from alerts import email, score
from alerts import run as alerts_run

NOW = "2026-10-01T15:00:00Z"


def trade(**overrides):
    row = {"asset_type": "stock", "asset_name": "Apple Inc. (AAPL)", "description": None, "amount_min": 1001,
           "filing_delay_days": 20, "ticker_status": "listed", "is_etf": 0}
    row.update(overrides)
    return row


# --- score ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("overrides, expected", [
    ({}, 70),  # buy 50 + amount 0 + delay(<=30) 5 + listed stock 15
    ({"amount_min": 15001}, 75),
    ({"amount_min": 1_000_001, "filing_delay_days": 3}, 100),
    ({"filing_delay_days": 46}, 60),
    ({"filing_delay_days": None}, 65),
    ({"is_etf": 1}, 60),
    ({"ticker_status": "unlisted"}, 45),
    ({"ticker_status": "renamed"}, 70),
    ({"asset_type": "option", "description": "Purchased 20 call options"}, 60),
])
def test_score(overrides, expected):
    points, reasons = score.score_v1(trade(**overrides))
    assert points == expected
    assert len(reasons) == 4
    assert score.score(trade(**overrides)) == (points, reasons + ["v1 rules: no outcome history yet"])


@pytest.mark.parametrize("overrides, expected", [
    ({"asset_type": "option", "asset_name": "Williams Companies Option Type: Call Strike price: $75.00"}, True),
    ({"asset_type": "option", "description": "Purchased 20 call options with a strike price of $150"}, True),
    ({"asset_type": "option", "description": "Put Option"}, False),
    ({"asset_type": "option", "description": None}, False),
    ({"asset_type": "stock", "description": "Exercised 50 call options"}, False),
])
def test_is_call(overrides, expected):
    assert score.is_call(trade(**overrides)) is expected


def test_amount_text():
    assert email.amount(1001, 15000) == "$1,001–$15,000"
    assert email.amount(1_000_001, None) == "over $1,000,000"
    assert email.amount(15, 15) == "$15"
    assert email.amount(None, None) == "amount unknown"


# --- run ------------------------------------------------------------------------------------------


class Outbox:
    def __init__(self):
        self.sent: list[email.Message] = []
        self.fail = False

    def __call__(self, message):
        if self.fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad app password")
        self.sent.append(message)


@pytest.fixture
def outbox():
    return Outbox()


def add_trade(conn, doc_id, line_no, symbol, action="BUY", **extra):
    values = {"asset_type": "stock", "asset_name": f"{symbol} Inc. ({symbol})", "description": None,
              "owner": "spouse", "tx_date": "2026-09-10", "disclosure_date": "2026-09-28", "amount_min": 15001,
              "amount_max": 50000, "filing_delay_days": 18, "ticker_status": "listed", "is_etf": 0,
              "confidence": 1.0, **extra}
    conn.execute(
        """INSERT INTO trades (doc_id, member_id, line_no, ticker, symbol, action, asset_type, asset_name,
                               description, owner, tx_date, disclosure_date, amount_min, amount_max,
                               filing_delay_days, ticker_status, is_etf, confidence)
           VALUES (?, (SELECT member_id FROM filings WHERE doc_id = ?),
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (doc_id, doc_id, line_no, symbol, symbol, action, values["asset_type"], values["asset_name"],
         values["description"], values["owner"], values["tx_date"], values["disclosure_date"], values["amount_min"],
         values["amount_max"], values["filing_delay_days"], values["ticker_status"], values["is_etf"],
         values["confidence"]),
    )


@pytest.fixture
def db(conn):
    conn.executescript("""
        INSERT INTO members (member_id, name, chamber, party, state) VALUES
          ('P000197', 'Nancy Pelosi', 'house', 'D', 'CA'),
          ('S001217', 'Rick Scott', 'senate', 'R', 'FL');
        INSERT INTO watchlist (member_id, added_at) VALUES ('P000197', '2026-09-01T00:00:00Z');
        INSERT INTO filings (doc_id, member_id, chamber, filing_date, source_url, first_seen_at, doc_format) VALUES
          ('NEW', 'P000197', 'house', '2026-09-28', 'https://ex.com/new.pdf', '2026-10-01T16:00:00Z', 'electronic'),
          ('OLD', 'P000197', 'house', '2026-09-01', 'https://ex.com/old.pdf', '2026-09-30T12:00:00Z', 'electronic'),
          ('UNW', 'S001217', 'senate', '2026-09-28', 'https://ex.com/unw', '2026-10-01T16:00:00Z', 'electronic'),
          ('SCAN', 'P000197', 'house', '2026-09-29', 'https://ex.com/scan.pdf', '2026-10-01T16:30:00Z', 'scanned');
    """)
    add_trade(conn, "NEW", 1, "NVDA")
    add_trade(conn, "NEW", 2, "GOOGL", asset_type="option", description="Purchased 20 call options")
    add_trade(conn, "NEW", 3, "AMZN", asset_type="option", description="Put Option")
    add_trade(conn, "NEW", 4, "AAPL", action="SELL")  # we hold AAPL
    add_trade(conn, "NEW", 5, "MSFT", action="SELL_PARTIAL")  # we don't hold MSFT
    add_trade(conn, "NEW", 6, None, asset_name="Some Fund LP", ticker_status="none")
    add_trade(conn, "OLD", 1, "TSLA")  # first seen before alerts started
    add_trade(conn, "UNW", 1, "LMT")  # not on the watchlist
    conn.execute("INSERT INTO my_positions (ticker, buy_date, buy_price, shares) VALUES ('aapl', '2026-09-02', 1, 1)")
    conn.execute("INSERT INTO source_state (source, checked_at) VALUES ('alerts.start', '2026-10-01T00:00:00Z')")
    conn.commit()
    return conn


def alerted(conn):
    return conn.execute(
        "SELECT t.symbol, a.rule, a.score FROM alerts a JOIN trades t USING (trade_id) ORDER BY t.line_no"
    ).fetchall()


def test_sends_one_email_per_filing_for_qualifying_trades(db, outbox):
    s = alerts_run.run(db, outbox, now=lambda: NOW)

    assert (s.emails, s.trades, s.scans, s.failures) == (2, 3, 1, 0)
    trades_email, scan_email = outbox.sent
    assert trades_email.subject == "[Trade Tracker] Nancy Pelosi bought NVDA, GOOGL · score 75; sold AAPL (you hold)"
    assert "BOUGHT GOOGL CALL OPTIONS" in trades_email.text and "AMZN" not in trades_email.text
    assert "https://ex.com/new.pdf" in trades_email.text and "<table" in trades_email.html
    assert "scanned PTR" in scan_email.subject and "https://ex.com/scan.pdf" in scan_email.text
    assert [tuple(r) for r in alerted(db)] == [("NVDA", "watchlist_buy", 75), ("GOOGL", "watchlist_buy", 65),
                                               ("AAPL", "held_sale", None)]
    assert [tuple(r) for r in db.execute("SELECT doc_id, kind FROM filing_alerts")] == [("SCAN", "scanned")]


def test_second_run_sends_nothing(db, outbox):
    alerts_run.run(db, outbox, now=lambda: NOW)
    s = alerts_run.run(db, outbox, now=lambda: NOW)
    assert s.emails == 0 and len(outbox.sent) == 2


def test_first_run_sets_the_start_time_so_the_backlog_is_skipped(db, outbox):
    db.execute("DELETE FROM source_state")
    s = alerts_run.run(db, outbox, now=lambda: "2026-10-02T00:00:00Z")
    assert s.emails == 0 and s.since == "2026-10-02T00:00:00Z"
    assert db.execute("SELECT checked_at FROM source_state WHERE source = 'alerts.start'").fetchone()[0] == s.since


def test_since_reaches_back(db, outbox):
    s = alerts_run.run(db, outbox, since="2026-09-30", now=lambda: NOW)
    assert s.trades == 4 and "TSLA" in outbox.sent[0].subject


def test_failed_send_records_nothing_and_retries(db, outbox):
    outbox.fail = True
    s = alerts_run.run(db, outbox, now=lambda: NOW)
    assert (s.emails, s.failures) == (0, 2)
    assert alerted(db) == [] and db.execute("SELECT COUNT(*) FROM filing_alerts").fetchone()[0] == 0

    outbox.fail = False
    assert alerts_run.run(db, outbox, now=lambda: NOW).emails == 2


def test_dry_run_prints_and_writes_nothing(db, outbox):
    db.execute("DELETE FROM source_state")
    printed = []
    s = alerts_run.run(db, None, since="2026-09-30", dry_run=True, now=lambda: NOW, out=printed.append)
    assert s.emails == 3 and len(printed) == 3 and outbox.sent == []
    assert alerted(db) == [] and db.execute("SELECT COUNT(*) FROM source_state").fetchone()[0] == 0


def test_unwatched_members_never_alert(db, outbox):
    db.execute("UPDATE watchlist SET active = 0")
    assert alerts_run.run(db, outbox, now=lambda: NOW).emails == 0


def test_missing_gmail_settings(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(email.ConfigError):
        email.gmail_sender()


def test_settings_default_recipient_and_strip_password_spaces(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.delenv("ALERT_RECIPIENT", raising=False)
    assert email.settings() == ("me@example.com", "abcdefghijklmnop", "me@example.com")


# --- v2 score and entry -----------------------------------------------------------------------------


def model(**overrides):
    m = score.Model(
        pooled=0.002,
        members={"P000197": (0.013, 27)},
        factors={("amount", "$15k-50k"): (0.002, 591), ("kind", "stock"): (0.0005, 1863),
                 ("kind", "call"): (-0.001, 64)},
        delays={("all", "all"): (0, 0.0, 1992), ("mcap", "mega"): (2, 0.003, 440),
                ("member", "P000197"): (None, None, 15)},
    )
    for key, value in overrides.items():
        setattr(m, key, value)
    return m


def v2_trade(**overrides):
    return trade(**{"member_id": "P000197", "mcap_bucket": None, "committee_relevant": 0, "amount_min": 15001,
                    **overrides})


def test_v2_score_adds_member_and_feature_effects():
    points, reasons = score.score(v2_trade(), model())
    # member 1.3% + amount 0.2% + stock 0.05% = 1.55% expected -> 50 + 15.5
    assert points == 66
    assert reasons[0] == "expected 20-day excess vs S&P 500 +1.55%"
    assert "member +1.30% (27 filings)" in reasons
    assert "amount $15k-50k +0.20%" in reasons
    assert not any(r.startswith("size") for r in reasons)  # no effect for that level: not listed


def test_v2_score_for_an_unknown_member_starts_from_the_pooled_mean():
    points, reasons = score.score(v2_trade(member_id="NEW1", asset_type="option"), model())
    # pooled 0.2% + amount 0.2% + call -0.1% = 0.3%
    assert points == 53
    assert reasons[1] == "member: no history, using the average +0.20%"


def test_v2_score_ignores_a_member_record_below_the_minimum_sample():
    points, reasons = score.score(v2_trade(), model(members={"P000197": (0.08, 5)}))
    assert points == 54  # pooled 0.2% + amount 0.2% + stock 0.05%, not +8%
    assert reasons[1] == "member: only 5 filings, using the average +0.20%"


def test_v2_score_is_clamped():
    assert score.score(v2_trade(), model(members={"P000197": (0.2, 30)}))[0] == 100
    assert score.score(v2_trade(), model(members={"P000197": (-0.2, 30)}))[0] == 0


def test_suggested_entry_falls_back_from_member_to_size_to_all():
    m = model()  # the member has too few filings for a best delay
    assert score.suggested_entry(v2_trade(mcap_bucket="mega"), m) == (
        "Consider waiting 2 trading days after D0: opens averaged 0.30% lower (mega-cap buys, 440 filings)")
    assert score.suggested_entry(v2_trade(), m) == (
        "Buy at the next open (D0); waiting hasn't paid off (all buys, 1992 filings)")
    m.delays[("member", "P000197")] = (1, 0.002, 30)
    assert score.suggested_entry(v2_trade(), m).startswith("Consider waiting 1 trading day after D0")
    assert score.suggested_entry(v2_trade(), None) is None


def test_alerts_use_v2_and_record_the_entry_once_analytics_exist(db, outbox):
    db.executescript("""
        INSERT INTO signal_factors (factor, level, n, n_trades, mean, shrunk, effect, computed_at) VALUES
          ('all', 'all', 1976, 15575, 0.002, 0.002, 0, 'x'), ('kind', 'stock', 1863, 13785, 0.003, 0.003, 0.001, 'x');
        INSERT INTO member_horizon_stats (member_id, as_of, horizon, n_filings, n_trades, shrunk_score) VALUES
          ('P000197', '2026-10-01', 20, 27, 85, 0.013);
        INSERT INTO entry_delays (group_type, group_key, n, n_trades, best_k, gain, computed_at) VALUES
          ('all', 'all', 1992, 15709, 0, 0, 'x');
    """)
    alerts_run.run(db, outbox, now=lambda: NOW)
    rows = dict(db.execute("SELECT t.symbol, a.score FROM alerts a JOIN trades t USING (trade_id) "
                           "WHERE a.rule = 'watchlist_buy'").fetchall())
    assert rows["NVDA"] == 64  # member 1.3% + stock 0.1% = 1.4%
    entries = {r[0] for r in db.execute("SELECT suggested_entry FROM alerts WHERE rule = 'watchlist_buy'")}
    assert entries == {"Buy at the next open (D0); waiting hasn't paid off (all buys, 1992 filings)"}
    assert db.execute("SELECT suggested_entry FROM alerts WHERE rule = 'held_sale'").fetchone()[0] is None
    body = outbox.sent[0].text
    assert "expected 20-day excess vs S&P 500 +1.40%" in body
    assert "Entry: Buy at the next open (D0)" in body


def test_buy_emails_carry_a_passive_exit_line_once_exit_rules_exist(db, outbox):
    alerts_run.run(db, outbox, now=lambda: NOW)
    assert "If you buy" not in outbox.sent[0].text  # no exit_rules yet: no line
    assert db.execute("SELECT COUNT(*) FROM alerts WHERE suggested_exit IS NOT NULL").fetchone()[0] == 0

    db.execute("DELETE FROM alerts")
    db.executescript("""
        INSERT INTO exit_rules (label, description, position, recommended, confidence, reason, computed_at) VALUES
          ('fixed_hold(days=5)', 'Hold 5 trading days, then sell at the close', 0, 1, 'low', 'beat 2 of 5 holds', 'x'),
          ('fixed_hold(days=20)', 'Hold 20 trading days, then sell at the close', 1, 0, NULL, NULL, 'x');
    """)
    outbox.sent.clear()
    alerts_run.run(db, outbox, now=lambda: NOW)
    line = ("If you buy: suggested exit is to hold 5 trading days, then sell at the close "
            "(confidence low: beat 2 of 5 holds)")
    assert outbox.sent[0].text.count(line) == 1  # once per email, not per buy
    exits = dict(db.execute("SELECT rule, suggested_exit FROM alerts GROUP BY rule").fetchall())
    assert exits == {"watchlist_buy": line, "held_sale": None}  # sales of what you hold get no suggestion
