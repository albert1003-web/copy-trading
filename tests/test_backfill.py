from datetime import date

import httpx

from alerts import rules
from db import migrate
from ingest import house, senate
from pipeline import backfill
from pipeline.run import single_run

NOW = "2026-10-01T13:00:00Z"

INDEX_2025 = b"""<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member><Prefix>Hon.</Prefix><Last>Miller</Last><First>Max</First><Suffix /><FilingType>P</FilingType>
    <StateDst>OH07</StateDst><Year>2025</Year><FilingDate>3/14/2025</FilingDate><DocID>20025001</DocID></Member>
  <Member><Prefix>Hon.</Prefix><Last>Rogers</Last><First>Harold</First><Suffix /><FilingType>P</FilingType>
    <StateDst>KY05</StateDst><Year>2025</Year><FilingDate>6/2/2025</FilingDate><DocID>9025002</DocID></Member>
</FinancialDisclosure>"""


def filings(conn):
    return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM filings")}


def listing(doc_id="20035528", filing_date="2026-09-27", source="index"):
    return house.Listing(doc_id, 2026, "Hon. Max Miller", "OH07", filing_date, source)


# --- availability on ingest -----------------------------------------------------------------------


def test_live_rows_are_seen_and_backfilled_rows_are_estimated(conn):
    house.upsert(conn, [listing("20000001")], NOW)
    house.upsert(conn, [listing("20000002")], NOW, backfill=True)
    rows = filings(conn)
    assert (rows["20000001"]["available_at"], rows["20000001"]["available_basis"]) == (NOW, "seen")
    assert (rows["20000002"]["available_at"], rows["20000002"]["available_basis"]) == ("2026-09-27T21:00:00Z", "filed")


def test_a_later_poll_never_rewrites_availability(conn):
    house.upsert(conn, [listing("20000001")], NOW)
    house.upsert(conn, [listing("20000002")], NOW, backfill=True)
    house.upsert(conn, [listing("20000001"), listing("20000002")], "2026-10-02T13:00:00Z", backfill=True)
    house.upsert(conn, [listing("20000001"), listing("20000002")], "2026-10-02T13:00:00Z")
    rows = filings(conn)
    assert (rows["20000001"]["available_at"], rows["20000001"]["available_basis"]) == (NOW, "seen")
    assert (rows["20000002"]["available_at"], rows["20000002"]["available_basis"]) == ("2026-09-27T21:00:00Z", "filed")


def test_backfilled_search_row_gets_its_estimate_when_the_index_supplies_the_date(conn):
    house.upsert(conn, [listing(filing_date=None, source="search")], NOW, backfill=True)
    assert filings(conn)["20035528"]["available_at"] == NOW  # no date yet
    house.upsert(conn, [listing()], "2026-10-01T14:00:00Z", backfill=True)
    assert filings(conn)["20035528"]["available_at"] == "2026-09-27T21:00:00Z"


def test_senate_backfill_rows_are_estimated_and_bounded_by_until(conn, senate_http, efd, tmp_path):
    s = senate.run(conn, senate_http, since=date(2026, 1, 1), until=date(2026, 9, 21), backfill=True,
                   download=False, raw_root=tmp_path, now=lambda: NOW, pause=lambda: None)
    rows = filings(conn)
    assert s.new == len(rows) and all(r["filing_date"] <= "2026-09-21" for r in rows.values())
    assert "9e2ff733-aeac-4ce8-872c-3d6b7913da88" not in rows  # received 09/28, after `until`
    assert {r["available_basis"] for r in rows.values()} == {"filed"}
    assert all(r["available_at"] == r["filing_date"] + "T21:00:00Z" for r in rows.values())


# --- alerts ---------------------------------------------------------------------------------------


