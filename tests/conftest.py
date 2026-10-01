import io
import zipfile
from pathlib import Path

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
