from datetime import date

import pytest

from ingest import house


def stamps(*values):
    it = iter(values)
    return lambda: next(it)


def filings(conn):
    return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM filings")}


def run(conn, http, tmp_path, now="2026-10-01T13:00:00Z", **kwargs):
    return house.run(conn, http, [2026], raw_root=tmp_path / "raw", now=lambda: now, pause=lambda: None, **kwargs)


# --- parsing ------------------------------------------------------------------------------------


def test_parse_index_keeps_ptrs_only(clerk):
    listings = house.parse_index(clerk.index_xml)
    assert {item.doc_id for item in listings} == {"20035528", "20035491", "9116342"}
    rogers = next(item for item in listings if item.doc_id == "9116342")
    assert rogers.filer_name == "Hon. Harold Dallas Rogers"
    assert rogers.state_district == "KY05"
    assert rogers.filing_date == "2026-09-23"
    assert rogers.year == 2026
    assert rogers.source == "index"


def test_parse_search_keeps_ptr_rows_and_normalizes_names(clerk):
    listings = {item.doc_id: item for item in house.parse_search(clerk.search_html)}
    assert set(listings) == {"20035528", "20035491", "9116342", "20099999"}
    assert listings["20099999"].filer_name == "Hon. Jane Doe"
    assert listings["20099999"].state_district == "ZZ01"
    assert listings["20035528"].filing_date is None


def test_format_detection():
    assert house.guess_format("20035528") == "electronic"
    assert house.guess_format("9116342") == "scanned"
    assert house.guess_format("8221360") == "scanned"


def test_default_years():
    assert house.default_years(date(2026, 10, 1)) == [2026]
    assert house.default_years(date(2027, 1, 10)) == [2026, 2027]


# --- a full pass --------------------------------------------------------------------------------


def test_first_run_records_and_downloads_every_ptr(conn, http, clerk, tmp_path):
    summary = run(conn, http, tmp_path)

    rows = filings(conn)
    assert set(rows) == {"20035528", "20035491", "9116342", "20099999"}
    assert summary.index_status == "changed"
    assert (summary.new_from_index, summary.new_from_search) == (3, 1)
    assert (summary.downloaded, summary.download_failures) == (4, 0)
    assert summary.failed_sources == []

    rogers = rows["9116342"]
    assert rogers["chamber"] == "house"
    assert rogers["filing_date"] == "2026-09-23"
    assert rogers["filer_name"] == "Hon. Harold Dallas Rogers"
    assert rogers["source_url"] == "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/9116342.pdf"
    assert rogers["doc_format"] == "scanned"
    assert rogers["parse_status"] == "needs_review"
    assert rogers["raw_path"] == "house/2026/9116342.pdf"
    assert (tmp_path / "raw/house/2026/9116342.pdf").read_bytes().startswith(b"%PDF")

    electronic = rows["20035528"]
    assert (electronic["doc_format"], electronic["parse_status"]) == ("electronic", "pending")
    assert electronic["first_seen_source"] == "index"
    assert electronic["index_seen_at"] == electronic["search_seen_at"] == "2026-10-01T13:00:00Z"


def test_second_run_uses_conditional_get_and_changes_nothing(conn, http, clerk, tmp_path):
    run(conn, http, tmp_path)
    before = filings(conn)

    summary = run(conn, http, tmp_path, now="2026-10-01T13:30:00Z")

    assert summary.index_status == "unchanged"
    assert (summary.new_from_index, summary.new_from_search, summary.downloaded) == (0, 0, 0)
    assert clerk.requests[-2].headers["If-None-Match"] == '"v1"'
    assert filings(conn) == before
    assert clerk.count("/ptr-pdfs/") == 4  # nothing re-downloaded


