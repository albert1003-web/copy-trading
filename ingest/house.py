"""House PTR ingestion (Milestone 1.1): python -m ingest.house [--year YYYY] [--no-download] [--lag-report]

Each pass:
  1. Index:  conditional GET of the Clerk's yearly {year}FD.zip (rebuilt about once a day).
  2. Search: POST the live search page for {year} (may list new filings before the index does).
  3. Upsert every PTR into `filings`; record when each source first listed it (index_seen_at /
     search_seen_at), so --lag-report can tell whether the index lags the search page.
  4. Download each PTR PDF not yet cached to <raw_dir>/house/<year>/<doc_id>.pdf.
"""

import argparse
import io
import logging
import re
import sqlite3
import statistics
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from common import config
from common import http as polite
from common import log as logs
from db import connect
from ingest.available import ON_CONFLICT_SQL, availability

log = logging.getLogger("ingest.house")

BASE = "https://disclosures-clerk.house.gov"
INDEX_URL = BASE + "/public_disc/financial-pdfs/{year}FD.zip"
SEARCH_URL = BASE + "/FinancialDisclosure/ViewMemberSearchResult"
PDF_URL = BASE + "/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
PTR_LINK = re.compile(r"ptr-pdfs/(\d{4})/(\w+)\.pdf", re.I)

MAX_CONSECUTIVE_FAILURES = 5


@dataclass(frozen=True)
class Listing:
    """One PTR as listed by a source."""

    doc_id: str
    year: int
    filer_name: str
    state_district: str | None
    filing_date: str | None  # ISO date; only the index has it
    source: str  # index | search


@dataclass
class Summary:
    index_status: str = "not run"  # changed | unchanged | failed
    index_ptrs: int = 0
    search_ptrs: int = 0
    new_from_index: int = 0
    new_from_search: int = 0
    downloaded: int = 0
    download_failures: int = 0
    failed_sources: list[str] = field(default_factory=list)


# --- parsing ------------------------------------------------------------------------------------


def parse_index(xml_bytes: bytes) -> list[Listing]:
    """PTRs (FilingType P) from the index XML."""
    listings = []
    for m in ET.fromstring(xml_bytes).iter("Member"):
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        name = " ".join(p for p in (_text(m, "Prefix"), _text(m, "First"), _text(m, "Last"), _text(m, "Suffix")) if p)
        listings.append(Listing(
            doc_id=_text(m, "DocID"),
            year=int(_text(m, "Year")),
            filer_name=name,
            state_district=_text(m, "StateDst") or None,
            filing_date=_iso_date(_text(m, "FilingDate")),
            source="index",
        ))
    return listings


def parse_search(html: str) -> list[Listing]:
    """PTRs from the search-results table (rows whose Filing column starts with "PTR")."""
    listings = []
    for row in BeautifulSoup(html, "html.parser").find_all("tr"):
        cells = {td.get("data-label"): td for td in row.find_all("td")}
        filing = cells.get("Filing")
        name_cell = cells.get("Name")
        link = name_cell.find("a") if name_cell else None
        if not filing or not link or not filing.get_text(strip=True).upper().startswith("PTR"):
            continue
        match = PTR_LINK.search(link.get("href", ""))
        if not match:
            continue
        office = cells.get("Office")
        listings.append(Listing(
            doc_id=match.group(2),
            year=int(match.group(1)),
            filer_name=_search_name(link.get_text(" ", strip=True)),
            state_district=(office.get_text(strip=True) or None) if office else None,
            filing_date=None,
            source="search",
        ))
    return listings


def _text(element: ET.Element, tag: str) -> str:
    return (element.findtext(tag) or "").strip()


