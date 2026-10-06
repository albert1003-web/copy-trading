from datetime import date

import httpx

from ingest import senate

PAPER = "f873aeb4-adbb-4934-a188-79416a2e4c76"
BLUMENTHAL = "9e2ff733-aeac-4ce8-872c-3d6b7913da88"
AMENDMENT = "27f15751-bcec-4e3e-a0ab-d7e843c08d06"
ALL_IDS = {PAPER, AMENDMENT, BLUMENTHAL, "028aef33-dc0d-44a1-992f-aa35ee42685b", "0a93a20c-2a0f-4979-80ca-cc2f61297527"}
TODAY = date(2026, 10, 1)


def filings(conn):
    return {r["doc_id"]: dict(r) for r in conn.execute("SELECT * FROM filings")}


def run(conn, http, tmp_path, now="2026-10-01T13:00:00Z", **kwargs):
    kwargs.setdefault("today", TODAY)
    return senate.run(conn, http, raw_root=tmp_path / "raw", now=lambda: now, pause=lambda: None, **kwargs)


# --- parsing ------------------------------------------------------------------------------------


def test_parse_search_rows(efd):
    listings = {item.doc_id: item for item in senate.parse_search_rows(efd.rows)}
    assert set(listings) == ALL_IDS

    paper = listings[PAPER]
    assert (paper.kind, paper.doc_format) == ("paper", "scanned")
    assert paper.filer_name == "Richard Blumenthal"  # listed as 'RICHARD ', 'BLUMENTHAL'
    assert paper.filing_date == "2026-01-20"
    assert paper.url == f"https://efdsearch.senate.gov/search/view/paper/{PAPER}/"

    electronic = listings[BLUMENTHAL]
    assert (electronic.kind, electronic.doc_format) == ("ptr", "electronic")
    assert listings["028aef33-dc0d-44a1-992f-aa35ee42685b"].filer_name == "A. Mitchell McConnell, Jr."

    amendment = listings[AMENDMENT]
    assert amendment.title == "Periodic Transaction Report for 10/10/2025 (Amendment 1)"
    assert amendment.filing_date == "2026-05-11"  # date received, not the report's period


def test_parse_search_rows_skips_rows_without_report_link():
    assert senate.parse_search_rows([["A", "B", "C", "<a href='/elsewhere/'>x</a>", "01/02/2026"], ["short"]]) == []


def test_agreement_token_and_report_detection(efd):
    assert senate.agreement_token(efd.home_html) == efd.form_token
    assert senate.agreement_token(efd.reports[BLUMENTHAL]) is None
    assert senate.is_report("ptr", efd.reports[BLUMENTHAL])
    assert senate.is_report("paper", efd.reports[PAPER])
    assert not senate.is_report("ptr", efd.home_html)
    assert not senate.is_report("paper", efd.reports[BLUMENTHAL])


def test_window_start(conn):
    assert senate.window_start(conn, TODAY) == date(2026, 1, 1)
    assert senate.window_start(conn, date(2027, 1, 10)) == date(2026, 1, 1)
    senate.save_checked(conn, "2026-10-01T13:00:00Z", changed=True)
    assert senate.window_start(conn, TODAY) == date(2026, 9, 24)


# --- a full pass --------------------------------------------------------------------------------