def test_search_first_doc_gets_filing_date_when_index_catches_up(conn, http, clerk, tmp_path):
    run(conn, http, tmp_path, download=False)
    doe = filings(conn)["20099999"]
    assert doe["first_seen_source"] == "search"
    assert doe["filing_date"] is None
    assert doe["index_seen_at"] is None
    assert doe["filer_name"] == "Hon. Jane Doe"

    xml = clerk.index_xml.decode("utf-8-sig").replace(
        "</FinancialDisclosure>",
        "<Member><Prefix>Hon.</Prefix><Last>Doe</Last><First>Jane A.</First><Suffix /><FilingType>P</FilingType>"
        "<StateDst>ZZ01</StateDst><Year>2026</Year><FilingDate>10/1/2026</FilingDate><DocID>20099999</DocID>"
        "</Member></FinancialDisclosure>",
    )
    clerk.set_index(xml.encode(), '"v2"')
    run(conn, http, tmp_path, now="2026-10-02T13:00:00Z", download=False)

    doe = filings(conn)["20099999"]
    assert doe["filing_date"] == "2026-10-01"
    assert doe["first_seen_source"] == "search"
    assert doe["first_seen_at"] == "2026-10-01T13:00:00Z"
    assert doe["search_seen_at"] == "2026-10-01T13:00:00Z"
    assert doe["index_seen_at"] == "2026-10-02T13:00:00Z"
    assert doe["filer_name"] == "Hon. Jane A. Doe"  # the index's fuller name wins

    report = house.lag_report(conn)
    assert "search page first:              1" in report
    assert "median 24.0h" in report


def test_failed_download_is_retried_next_run(conn, http, clerk, tmp_path):
    clerk.pdf_failures["20035491"] = 3  # every retry within the first run fails
    summary = run(conn, http, tmp_path)
    assert (summary.downloaded, summary.download_failures) == (3, 1)
    assert filings(conn)["20035491"]["raw_path"] is None

    summary = run(conn, http, tmp_path, now="2026-10-01T13:30:00Z")
    assert (summary.downloaded, summary.download_failures) == (1, 0)
    assert filings(conn)["20035491"]["raw_path"] == "house/2026/20035491.pdf"


def test_non_pdf_response_is_rejected(conn, http, clerk, tmp_path):
    del clerk.pdfs["20099999"]  # server answers 404 with an HTML page
    clerk.pdfs["20035491"] = b"<html>maintenance</html>"  # or 200 with HTML
    summary = run(conn, http, tmp_path)
    assert summary.download_failures == 2
    rows = filings(conn)
    assert rows["20099999"]["raw_path"] is None
    assert rows["20035491"]["raw_path"] is None
    assert not (tmp_path / "raw/house/2026/20035491.pdf").exists()


def test_downloaded_pdf_corrects_a_wrong_format_guess(conn, http, clerk, tmp_path):
    clerk.pdfs["20035491"] = clerk.pdfs["9116342"]  # electronic-looking DocID, but a scan
    run(conn, http, tmp_path)
    row = filings(conn)["20035491"]
    assert (row["doc_format"], row["parse_status"]) == ("scanned", "needs_review")


def test_stops_downloading_after_consecutive_failures(conn, http, clerk, tmp_path, monkeypatch):
    monkeypatch.setattr(house, "MAX_CONSECUTIVE_FAILURES", 2)
    clerk.pdfs.clear()
    summary = run(conn, http, tmp_path)
    assert summary.download_failures == 2
    assert clerk.count("/ptr-pdfs/") == 2


@pytest.mark.parametrize("broken", ["index", "search"])
def test_a_failing_source_is_reported_but_the_other_still_runs(conn, http, clerk, tmp_path, broken):
    setattr(clerk, f"{broken}_status", 503)
    summary = run(conn, http, tmp_path, download=False)
    assert summary.failed_sources == [f"{broken} 2026"]
    assert len(filings(conn)) == (4 if broken == "index" else 3)


def test_rerunning_never_duplicates(conn, http, clerk, tmp_path):
    for hour in ("13", "14", "15"):
        clerk.etag = f'"{hour}"'  # force the index to be re-read each time
        run(conn, http, tmp_path, now=f"2026-10-01T{hour}:00:00Z", download=False)
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 4
    assert filings(conn)["20035528"]["first_seen_at"] == "2026-10-01T13:00:00Z"


def test_pdfs_already_on_disk_are_reused_without_a_request(conn, http, clerk, tmp_path):
    cached = tmp_path / "raw/house/2026/9116342.pdf"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(clerk.pdfs["9116342"])
    partial = tmp_path / "raw/house/2026/20035528.pdf"
    partial.write_bytes(b"<html>truncated")  # not a PDF: must be fetched again

    summary = run(conn, http, tmp_path)

    assert summary.downloaded == 4
    assert clerk.count("/ptr-pdfs/2026/9116342.pdf") == 0
    assert clerk.count("/ptr-pdfs/2026/20035528.pdf") == 1
    rows = filings(conn)
    assert rows["9116342"]["raw_path"] == "house/2026/9116342.pdf"
    assert rows["9116342"]["doc_format"] == "scanned"
    assert partial.read_bytes().startswith(b"%PDF")