def _iso_date(us_date: str) -> str | None:
    try:
        return datetime.strptime(us_date, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _search_name(raw: str) -> str:
    """'Alford, Hon.. Mark' -> 'Hon. Mark Alford'."""
    raw = re.sub(r"\.{2,}", ".", raw)
    last, _, rest = raw.partition(",")
    return " ".join(f"{rest.strip()} {last.strip()}".split())


def guess_format(doc_id: str) -> str:
    """Electronic PTRs have 8-digit DocIDs starting with 2; paper filings (scanned) start with 8 or 9."""
    return "electronic" if doc_id.startswith("2") else "scanned"


def pdf_format(content: bytes) -> str:
    """A PDF with fonts has a text layer; image-only PDFs are scans."""
    return "electronic" if b"/Font" in content else "scanned"


# --- sources ------------------------------------------------------------------------------------


def fetch_index(http: httpx.Client, conn: sqlite3.Connection, year: int, now: str) -> list[Listing] | None:
    """Index PTRs, or None if the ZIP hasn't changed since the last check (HTTP 304)."""
    source = f"house_index_{year}"
    state = conn.execute("SELECT etag, last_modified FROM source_state WHERE source = ?", (source,)).fetchone()
    headers = {}
    if state and state["etag"]:
        headers["If-None-Match"] = state["etag"]
    if state and state["last_modified"]:
        headers["If-Modified-Since"] = state["last_modified"]

    response = polite.request(http, "GET", INDEX_URL.format(year=year), headers=headers)
    if response.status_code == 304:
        _save_state(conn, source, now, changed=False)
        return None
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        xml_name = next(n for n in archive.namelist() if n.lower().endswith(".xml"))
        listings = parse_index(archive.read(xml_name))
    _save_state(conn, source, now, changed=True,
                etag=response.headers.get("ETag"), last_modified=response.headers.get("Last-Modified"))
    return listings


def fetch_search(http: httpx.Client, year: int) -> list[Listing]:
    response = polite.request(http, "POST", SEARCH_URL,
                              data={"LastName": "", "FilingYear": str(year), "State": "", "District": ""})
    response.raise_for_status()
    return parse_search(response.text)


def _save_state(conn, source, now, *, changed, etag=None, last_modified=None):
    conn.execute(
        """
        INSERT INTO source_state (source, etag, last_modified, checked_at, changed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
          checked_at    = excluded.checked_at,
          etag          = CASE WHEN ? THEN excluded.etag ELSE source_state.etag END,
          last_modified = CASE WHEN ? THEN excluded.last_modified ELSE source_state.last_modified END,
          changed_at    = CASE WHEN ? THEN excluded.changed_at ELSE source_state.changed_at END
        """,
        (source, etag, last_modified, now, now if changed else None, changed, changed, changed),
    )
    conn.commit()


# --- storage ------------------------------------------------------------------------------------


def upsert(conn: sqlite3.Connection, listings: list[Listing], now: str, *, backfill: bool = False) -> int:
    """Inserts new PTRs and fills gaps on known ones. Returns how many were new.

    backfill=True marks new rows as not seen live (available_at estimated from the filing date; never alerted).
    """
    new = 0
    for item in listings:
        exists = conn.execute("SELECT 1 FROM filings WHERE doc_id = ?", (item.doc_id,)).fetchone()
        new += not exists
        fmt = guess_format(item.doc_id)
        from_index = item.source == "index"
        available_at, basis = availability(item.filing_date, now, backfill=backfill)
        conn.execute(
            f"""
            INSERT INTO filings (doc_id, chamber, filing_date, source_url, first_seen_at, parse_status,
                                 filer_name, state_district, filing_year, doc_format, first_seen_source,
                                 index_seen_at, search_seen_at, available_at, available_basis)
            VALUES (?, 'house', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
              filing_date    = COALESCE(filings.filing_date, excluded.filing_date),
              source_url     = COALESCE(filings.source_url, excluded.source_url),
              filer_name     = CASE WHEN excluded.index_seen_at IS NOT NULL THEN excluded.filer_name
                                    ELSE COALESCE(filings.filer_name, excluded.filer_name) END,
              state_district = COALESCE(filings.state_district, excluded.state_district),
              filing_year    = COALESCE(filings.filing_year, excluded.filing_year),
              index_seen_at  = COALESCE(filings.index_seen_at, excluded.index_seen_at),
              search_seen_at = COALESCE(filings.search_seen_at, excluded.search_seen_at),{ON_CONFLICT_SQL}
            """,
            (
                item.doc_id, item.filing_date, PDF_URL.format(year=item.year, doc_id=item.doc_id), now,
                "pending" if fmt == "electronic" else "needs_review",
                item.filer_name, item.state_district, item.year, fmt, item.source,
                now if from_index else None, None if from_index else now, available_at, basis,
            ),
        )
    conn.commit()
    return new


def download_pending(
    http: httpx.Client, conn: sqlite3.Connection, raw_root: Path, pause: Callable[[], None] = polite.pause
) -> tuple[int, int]:
    """Downloads every House PTR without a cached PDF. Returns (downloaded, failed)."""
    pending = conn.execute(
        """
        SELECT doc_id, filing_year, source_url FROM filings
        WHERE chamber = 'house' AND raw_path IS NULL
        ORDER BY first_seen_at, doc_id
        """
    ).fetchall()
    if pending:
        log.info("%d PDF(s) to cache (files already on disk are reused)", len(pending))

    downloaded = failed = consecutive = 0
    fetched_any = False
    for row in pending:
        relative = Path("house") / str(row["filing_year"]) / f"{row['doc_id']}.pdf"
        target = raw_root / relative
        if target.exists() and target.read_bytes()[:4] == b"%PDF":
            _record_download(conn, row["doc_id"], relative, target.read_bytes())  # cached earlier; no request
            downloaded += 1
            continue

        if fetched_any:
            pause()
        fetched_any = True
        try:
            response = polite.request(http, "GET", row["source_url"])
            content = response.content
            if response.status_code != 200 or not content.startswith(b"%PDF"):
                raise ValueError(f"HTTP {response.status_code}, {len(content)} bytes, not a PDF")
        except (httpx.HTTPError, ValueError) as e:
            failed += 1
            consecutive += 1
            log.warning("Download failed for %s: %s", row["doc_id"], e)
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                log.error("Stopping downloads after %d failures in a row; will retry next run", consecutive)
                break
            continue

        consecutive = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".pdf.part")
        tmp.write_bytes(content)
        tmp.replace(target)
        _record_download(conn, row["doc_id"], relative, content)
        downloaded += 1
    return downloaded, failed


