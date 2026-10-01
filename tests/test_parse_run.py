import shutil
from pathlib import Path

import pytest

from parse import run as parse_run

FIXTURES = Path(__file__).parent / "fixtures"

HOUSE_PDF = "electronic_20034694.pdf"  # 6 rows
SENATE_BIG = "ptr_9e2ff733-aeac-4ce8-872c-3d6b7913da88.html"  # 33 rows
SENATE_ONE = "ptr_028aef33-dc0d-44a1-992f-aa35ee42685b.html"  # 1 row


@pytest.fixture
def raw(tmp_path):
    root = tmp_path / "raw"
    for chamber, name in [("house", HOUSE_PDF), ("house", "scanned_9116342.pdf"),
                          ("senate", SENATE_BIG), ("senate", SENATE_ONE)]:
        (root / chamber).mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / chamber / name, root / chamber / name)
    return root


def add_filing(conn, doc_id, chamber, raw_path, *, fmt="electronic", status="pending"):
    conn.execute(
        """INSERT INTO filings (doc_id, chamber, filing_date, raw_path, first_seen_at, parse_status, doc_format)
           VALUES (?, ?, '2026-09-28', ?, '2026-09-28T13:00:00Z', ?, ?)""",
        (doc_id, chamber, raw_path, status, fmt),
    )
    conn.commit()


def status(conn, doc_id):
    return conn.execute("SELECT parse_status FROM filings WHERE doc_id = ?", (doc_id,)).fetchone()[0]


def trade_ids(conn, doc_id):
    return conn.execute("SELECT line_no, trade_id FROM trades WHERE doc_id = ? ORDER BY line_no", (doc_id,)).fetchall()


def test_parses_pending_filings_and_skips_the_rest(conn, raw):
    add_filing(conn, "H1", "house", f"house/{HOUSE_PDF}")
    add_filing(conn, "S1", "senate", f"senate/{SENATE_BIG}")
    add_filing(conn, "SCAN", "house", "house/scanned_9116342.pdf", fmt="scanned", status="needs_review")
    add_filing(conn, "NOFILE", "house", None)

    s = parse_run.run(conn, raw_root=raw)

    assert (s.parsed, s.needs_review, s.failed, s.rows, s.scanned_waiting) == (2, 0, 0, 39, 1)
    assert status(conn, "H1") == status(conn, "S1") == "parsed"
    assert status(conn, "SCAN") == "needs_review"
    assert status(conn, "NOFILE") == "pending"
    row = conn.execute("SELECT * FROM trades WHERE doc_id = 'H1' AND line_no = 2").fetchone()
    assert (row["ticker"], row["asset_type"], row["action"], row["owner"], row["tx_date"], row["disclosure_date"],
            row["amount_min"], row["amount_max"], row["confidence"]) == (
        "GE", "option", "SELL", "spouse", "2026-06-16", "2026-09-28", 1001, 15000, 1.0)


def test_rerun_is_a_no_op_and_reparse_keeps_trade_ids(conn, raw):
    add_filing(conn, "S1", "senate", f"senate/{SENATE_BIG}")
    parse_run.run(conn, raw_root=raw)
    before = trade_ids(conn, "S1")

    assert parse_run.run(conn, raw_root=raw).parsed == 0
    s = parse_run.run(conn, raw_root=raw, reparse=True)
    assert (s.parsed, s.rows) == (1, 33)
    assert trade_ids(conn, "S1") == before
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 33


def test_doc_id_and_chamber_filters(conn, raw):
    add_filing(conn, "H1", "house", f"house/{HOUSE_PDF}")
    add_filing(conn, "S1", "senate", f"senate/{SENATE_ONE}")
    assert parse_run.run(conn, raw_root=raw, chamber="senate").parsed == 1
    assert status(conn, "H1") == "pending"
    assert parse_run.run(conn, raw_root=raw, doc_ids=["H1"]).parsed == 1


def test_bad_files_fail_without_stopping_the_run(conn, raw):
    (raw / "house" / "corrupt.pdf").write_bytes(b"%PDF-1.4 not really")
    add_filing(conn, "BAD", "house", "house/corrupt.pdf")
    add_filing(conn, "GONE", "senate", "senate/missing.html")
    add_filing(conn, "OK", "senate", f"senate/{SENATE_ONE}")

    s = parse_run.run(conn, raw_root=raw)

    assert (s.parsed, s.failed) == (1, 2)
    assert status(conn, "BAD") == status(conn, "GONE") == "failed"
    assert status(conn, "OK") == "parsed"


def test_unrecognized_values_and_empty_reports_need_review(conn, raw):
    html = (raw / "senate" / SENATE_ONE).read_text().replace("Purchase", "Gift")
    (raw / "senate" / "odd.html").write_text(html)
    (raw / "senate" / "empty.html").write_text("<html><body>No transactions</body></html>")
    add_filing(conn, "ODD", "senate", "senate/odd.html")
    add_filing(conn, "EMPTY", "senate", "senate/empty.html")

    s = parse_run.run(conn, raw_root=raw)

    assert s.needs_review == 2
    assert status(conn, "ODD") == status(conn, "EMPTY") == "needs_review"
    action, confidence = conn.execute("SELECT action, confidence FROM trades WHERE doc_id = 'ODD'").fetchone()
    assert action is None and confidence < 1.0


def test_trade_dated_after_the_filing_needs_review(conn, raw):
    add_filing(conn, "S1", "senate", f"senate/{SENATE_ONE}")  # traded 2026-09-01
    conn.execute("UPDATE filings SET filing_date = '2026-08-01' WHERE doc_id = 'S1'")
    conn.commit()
    assert parse_run.run(conn, raw_root=raw).needs_review == 1
    assert conn.execute("SELECT confidence FROM trades WHERE doc_id = 'S1'").fetchone()[0] < 1.0


def test_reparse_with_fewer_rows_drops_extras_unless_referenced(conn, raw):
    add_filing(conn, "S1", "senate", f"senate/{SENATE_BIG}")
    parse_run.run(conn, raw_root=raw)
    kept_id = dict(trade_ids(conn, "S1"))[5]
    conn.execute(
        "INSERT INTO my_positions (trade_id, ticker, buy_date, buy_price, shares) VALUES (?, 'X', '2026-09-29', 1, 1)",
        (kept_id,),
    )
    conn.commit()

    shutil.copy(raw / "senate" / SENATE_ONE, raw / "senate" / SENATE_BIG)  # the filing now has one row
    parse_run.run(conn, raw_root=raw, doc_ids=["S1"])

    assert [line for line, _ in trade_ids(conn, "S1")] == [1, 5]