def test_backfilled_filings_are_never_alerted(conn):
    conn.execute("INSERT INTO members (member_id, name, chamber) VALUES ('M001', 'Max Miller', 'house')")
    conn.execute("INSERT INTO watchlist (member_id, added_at) VALUES ('M001', '2026-01-01T00:00:00Z')")
    house.upsert(conn, [listing("20000001")], NOW)
    house.upsert(conn, [listing("20000002")], NOW, backfill=True)
    conn.execute("UPDATE filings SET member_id = 'M001'")
    for doc_id in ("20000001", "20000002"):
        conn.execute("INSERT INTO trades (doc_id, member_id, line_no, ticker, symbol, action, asset_type) "
                     "VALUES (?, 'M001', 1, 'NVDA', 'NVDA', 'BUY', 'stock')", (doc_id,))
    conn.commit()
    assert [row["doc_id"] for _rule, row in rules.trades(conn, "2000-01-01T00:00:00Z")] == ["20000001"]


# --- migration ------------------------------------------------------------------------------------


def test_migration_estimates_filings_loaded_before_scheduling_started(conn):
    conn.executescript("""
        INSERT INTO pipeline_runs (started_at, status) VALUES ('2026-10-01T18:00:00Z', 'ok');
        INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at) VALUES
          ('OLD', 'house', '2026-09-28', '2026-10-01T08:00:00Z'),
          ('SAMEDAY', 'house', '2026-10-01', '2026-10-01T08:00:00Z'),
          ('LIVE', 'house', '2026-09-30', '2026-10-01T19:00:00Z');
        UPDATE filings SET available_at = NULL;
        PRAGMA user_version = 4;
    """)
    migrate(conn)
    rows = filings(conn)
    assert (rows["OLD"]["available_at"], rows["OLD"]["available_basis"]) == ("2026-09-28T21:00:00Z", "filed")
    # first seen before that day's close: the real sighting is earlier than the estimate
    assert (rows["SAMEDAY"]["available_at"], rows["SAMEDAY"]["available_basis"]) == ("2026-10-01T08:00:00Z", "seen")
    assert (rows["LIVE"]["available_at"], rows["LIVE"]["available_basis"]) == ("2026-10-01T19:00:00Z", "seen")


# --- the backfill command -------------------------------------------------------------------------


def test_backfill_loads_parses_and_reruns_add_nothing(conn, clerk, efd, tmp_path):
    clerk.past_years[2025] = INDEX_2025
    clerk.pdfs["20025001"] = clerk.pdfs["20035528"]
    clerk.pdfs["9025002"] = clerk.pdfs["9116342"]

    def clients():
        return (httpx.Client(transport=httpx.MockTransport(clerk.handler)),
                httpx.Client(transport=httpx.MockTransport(efd.handler), follow_redirects=True))

    house_http, senate_http = clients()
    results = backfill.run(conn, 2025, 2025, raw_root=tmp_path, house_http=house_http, senate_http=senate_http)

    rows = filings(conn)
    assert set(rows) == {"20025001", "9025002"}  # no Senate fixture was received in 2025
    assert results["house"]["new_from_index"] == 2 and results["senate"]["new"] == 0
    assert rows["20025001"]["available_at"] == "2025-03-14T21:00:00Z"
    # parsed (needs_review only because the reused 2026 PDF has trades dated after this 2025 filing date)
    assert rows["20025001"]["parse_status"] != "pending" and rows["20025001"]["doc_format"] == "electronic"
    assert rows["9025002"]["doc_format"] == "scanned" and rows["9025002"]["parse_status"] == "needs_review"
    trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert trades > 0

    requests = len(clerk.requests)
    house_http, senate_http = clients()
    results = backfill.run(conn, 2025, 2025, raw_root=tmp_path, house_http=house_http, senate_http=senate_http)
    assert results["house"]["new_from_index"] == 0 and results["house"]["downloaded"] == 0
    assert not any("/ptr-pdfs/" in str(r.url) for r in clerk.requests[requests:])
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == trades


def test_backfill_refuses_to_run_during_a_pipeline_run(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TRACKER_LOG_DIR", str(tmp_path / "logs"))
    with single_run(tmp_path / "pipeline.lock") as acquired:
        assert acquired
        assert backfill.main(["--from", "2020"]) == 1
