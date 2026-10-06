import json
from datetime import UTC, datetime, timedelta

import pytest

from parse import llm_fallback as vision

NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)  # 14:00 ET


def add_filing(conn, doc_id, *, chamber="house", basis="filed", filing_date="2026-09-01", raw=True, **extra):
    raw_path = (f"{chamber}/2026/{doc_id}.pdf" if chamber == "house" else f"senate/2026/{doc_id}.html") if raw else None
    conn.execute(
        "INSERT INTO filings (doc_id, member_id, chamber, filing_date, first_seen_at, available_basis, doc_format, "
        "parse_status, raw_path, filing_year) VALUES (?, 'M1', ?, ?, ?, ?, 'scanned', 'needs_review', ?, 2026)",
        (doc_id, chamber, filing_date, f"{filing_date}T15:00:00Z", basis, raw_path))
    for column, value in extra.items():
        conn.execute(f"UPDATE filings SET {column} = ? WHERE doc_id = ?", (value, doc_id))


@pytest.fixture
def raw(tmp_path):
    return tmp_path / "raw"


@pytest.fixture
def db(conn, raw):
    conn.execute("INSERT INTO members (member_id, name, chamber) VALUES ('M1', 'Max Member', 'house')")
    conn.commit()
    return conn


def touch_pdf(raw, doc_id):
    path = raw / "house" / "2026" / f"{doc_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.5 scanned")


ROWS = [
    {"owner": "joint", "asset": "Vaneck ETF TR Gold Miners ETF GDX", "ticker": "GDX", "type": "Purchase",
     "date": "01/28/26", "amount": "$1,001 - $15,000", "notes": None},
    {"owner": None, "asset": "(S) MH Four Winds LLC", "ticker": None, "type": "Sale", "date": "7/30/2026",
     "amount": "$15,001 - $50,000", "notes": None},
    {"owner": "spouse", "asset": "Alpha Teknova Inc (Stock)(TKNO)", "ticker": None, "type": "Partial Sale",
     "date": "8/3/26", "amount": "Over $1,000,000", "notes": None},
    {"owner": "self", "asset": "NVDA call options", "ticker": "NVDA", "type": "Exchange", "date": "8/5/26",
     "amount": "$50,001 - $100,000", "notes": "option: call"},
]


def reading(rows=ROWS, readable=True, is_ptr=True, problems=None):
    calls = []

    def extract(files, doc_id, conn):
        calls.append((doc_id, [f.name for f in files]))
        return {"readable": readable, "is_ptr": is_ptr, "filer_name": "Max Member", "rows": rows,
                "problems": problems}

    extract.calls = calls
    return extract


def trades(conn, doc_id):
    return [dict(r) for r in conn.execute(
        "SELECT trade_id, line_no, ticker, action, owner, tx_date, amount_min, amount_max, asset_type, confidence "
        "FROM trades WHERE doc_id = ? ORDER BY line_no", (doc_id,))]


def test_rows_are_normalized_and_marked(db, raw):
    add_filing(db, "D1", basis="seen")
    touch_pdf(raw, "D1")
    s = vision.run(db, raw_root=raw, now=lambda: NOW, extract=reading())
    assert (s.read, s.rows, s.failed) == (1, 4, 0)

    rows = trades(db, "D1")
    assert [(r["ticker"], r["action"], r["owner"], r["tx_date"]) for r in rows] == [
        ("GDX", "BUY", "joint", "2026-01-28"),
        (None, "SELL", "spouse", "2026-07-30"),  # (S) marks the owner (spouse): not SentinelOne's ticker
        ("TKNO", "SELL_PARTIAL", "spouse", "2026-08-03"),
        ("NVDA", "EXCHANGE", "self", "2026-08-05"),
    ]
    assert (rows[0]["amount_min"], rows[0]["amount_max"]) == (1001, 15000)
    assert (rows[2]["amount_min"], rows[2]["amount_max"]) == (1_000_001, None)
    assert [r["asset_type"] for r in rows] == ["stock", "other", "stock", "option"]
    assert {r["confidence"] for r in rows} == {vision.VISION_CONFIDENCE}
    filing = db.execute("SELECT parse_method, parse_status, vision_attempts FROM filings WHERE doc_id = 'D1'")
    assert tuple(filing.fetchone()) == ("vision", "parsed", 1)

    # A later re-read keeps trade_ids (same upsert as the text parsers).
    ids = [r["trade_id"] for r in rows]
    vision.read_filing(db, db.execute("SELECT * FROM filings WHERE doc_id = 'D1'").fetchone(),
                       [raw / "house/2026/D1.pdf"], now=NOW, extract=reading())
    assert [r["trade_id"] for r in trades(db, "D1")] == ids


def test_a_row_with_problems_lowers_confidence_and_needs_review(db, raw):
    add_filing(db, "D1", basis="seen")
    touch_pdf(raw, "D1")
    bad = [{**ROWS[0], "date": "13/45/26"}]  # unreadable date
    vision.run(db, raw_root=raw, now=lambda: NOW, extract=reading(bad))
    assert trades(db, "D1")[0]["confidence"] == 0.5
    assert db.execute("SELECT parse_status FROM filings WHERE doc_id = 'D1'").fetchone()[0] == "needs_review"


