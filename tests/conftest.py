import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from db import connect

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


class FakeHouseClerk:
    """In-memory stand-in for disclosures-clerk.house.gov, served through httpx.MockTransport."""

    def __init__(self):
        house = FIXTURES / "house"
        self.index_xml = (house / "2026FD.xml").read_bytes()
        self.search_html = (house / "search_2026.html").read_text()
        self.etag = '"v1"'
        self.pdfs = {
            "20035528": (house / "electronic_20035528.pdf").read_bytes(),
            "20035491": (house / "electronic_20035528.pdf").read_bytes(),
            "20099999": (house / "electronic_20035528.pdf").read_bytes(),
            "9116342": (house / "scanned_9116342.pdf").read_bytes(),
        }
        self.pdf_failures: dict[str, int] = {}  # doc_id -> remaining 503 responses
        self.requests: list[httpx.Request] = []
        self.index_status = 200
        self.search_status = 200

    def set_index(self, xml: bytes, etag: str):
        self.index_xml, self.etag = xml, etag

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("FD.zip"):
            if self.index_status != 200:
                return httpx.Response(self.index_status)
            if request.headers.get("If-None-Match") == self.etag:
                return httpx.Response(304)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("2026FD.xml", self.index_xml)
                z.writestr("2026FD.txt", "unused")
            return httpx.Response(200, content=buf.getvalue(),
                                  headers={"ETag": self.etag, "Last-Modified": "Wed, 30 Sep 2026 13:00:52 GMT"})
        if path.endswith("ViewMemberSearchResult"):
            if self.search_status != 200:
                return httpx.Response(self.search_status)
            return httpx.Response(200, text=self.search_html)
        if "/ptr-pdfs/" in path:
            doc_id = path.rsplit("/", 1)[1].removesuffix(".pdf")
            if self.pdf_failures.get(doc_id, 0) > 0:
                self.pdf_failures[doc_id] -= 1
                return httpx.Response(503)
            if doc_id not in self.pdfs:
                return httpx.Response(404, text="<html>not found</html>")
            return httpx.Response(200, content=self.pdfs[doc_id])
        return httpx.Response(404)

    def count(self, fragment: str) -> int:
        return sum(fragment in str(r.url) for r in self.requests)


@pytest.fixture
def clerk():
    return FakeHouseClerk()


@pytest.fixture
def http(clerk):
    client = httpx.Client(transport=httpx.MockTransport(clerk.handler))
    yield client
    client.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retries and download pauses shouldn't slow the tests down."""
    monkeypatch.setattr("common.http.time.sleep", lambda _s: None)


class FakeSenateEfd:
    """In-memory stand-in for efdsearch.senate.gov: the terms agreement (CSRF + session cookie), the
    DataTables search endpoint, and report pages. Requests without an accepted session are redirected
    to the agreement, as eFD does."""

    def __init__(self):
        senate = FIXTURES / "senate"
        self.home_html = (senate / "home.html").read_text()
        self.form_token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', self.home_html).group(1)
        self.rows = json.loads((senate / "search_rows.json").read_text())
        ptr = (senate / "ptr_9e2ff733-aeac-4ce8-872c-3d6b7913da88.html").read_text()
        paper = (senate / "paper_f873aeb4-adbb-4934-a188-79416a2e4c76.html").read_text()
        self.reports = {}
        for row in self.rows:
            kind, doc_id = re.search(r"/view/(\w+)/([0-9a-f-]+)/", row[3]).groups()
            self.reports[doc_id] = paper if kind == "paper" else ptr
        self.sessions: set[str] = set()
        self.report_failures: dict[str, int] = {}  # doc_id -> remaining 503 responses
        self.searches: list[dict[str, str]] = []  # form of every accepted search request
        self.requests: list[httpx.Request] = []
        self.search_status = 200

    def expire_sessions(self):
        self.sessions.clear()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        cookies = dict(c.split("=", 1) for c in request.headers.get("cookie", "").split("; ") if "=" in c)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.method == "POST" else {}

        if path == "/search/home/":
            if request.method == "GET":
                return httpx.Response(200, text=self.home_html, headers={"Set-Cookie": "csrftoken=ctok; Path=/"})
            if form.get("prohibition_agreement") == "1" and form.get("csrfmiddlewaretoken") == self.form_token:
                session = f"s{len(self.sessions) + len(self.requests)}"
                self.sessions.add(session)
                return httpx.Response(302, headers={"Location": "/search/",
                                                    "Set-Cookie": f"sessionid={session}; Path=/"})
            return httpx.Response(200, text=self.home_html)
        if path == "/search/":
            return httpx.Response(200, text="<html>search</html>")

        bounce = httpx.Response(302, headers={"Location": "/search/home/"})
        if cookies.get("sessionid") not in self.sessions:
            return bounce
        if path == "/search/report/data/":
            if request.headers.get("X-CSRFToken") != cookies.get("csrftoken"):
                return httpx.Response(403)
            if self.search_status != 200:
                return httpx.Response(self.search_status)
            self.searches.append(form)
            start = datetime.strptime(form["submitted_start_date"], "%m/%d/%Y %H:%M:%S").date()
            rows = [r for r in self.rows if datetime.strptime(r[4], "%m/%d/%Y").date() >= start]
            rows.sort(key=lambda r: datetime.strptime(r[4], "%m/%d/%Y"))
            offset, length = int(form["start"]), int(form["length"])
            return httpx.Response(200, json={"draw": 0, "recordsTotal": len(rows), "recordsFiltered": len(rows),
                                             "data": rows[offset:offset + length], "result": "ok"})
        match = re.match(r"/search/view/(?:ptr|paper)/([0-9a-f-]+)/$", path)
        if match:
            doc_id = match.group(1)
            if self.report_failures.get(doc_id, 0) > 0:
                self.report_failures[doc_id] -= 1
                return httpx.Response(503)
            if doc_id not in self.reports:
                return httpx.Response(404, text="<html>not found</html>")
            return httpx.Response(200, text=self.reports[doc_id])
        return httpx.Response(404)

    def count(self, fragment: str) -> int:
        return sum(fragment in str(r.url) for r in self.requests)


@pytest.fixture
def efd():
    return FakeSenateEfd()


@pytest.fixture
def senate_http(efd):
    client = httpx.Client(transport=httpx.MockTransport(efd.handler), follow_redirects=True)
    yield client
    client.close()
