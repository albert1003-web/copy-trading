"""Senate PTR ingestion (Milestone 1.2): python -m ingest.senate [--since YYYY-MM-DD] [--no-download]

Each pass:
  1. Session: accept the eFD terms agreement (Django CSRF form); every search and report view needs it.
  2. Search: page through PTRs (report type 11) filed by senators and former senators, starting a week before
     the last successful search so late postings are still caught.
  3. Upsert every PTR into `filings` (doc_id = the report's UUID).
  4. Download each report page not yet cached to <raw_dir>/senate/<year>/<doc_id>.html.
     Electronic reports are HTML tables; paper reports are pages of scanned images (needs_review).
"""

import argparse
import logging
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from common import config
from common import http as polite
from common import log as logs
from db import connect

log = logging.getLogger("ingest.senate")

BASE = "https://efdsearch.senate.gov"
HOME_URL = BASE + "/search/home/"
SEARCH_PAGE_URL = BASE + "/search/"
DATA_URL = BASE + "/search/report/data/"
REPORT_LINK = re.compile(r"/search/view/(ptr|paper)/([0-9a-f-]{36})/", re.I)

PTR_REPORT_TYPE = 11
FILER_TYPES = "[1,5]"  # 1 = Senator, 5 = Former Senator (candidates aren't members)
PAGE_SIZE = 100
OVERLAP_DAYS = 7  # re-search this far back; reports can be posted with an earlier received date
SOURCE = "senate_search"
MAX_CONSECUTIVE_FAILURES = 5


class SessionError(Exception):
    """eFD sent us back to the terms agreement and accepting it again didn't help."""


@dataclass(frozen=True)
class Listing:
    """One PTR as listed by the eFD search."""

    doc_id: str
    kind: str  # ptr (electronic HTML) | paper (scanned images)
    filer_name: str
    filing_date: str | None  # ISO date the Senate received it
    title: str  # e.g. "Periodic Transaction Report for 10/10/2025 (Amendment 1)"

    @property
    def url(self) -> str:
        return f"{BASE}/search/view/{self.kind}/{self.doc_id}/"

    @property
    def doc_format(self) -> str:
        return "electronic" if self.kind == "ptr" else "scanned"


@dataclass
class Summary:
    window_start: str = ""
    search_ptrs: int = 0
    new: int = 0
    amendments: int = 0
    downloaded: int = 0
    download_failures: int = 0
    failed_sources: list[str] = field(default_factory=list)


# --- parsing ------------------------------------------------------------------------------------


def parse_search_rows(rows: list[list[str]]) -> list[Listing]:
    """Listings from the search endpoint's `data` rows: [first, last, office, report link, date received]."""
    listings = []
    for row in rows:
        if len(row) < 5:
            continue
        first, last, _office, link_html, received = row[:5]
        link = BeautifulSoup(link_html, "html.parser").find("a")
        match = REPORT_LINK.search(link.get("href", "")) if link else None
        if not match:
            continue
        listings.append(Listing(
            doc_id=match.group(2).lower(),
            kind=match.group(1).lower(),
            filer_name=_name(first, last),
            filing_date=_iso_date(received),
            title=" ".join(link.get_text(" ", strip=True).split()),
        ))
    return listings


def _name(first: str, last: str) -> str:
    """'RICHARD ', 'BLUMENTHAL' -> 'Richard Blumenthal' (paper filings are listed in capitals)."""
    name = " ".join(f"{first} {last}".split())
    return name.title() if name.isupper() else name