def test_first_run_records_and_caches_every_ptr(conn, senate_http, efd, tmp_path):
    summary = run(conn, senate_http, tmp_path)

    assert summary.failed_sources == []
    assert (summary.search_ptrs, summary.new, summary.amendments) == (5, 5, 1)
    assert (summary.downloaded, summary.download_failures) == (5, 0)
    assert summary.window_start == "2026-01-01"

    # The agreement is accepted once, and searches send the CSRF token from the cookie.
    assert efd.count("/search/home/") == 2
    assert efd.searches[0]["csrfmiddlewaretoken"] == "ctok"
    assert efd.searches[0]["report_types"] == "[11]"
    assert efd.searches[0]["filer_types"] == "[1,5]"
    assert efd.searches[0]["submitted_start_date"] == "01/01/2026 00:00:00"

    rows = filings(conn)
    assert set(rows) == ALL_IDS
    paper = rows[PAPER]
    assert paper["chamber"] == "senate"
    assert (paper["doc_format"], paper["parse_status"]) == ("scanned", "needs_review")
    assert paper["raw_path"] == f"senate/2026/{PAPER}.html"
    assert paper["first_seen_at"] == paper["search_seen_at"] == "2026-10-01T13:00:00Z"
    assert paper["first_seen_source"] == "search"
    assert paper["state_district"] is None and paper["index_seen_at"] is None

    electronic = rows[BLUMENTHAL]
    assert (electronic["doc_format"], electronic["parse_status"]) == ("electronic", "pending")
    assert electronic["filing_date"] == "2026-09-28"
    assert electronic["filing_year"] == 2026
    assert electronic["filer_name"] == "Richard Blumenthal"
    assert electronic["source_url"] == f"https://efdsearch.senate.gov/search/view/ptr/{BLUMENTHAL}/"
    cached = (tmp_path / f"raw/senate/2026/{BLUMENTHAL}.html").read_text()
    assert senate.is_report("ptr", cached)


def test_rerun_adds_nothing_and_downloads_nothing(conn, senate_http, efd, tmp_path):
    run(conn, senate_http, tmp_path)
    before = filings(conn)
    views = efd.count("/search/view/")

    summary = run(conn, senate_http, tmp_path, now="2026-10-01T13:30:00Z")

    assert (summary.new, summary.downloaded) == (0, 0)
    assert filings(conn) == before  # first_seen_at and everything else unchanged
    assert efd.count("/search/view/") == views
    # The second search starts a week before the last successful one.
    assert efd.searches[-1]["submitted_start_date"] == "09/24/2026 00:00:00"
    assert summary.search_ptrs == 2  # the two reports received 09/28


def test_new_report_on_a_later_run_gets_its_own_first_seen(conn, senate_http, efd, tmp_path):
    first, *rest = efd.rows
    efd.rows = rest
    run(conn, senate_http, tmp_path)
    efd.rows = [first, *rest]

    summary = run(conn, senate_http, tmp_path, now="2026-10-02T13:00:00Z", since=date(2026, 1, 1))

    assert summary.new == 1
    assert filings(conn)[PAPER]["first_seen_at"] == "2026-10-02T13:00:00Z"
    assert filings(conn)[BLUMENTHAL]["first_seen_at"] == "2026-10-01T13:00:00Z"


def test_search_pages_through_results(conn, senate_http, efd, tmp_path, monkeypatch):
    monkeypatch.setattr(senate, "PAGE_SIZE", 2)
    summary = run(conn, senate_http, tmp_path, download=False)

    assert summary.new == 5
    assert [s["start"] for s in efd.searches] == ["0", "2", "4"]
    assert all(s["order[0][column]"] == "4" for s in efd.searches)


def test_expired_session_is_accepted_again(conn, senate_http, efd, tmp_path):
    run(conn, senate_http, tmp_path, download=False)
    efd.expire_sessions()

    summary = run(conn, senate_http, tmp_path)

    assert summary.failed_sources == []
    assert summary.downloaded == 5
    assert efd.count("/search/home/") > 2


def test_agreement_page_is_never_cached_as_a_report(conn, senate_http, efd, tmp_path):
    efd.reports[BLUMENTHAL] = efd.home_html  # e.g. the session keeps bouncing for this report

    summary = run(conn, senate_http, tmp_path)

    assert (summary.downloaded, summary.download_failures) == (4, 1)
    assert filings(conn)[BLUMENTHAL]["raw_path"] is None
    assert not (tmp_path / f"raw/senate/2026/{BLUMENTHAL}.html").exists()


