import smtplib
from datetime import date, timedelta

import pytest

from alerts import email, positions
from alerts import run as alerts_run
from common.exit_rules import FixedHold, MemberSale, TrailingStop, describe, label

START = date(2026, 9, 1)  # a Tuesday
NOW = "2026-10-01T23:00:00Z"


def weekdays(n: int, start: date = START) -> list[str]:
    days, day = [], start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


DAYS = weekdays(20)


def bars(conn, symbol, rows, start=0):
    """rows: (open, high, low, close) from DAYS[start] on."""
    conn.executemany(  # adj_close differs from close: the watcher must use the raw (dividend-unadjusted) close
        "INSERT INTO prices (ticker, date, open, high, low, close, adj_close) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(symbol, DAYS[start + i], *r, r[3] * 0.97) for i, r in enumerate(rows)])


def flat(n, price=100.0):
    return [(price, price, price, price)] * n


def position(conn, ticker, buy_day, price, rule, *, trade_id=None, status="open"):
    cur = conn.execute("INSERT INTO my_positions (trade_id, ticker, buy_date, buy_price, shares, exit_rule, status) "
                       "VALUES (?, ?, ?, ?, 10, ?, ?)", (trade_id, ticker, DAYS[buy_day], price, rule, status))
    conn.commit()
    return cur.lastrowid


@pytest.fixture
def db(conn):
    bars(conn, "SPY", flat(len(DAYS), 400))
    conn.execute("INSERT INTO source_state (source, checked_at) VALUES ('alerts.start', '2026-09-01T00:00:00Z')")
    conn.commit()
    return conn


def test_trailing_stop_fires_on_the_right_day_and_ignores_the_buy_days_low(db):
    bars(db, "X", [
        (100, 100, 80, 100),  # buy day: the low may have come before the fill, so it doesn't count
        (100, 110, 101, 108),  # high 110: stop moves to 99 after today's check
        (108, 109, 98, 99),  # low 98 <= 99
    ] + flat(17, 99))
    pid = position(db, "x", 0, 100.0, label(TrailingStop(0.10)))
    [alert], warnings = positions.due(db)
    assert warnings == []
    assert (alert.position["position_id"], alert.reason, alert.day, alert.price) == (pid, "stop", DAYS[2],
                                                                                     pytest.approx(99))


def test_fixed_hold_fires_after_n_closes_and_not_before(db):
    db.execute("DELETE FROM prices WHERE ticker = 'SPY' AND date > ?", (DAYS[3],))  # the market's last bar is day 3
    bars(db, "Y", flat(4))  # so day 5 hasn't closed yet
    position(db, "Y", 0, 100.0, label(FixedHold(5)))
    assert positions.due(db) == ([], [])
    bars(db, "SPY", flat(3, 400), start=4)
    bars(db, "Y", flat(3), start=4)
    [alert], _ = positions.due(db)
    assert (alert.reason, alert.day, alert.price) == ("hold", DAYS[5], 100)


def test_unwatched_closed_and_free_text_positions_are_ignored(db):
    bars(db, "Z", [(100, 100, 50, 50)] * len(DAYS))
    position(db, "Z", 0, 100.0, None)
    position(db, "Z", 0, 100.0, "sell when it feels right")  # older free text: not a rule label
    position(db, "Z", 0, 100.0, label(TrailingStop(0.10)), status="closed")
    assert positions.due(db) == ([], [])


def test_a_split_since_the_buy_is_skipped_with_a_warning(db):
    bars(db, "S", flat(len(DAYS), 100))
    position(db, "S", 0, 400.0, label(TrailingStop(0.10)))  # bought pre-split at 400; history now shows 100
    alerts, [warning] = positions.due(db)
    assert alerts == [] and "split" in warning


def test_prices_that_stop_alert_once_but_a_late_bar_does_not(db):
    bars(db, "GONE", flat(len(DAYS) - 6))  # 6 trading days behind SPY: stopped
    bars(db, "LATE", flat(len(DAYS) - 1))  # one day behind: just late
    position(db, "GONE", 0, 100.0, label(TrailingStop(0.10)))
    position(db, "LATE", 0, 100.0, label(TrailingStop(0.10)))
    [alert], _ = positions.due(db)
    assert (alert.position["ticker"], alert.reason) == ("GONE", "data_end")


def sale_setup(conn):
    conn.executescript("""
        INSERT INTO members (member_id, name, chamber) VALUES ('A', 'Member A', 'house'), ('B', 'Member B', 'house');
        INSERT INTO filings (doc_id, member_id, chamber, filing_date, first_seen_at, available_at, source_url) VALUES
          ('BUY', 'A', 'house', '2026-09-01', '2026-09-01T12:00:00Z', '2026-09-01T12:00:00Z', 'https://ex.com/buy'),
          ('OLDSALE', 'A', 'house', '2026-08-20', '2026-08-20T12:00:00Z', '2026-08-20T12:00:00Z', 'https://ex.com/o'),
          ('BSALE', 'B', 'house', '2026-09-08', '2026-09-08T12:00:00Z', '2026-09-08T12:00:00Z', 'https://ex.com/b');
        INSERT INTO trades (trade_id, doc_id, line_no, member_id, symbol, action) VALUES
          (1, 'BUY', 1, 'A', 'M', 'BUY'),
          (2, 'OLDSALE', 1, 'A', 'M', 'SELL'),
          (3, 'BSALE', 1, 'B', 'M', 'SELL');
    """)
    bars(conn, "M", flat(len(DAYS)))


def test_member_sale_fires_when_the_source_members_sale_is_available(db):
    sale_setup(db)
    position(db, "M", 1, 100.0, label(MemberSale()), trade_id=1)
    assert positions.due(db) == ([], [])  # A's old sale predates the buy; B isn't the member we copied
    db.execute("INSERT INTO filings (doc_id, member_id, chamber, filing_date, first_seen_at, available_at, source_url) "
               "VALUES ('SALE', 'A', 'house', '2026-09-10', '2026-09-10T15:00:00Z', '2026-09-10T15:00:00Z', "
               "'https://ex.com/sale')")
    db.execute("INSERT INTO trades (trade_id, doc_id, line_no, member_id, symbol, action) "
               "VALUES (4, 'SALE', 1, 'A', 'M', 'SELL_PARTIAL')")
    [alert], _ = positions.due(db)
    assert (alert.reason, alert.day, alert.sale["source_url"]) == ("sale", "2026-09-10", "https://ex.com/sale")
    assert "Member A disclosed a sale of M" in email.compose_exit(alert).text


def test_member_sale_without_a_source_trade_keeps_only_the_hold_limit(db):
    sale_setup(db)
    position(db, "M", 0, 100.0, label(MemberSale(max_hold=3)))
    [alert], _ = positions.due(db)
    assert (alert.reason, alert.day) == ("hold", DAYS[3])
    assert "No source trade" in email.compose_exit(alert).text


class Outbox:
    def __init__(self):
        self.sent, self.fail = [], False

    def __call__(self, message):
        if self.fail:
            raise smtplib.SMTPServerDisconnected("down")
        self.sent.append(message)


def test_one_email_per_position_recorded_after_sending(db):
    bars(db, "X", [(100, 100, 100, 100), (100, 100, 85, 88)] + flat(18, 88))
    pid = position(db, "X", 0, 100.0, label(TrailingStop(0.10)))
    outbox = Outbox()
    outbox.fail = True
    s = alerts_run.run(db, outbox, now=lambda: NOW)
    assert (s.exits, s.failures) == (0, 1)
    assert db.execute("SELECT COUNT(*) FROM exit_alerts").fetchone()[0] == 0  # retried next run

    outbox.fail = False
    s = alerts_run.run(db, outbox, now=lambda: NOW)
    assert s.exits == 1
    [message] = outbox.sent
    assert message.subject == "[Trade Tracker] Exit X: stop hit"
    assert describe(TrailingStop(0.10)) in message.text and "T+1" in message.text and "-10.0%" in message.text
    row = dict(db.execute("SELECT * FROM exit_alerts").fetchone())
    assert (row["position_id"], row["reason"], row["triggered_on"], row["sent_at"]) == (pid, "stop", DAYS[1], NOW)

    assert alerts_run.run(db, outbox, now=lambda: NOW).exits == 0  # never again for this position
    assert len(outbox.sent) == 1


def test_dry_run_prints_the_exit_email_and_records_nothing(db):
    bars(db, "X", [(100, 100, 100, 100), (100, 100, 85, 88)] + flat(18, 88))
    position(db, "X", 0, 100.0, label(TrailingStop(0.10)))
    printed = []
    s = alerts_run.run(db, None, dry_run=True, now=lambda: NOW, out=printed.append)
    assert s.exits == 1 and "Exit X: stop hit" in printed[0]
    assert db.execute("SELECT COUNT(*) FROM exit_alerts").fetchone()[0] == 0