def _iso_date(us_date: str) -> str | None:
    try:
        return datetime.strptime(us_date.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def agreement_token(html: str) -> str | None:
    """The CSRF token in the terms-agreement form, or None if the page isn't that form."""
    soup = BeautifulSoup(html, "html.parser")
    if not soup.find("input", {"name": "prohibition_agreement"}):
        return None
    token = soup.find("input", {"name": "csrfmiddlewaretoken"})
    return token.get("value") if token else None


def is_report(kind: str, html: str) -> bool:
    """A real report page (not the agreement form or an error page) for its kind."""
    if kind == "paper":
        return "filingImage" in html
    return "filedReport" in html and "Periodic Transaction Report" in html


# --- source -------------------------------------------------------------------------------------


class Efd:
    """An eFD session: accepts the terms agreement and accepts it again when the session expires."""

    def __init__(self, http: httpx.Client):
        self.http = http
        self.accepted = False

    def accept(self) -> None:
        response = polite.request(self.http, "GET", HOME_URL)
        response.raise_for_status()
        token = agreement_token(response.text)
        if not token:
            raise SessionError("no terms agreement form on the eFD home page")
        response = polite.request(self.http, "POST", HOME_URL, headers={"Referer": HOME_URL},
                                  data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token})
        response.raise_for_status()
        if _bounced(response):
            raise SessionError("eFD did not accept the terms agreement")
        self.accepted = True

    def _send(self, method: str, url: str, data: dict[str, str] | None = None) -> httpx.Response:
        """Sends a request in an accepted session, re-accepting once if eFD bounces us to the agreement."""
        for _attempt in (1, 2):
            if not self.accepted:
                self.accept()
            csrf = self.http.cookies.get("csrftoken") or ""
            headers = {"Referer": SEARCH_PAGE_URL, "X-CSRFToken": csrf}
            form = {**data, "csrfmiddlewaretoken": csrf} if data is not None else None
            response = polite.request(self.http, method, url, headers=headers, data=form)
            if not _bounced(response):
                return response
            log.info("eFD session expired; accepting the terms again")
            self.accepted = False
        raise SessionError(f"eFD keeps returning the terms agreement for {url}")

    def search(self, start: date, pause: Callable[[], None] = polite.pause) -> list[Listing]:
        """Every senator PTR received on or after `start`, oldest first."""
        listings: list[Listing] = []
        total = None
        while total is None or len(listings) < total:
            if total is not None:
                pause()
            response = self._send("POST", DATA_URL, data={
                "start": str(len(listings)),
                "length": str(PAGE_SIZE),
                "report_types": f"[{PTR_REPORT_TYPE}]",
                "filer_types": FILER_TYPES,
                "submitted_start_date": start.strftime("%m/%d/%Y 00:00:00"),
                "submitted_end_date": "",
                "candidate_state": "",
                "senator_state": "",
                "office_id": "",
                "first_name": "",
                "last_name": "",
                "order[0][column]": "4",  # date received, so pages stay stable while new reports arrive
                "order[0][dir]": "asc",
            })
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") or []
            total = int(payload.get("recordsFiltered", 0))
            if not rows:
                break
            listings.extend(parse_search_rows(rows))
            if len(rows) < PAGE_SIZE:
                break
        return listings

    def report(self, url: str) -> httpx.Response:
        return self._send("GET", url)


def _bounced(response: httpx.Response) -> bool:
    """eFD answers requests from a session without the agreement with a redirect to the home page."""
    return response.url.path.rstrip("/") == "/search/home"


# --- storage ------------------------------------------------------------------------------------


def window_start(conn: sqlite3.Connection, today: date) -> date:
    """A week before the last successful search, or Jan 1 (of last year during January) on the first run."""
    row = conn.execute("SELECT checked_at FROM source_state WHERE source = ?", (SOURCE,)).fetchone()
    if row and row["checked_at"]:
        return date.fromisoformat(row["checked_at"][:10]) - timedelta(days=OVERLAP_DAYS)
    return date(today.year - 1 if today.month == 1 else today.year, 1, 1)


def save_checked(conn: sqlite3.Connection, now: str, changed: bool) -> None:
    conn.execute(
        """
        INSERT INTO source_state (source, checked_at, changed_at) VALUES (?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
          checked_at = excluded.checked_at,
          changed_at = COALESCE(excluded.changed_at, source_state.changed_at)
        """,
        (SOURCE, now, now if changed else None),
    )
    conn.commit()


def upsert(conn: sqlite3.Connection, listings: list[Listing], now: str) -> int:
    """Inserts new PTRs and fills gaps on known ones. Returns how many were new."""
    new = 0
    for item in listings:
        exists = conn.execute("SELECT 1 FROM filings WHERE doc_id = ?", (item.doc_id,)).fetchone()
        new += not exists
        conn.execute(
            """
            INSERT INTO filings (doc_id, chamber, filing_date, source_url, first_seen_at, parse_status,
                                 filer_name, filing_year, doc_format, first_seen_source, search_seen_at)
            VALUES (?, 'senate', ?, ?, ?, ?, ?, ?, ?, 'search', ?)
            ON CONFLICT(doc_id) DO UPDATE SET
              filing_date    = COALESCE(filings.filing_date, excluded.filing_date),
              source_url     = COALESCE(filings.source_url, excluded.source_url),
              filer_name     = COALESCE(filings.filer_name, excluded.filer_name),
              filing_year    = COALESCE(filings.filing_year, excluded.filing_year),
              search_seen_at = COALESCE(filings.search_seen_at, excluded.search_seen_at)
            """,
            (
                item.doc_id, item.filing_date, item.url, now,
                "pending" if item.doc_format == "electronic" else "needs_review",
                item.filer_name, int(item.filing_date[:4]) if item.filing_date else None, item.doc_format, now,
            ),
        )
    conn.commit()
    return new