def test_cached_reports_are_reused_without_a_request(conn, senate_http, efd, tmp_path):
    target = tmp_path / f"raw/senate/2026/{BLUMENTHAL}.html"
    target.parent.mkdir(parents=True)
    target.write_text(efd.reports[BLUMENTHAL])

    run(conn, senate_http, tmp_path)

    assert efd.count(f"/view/ptr/{BLUMENTHAL}/") == 0
    assert filings(conn)[BLUMENTHAL]["raw_path"] == f"senate/2026/{BLUMENTHAL}.html"


def test_transient_errors_are_retried(conn, senate_http, efd, tmp_path):
    efd.report_failures[BLUMENTHAL] = 2

    summary = run(conn, senate_http, tmp_path)

    assert (summary.downloaded, summary.download_failures) == (5, 0)


def test_downloads_stop_after_consecutive_failures(conn, senate_http, efd, tmp_path):
    efd.reports.clear()  # every report 404s

    summary = run(conn, senate_http, tmp_path)

    assert (summary.downloaded, summary.download_failures) == (0, senate.MAX_CONSECUTIVE_FAILURES)


def test_search_failure_keeps_the_cursor_and_exits_nonzero(conn, senate_http, efd, tmp_path, monkeypatch):
    run(conn, senate_http, tmp_path, download=False)
    checked = conn.execute("SELECT checked_at FROM source_state WHERE source = 'senate_search'").fetchone()[0]
    efd.search_status = 500

    summary = run(conn, senate_http, tmp_path, now="2026-10-01T14:00:00Z", download=False)

    assert summary.failed_sources == ["search"]
    assert conn.execute("SELECT checked_at FROM source_state WHERE source = 'senate_search'").fetchone()[0] == checked

    monkeypatch.setattr(senate.polite, "client",
                        lambda: httpx.Client(transport=httpx.MockTransport(efd.handler), follow_redirects=True))
    monkeypatch.setattr(senate, "connect", lambda: conn)
    monkeypatch.setattr(senate.logs, "setup", lambda: None)
    assert senate.main(["--no-download"]) == 1


# --- paper report page images (M5.4) ------------------------------------------------------------


def test_paper_report_page_images_are_cached_once(conn, senate_http, efd, tmp_path):
    import json

    summary = run(conn, senate_http, tmp_path)
    assert (summary.page_sets, summary.page_failures) == (1, 0)
    folder = tmp_path / f"raw/senate/2026/{PAPER}"
    names = json.loads((folder / "pages.json").read_text())
    assert names == ["001.gif", "002.gif", "003.gif", "004.gif", "005.gif"]
    assert all((folder / n).read_bytes().startswith(b"GIF8") for n in names)
    assert efd.count("efd-media-public") == 5

    again = run(conn, senate_http, tmp_path)
    assert again.page_sets == 0 and efd.count("efd-media-public") == 5  # complete sets aren't fetched again


def test_a_bad_page_image_leaves_the_set_incomplete_and_retries(conn, senate_http, efd, tmp_path):
    efd.media_broken.add("/media/2026/2/000/000/000000007.gif")  # the last page
    summary = run(conn, senate_http, tmp_path)
    folder = tmp_path / f"raw/senate/2026/{PAPER}"
    assert (summary.page_sets, summary.page_failures) == (0, 1) and not (folder / "pages.json").exists()

    efd.media_broken.clear()
    fetched = efd.count("efd-media-public")
    summary = run(conn, senate_http, tmp_path)
    assert summary.page_sets == 1
    assert efd.count("efd-media-public") == fetched + 1  # only the missing page is fetched again


def test_page_images_respect_the_per_pass_limit(conn, senate_http, efd, tmp_path):
    run(conn, senate_http, tmp_path, download=True)  # caches the report pages (and the one paper set)
    done, failed = senate.download_pages(senate_http, conn, tmp_path / "raw", lambda: None, limit=0)
    assert (done, failed) == (0, 0)