@pytest.mark.parametrize("kwargs", [{"readable": False}, {"is_ptr": False}, {"rows": []}])
def test_unusable_reads_write_nothing_and_count_an_attempt(db, raw, kwargs):
    add_filing(db, "D1", basis="seen")
    touch_pdf(raw, "D1")
    s = vision.run(db, raw_root=raw, now=lambda: NOW, extract=reading(**kwargs))
    assert s.unreadable == 1 and trades(db, "D1") == []
    row = db.execute("SELECT parse_method, vision_attempts, vision_error FROM filings WHERE doc_id = 'D1'").fetchone()
    assert row[0] is None and row[1] == 1 and row[2]


def test_failed_reads_retry_after_a_day_and_stop_after_three(db, raw):
    add_filing(db, "D1", basis="seen")
    touch_pdf(raw, "D1")

    def broken(files, doc_id, conn):
        raise RuntimeError("Claude Code timed out")

    moment = NOW
    for _ in range(3):
        assert vision.run(db, raw_root=raw, now=lambda m=moment: m, extract=broken).failed == 1
        assert vision.run(db, raw_root=raw, now=lambda m=moment: m, extract=broken).failed == 0  # not again today
        moment += timedelta(days=1, minutes=1)
    assert vision.run(db, raw_root=raw, now=lambda: moment, extract=broken).failed == 0  # gave up after 3


def test_live_filings_first_then_a_capped_backlog(db, raw):
    for i in range(7):
        add_filing(db, f"L{i}", basis="seen", filing_date=f"2026-09-{10 + i}")
    for i in range(30):
        add_filing(db, f"B{i:02d}", filing_date=(datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat())
    for doc in [f"L{i}" for i in range(7)] + [f"B{i:02d}" for i in range(30)]:
        touch_pdf(raw, doc)

    extract = reading()
    vision.run(db, raw_root=raw, now=lambda: NOW, extract=extract)
    done = [d for d, _ in extract.calls]
    assert len(done) == vision.LIVE_PER_PASS + vision.BACKLOG_PER_PASS
    assert done[:5] == ["L6", "L5", "L4", "L3", "L2"]  # newest live first
    assert done[5:] == ["B29", "B28", "B27"]  # backlog newest first

    for _ in range(10):  # the backlog stops at BACKLOG_PER_DAY for the (ET) day
        vision.run(db, raw_root=raw, now=lambda: NOW, extract=extract)
    backlog = [d for d, _ in extract.calls if d.startswith("B")]
    assert len(backlog) == vision.BACKLOG_PER_DAY
    tomorrow = NOW + timedelta(days=1)
    vision.run(db, raw_root=raw, now=lambda: tomorrow, extract=extract)
    assert len([d for d, _ in extract.calls if d.startswith("B")]) == vision.BACKLOG_PER_DAY + 3


def test_senate_pages_need_a_complete_set(db, raw):
    add_filing(db, "S1", chamber="senate", basis="seen")
    folder = raw / "senate" / "2026" / "S1"
    folder.mkdir(parents=True)
    (folder / "001.gif").write_bytes(b"GIF89a")
    extract = reading()
    assert vision.run(db, raw_root=raw, now=lambda: NOW, extract=extract).skipped_no_files == 1  # no manifest yet

    (folder / "002.gif").write_bytes(b"GIF89a")
    (folder / "pages.json").write_text(json.dumps(["001.gif", "002.gif"]))
    vision.run(db, raw_root=raw, now=lambda: NOW, extract=extract)
    assert extract.calls == [("S1", ["001.gif", "002.gif"])]


def test_vision_trades_stay_out_of_outcomes(db, raw):
    from analytics import outcomes

    add_filing(db, "TXT", basis="seen", parse_method="text")
    add_filing(db, "VIS", basis="seen", parse_method="vision")
    for doc in ("TXT", "VIS"):
        db.execute("INSERT INTO trades (doc_id, member_id, line_no, symbol, ticker_status, action) "
                   "VALUES (?, 'M1', 1, 'NVDA', 'listed', 'BUY')", (doc,))
    in_scope = {r["trade_id"] for r in outcomes.in_scope(db)}
    vis = db.execute("SELECT trade_id FROM trades WHERE doc_id = 'VIS'").fetchone()[0]
    assert len(in_scope) == 1 and vis not in in_scope


def test_alert_emails_flag_vision_trades():
    from alerts import email

    base = {"ticker_status": "listed", "ticker": "NVDA", "confidence": 0.7}
    assert email._flags({**base, "parse_method": "vision"}) == [
        "read by Claude from a scanned filing; check the filing"]
    assert email._flags({**base, "parse_method": "text", "confidence": 0.5}) == [
        "low-confidence parse; check the filing"]