def _record_download(conn: sqlite3.Connection, doc_id: str, relative: Path, content: bytes) -> None:
    """Stores the cached path and the format the PDF actually has (a scan needs review before parsing)."""
    fmt = pdf_format(content)
    conn.execute(
        """
        UPDATE filings SET
          raw_path = ?,
          doc_format = ?,
          parse_status = CASE WHEN parse_status IN ('pending', 'needs_review')
                              THEN (CASE WHEN ? = 'electronic' THEN 'pending' ELSE 'needs_review' END)
                              ELSE parse_status END
        WHERE doc_id = ?
        """,
        (relative.as_posix(), fmt, fmt, doc_id),
    )
    conn.commit()


# --- orchestration --------------------------------------------------------------------------------


def default_years(today: date) -> list[int]:
    """The current filing year, plus last year during January (late filings for the prior year)."""
    return [today.year - 1, today.year] if today.month == 1 else [today.year]


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    conn: sqlite3.Connection,
    http: httpx.Client,
    years: list[int],
    *,
    download: bool = True,
    backfill: bool = False,
    raw_root: Path | None = None,
    now: Callable[[], str] = utc_now,
    pause: Callable[[], None] = polite.pause,
) -> Summary:
    summary = Summary()
    for year in years:
        stamp = now()
        try:
            listings = fetch_index(http, conn, year, stamp)
            if listings is None:
                summary.index_status = "unchanged"
            else:
                summary.index_status = "changed"
                summary.index_ptrs += len(listings)
                summary.new_from_index += upsert(conn, listings, stamp, backfill=backfill)
        except (httpx.HTTPError, zipfile.BadZipFile, ET.ParseError, StopIteration) as e:
            summary.index_status = "failed"
            summary.failed_sources.append(f"index {year}")
            log.error("House index %s failed: %s", year, e)

        try:
            listings = fetch_search(http, year)
            summary.search_ptrs += len(listings)
            summary.new_from_search += upsert(conn, listings, stamp, backfill=backfill)
        except httpx.HTTPError as e:
            summary.failed_sources.append(f"search {year}")
            log.error("House search %s failed: %s", year, e)

    if download:
        summary.downloaded, summary.download_failures = download_pending(
            http, conn, raw_root or config.raw_dir(), pause)
    return summary


def lag_report(conn: sqlite3.Connection) -> str:
    """Which source lists new PTRs first, and by how much (search page vs daily index)."""
    rows = conn.execute(
        "SELECT index_seen_at, search_seen_at FROM filings WHERE chamber = 'house'"
    ).fetchall()
    search_first, index_first, same, only_index, only_search = [], 0, 0, 0, 0
    for r in rows:
        i, s = r["index_seen_at"], r["search_seen_at"]
        if i and s:
            if s < i:
                search_first.append(_hours_between(s, i))
            elif i < s:
                index_first += 1
            else:
                same += 1
        elif i:
            only_index += 1
        elif s:
            only_search += 1

    lines = [
        f"House PTRs tracked: {len(rows)}",
        f"  seen by both in the same run:   {same}",
        f"  search page first:              {len(search_first)}",
        f"  index first:                    {index_first}",
        f"  only in search (index pending): {only_search}",
        f"  only in index:                  {only_index}",
    ]
    if search_first:
        lines.append(f"  index lag when search was first: median {statistics.median(search_first):.1f}h, "
                     f"max {max(search_first):.1f}h")
    return "\n".join(lines)


def _hours_between(earlier: str, later: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(later, fmt) - datetime.strptime(earlier, fmt)).total_seconds() / 3600


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--year", type=int, action="append", help="filing year (repeatable); default: current")
    parser.add_argument("--no-download", action="store_true", help="record filings without downloading PDFs")
    parser.add_argument("--lag-report", action="store_true", help="print index-vs-search detection lag and exit")
    args = parser.parse_args(argv)

    logs.setup()
    conn = connect()
    if args.lag_report:
        print(lag_report(conn))
        return 0

    years = args.year or default_years(date.today())
    with polite.client() as http:
        s = run(conn, http, years, download=not args.no_download)
    log.info(
        "House %s: index %s (%d PTRs), search %d PTRs; new: %d via index, %d via search; "
        "downloaded %d, failed %d",
        ",".join(map(str, years)), s.index_status, s.index_ptrs, s.search_ptrs,
        s.new_from_index, s.new_from_search, s.downloaded, s.download_failures,
    )
    if s.failed_sources:
        log.error("Failed sources: %s", ", ".join(s.failed_sources))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