def download_pending(
    efd: Efd, conn: sqlite3.Connection, raw_root: Path, pause: Callable[[], None] = polite.pause
) -> tuple[int, int]:
    """Downloads every Senate report page without a cached copy. Returns (downloaded, failed)."""
    pending = conn.execute(
        """
        SELECT doc_id, filing_year, source_url FROM filings
        WHERE chamber = 'senate' AND raw_path IS NULL
        ORDER BY first_seen_at, doc_id
        """
    ).fetchall()
    if pending:
        log.info("%d report(s) to cache (files already on disk are reused)", len(pending))

    downloaded = failed = consecutive = 0
    fetched_any = False
    for row in pending:
        kind = "paper" if "/view/paper/" in row["source_url"] else "ptr"
        relative = Path("senate") / str(row["filing_year"]) / f"{row['doc_id']}.html"
        target = raw_root / relative
        if target.exists() and is_report(kind, target.read_text(errors="replace")):
            _record_download(conn, row["doc_id"], relative)  # cached earlier; no request
            downloaded += 1
            continue

        if fetched_any:
            pause()
        fetched_any = True
        try:
            response = efd.report(row["source_url"])
            if response.status_code != 200 or not is_report(kind, response.text):
                raise ValueError(f"HTTP {response.status_code}, {len(response.content)} bytes, not a {kind} report")
        except (httpx.HTTPError, ValueError, SessionError) as e:
            failed += 1
            consecutive += 1
            log.warning("Download failed for %s: %s", row["doc_id"], e)
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                log.error("Stopping downloads after %d failures in a row; will retry next run", consecutive)
                break
            continue

        consecutive = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".html.part")
        tmp.write_bytes(response.content)
        tmp.replace(target)
        _record_download(conn, row["doc_id"], relative)
        downloaded += 1
    return downloaded, failed


def _record_download(conn: sqlite3.Connection, doc_id: str, relative: Path) -> None:
    conn.execute("UPDATE filings SET raw_path = ? WHERE doc_id = ?", (relative.as_posix(), doc_id))
    conn.commit()


# --- orchestration --------------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    conn: sqlite3.Connection,
    http: httpx.Client,
    *,
    since: date | None = None,
    today: date | None = None,
    download: bool = True,
    raw_root: Path | None = None,
    now: Callable[[], str] = utc_now,
    pause: Callable[[], None] = polite.pause,
) -> Summary:
    summary = Summary()
    efd = Efd(http)
    stamp = now()
    start = since or window_start(conn, today or date.today())
    summary.window_start = start.isoformat()
    try:
        listings = efd.search(start, pause)
        summary.search_ptrs = len(listings)
        summary.amendments = sum("amendment" in item.title.lower() for item in listings)
        summary.new = upsert(conn, listings, stamp)
        save_checked(conn, stamp, changed=summary.new > 0)
    except (httpx.HTTPError, ValueError, SessionError) as e:  # ValueError: a non-JSON search response
        summary.failed_sources.append("search")
        log.error("Senate search failed: %s", e)

    if download:
        summary.downloaded, summary.download_failures = download_pending(
            efd, conn, raw_root or config.raw_dir(), pause)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--since", type=date.fromisoformat,
                        help="search reports received on or after this date (YYYY-MM-DD); default: last run - 7 days")
    parser.add_argument("--no-download", action="store_true", help="record filings without downloading reports")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    with polite.client() as http:
        s = run(conn, http, since=args.since, download=not args.no_download)
    log.info(
        "Senate since %s: %d PTRs listed (%d amendments), %d new; downloaded %d, failed %d",
        s.window_start, s.search_ptrs, s.amendments, s.new, s.downloaded, s.download_failures,
    )
    if s.failed_sources:
        log.error("Failed sources: %s", ", ".join(s.failed_sources))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
