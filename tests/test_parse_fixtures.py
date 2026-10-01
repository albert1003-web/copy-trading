"""Every saved filing parses to exactly the rows in its .expected.json (checked by hand against the filing).

To add a case: save the raw file as tests/fixtures/house/electronic_<doc_id>.pdf or
tests/fixtures/senate/ptr_<uuid>.html, write <same name>.expected.json, and verify it against the source.
"""

import json
from pathlib import Path

import pytest

from parse import house_pdf, senate_html

FIXTURES = Path(__file__).parent / "fixtures"
HOUSE = sorted((FIXTURES / "house").glob("electronic_*.pdf"))
SENATE = sorted((FIXTURES / "senate").glob("ptr_*.html"))


def expected(source: Path) -> list[dict]:
    return json.loads(source.with_name(source.stem + ".expected.json").read_text())


def test_enough_fixtures():
    assert len(HOUSE) >= 5 and len(SENATE) >= 5


@pytest.mark.parametrize("source", HOUSE, ids=lambda p: p.stem)
def test_house_pdf(source):
    assert [t.as_dict() for t in house_pdf.parse(source.read_bytes())] == expected(source)


@pytest.mark.parametrize("source", SENATE, ids=lambda p: p.stem)
def test_senate_html(source):
    assert [t.as_dict() for t in senate_html.parse(source.read_text())] == expected(source)


def test_house_scan_has_no_text_rows():
    assert house_pdf.parse((FIXTURES / "house" / "scanned_9116342.pdf").read_bytes()) == []


def test_senate_page_without_a_transactions_table():
    assert senate_html.parse((FIXTURES / "senate" / "home.html").read_text()) == []


def test_senate_columns_are_matched_by_header():
    html = """<table>
      <tr><th>#</th><th>Owner</th><th>Transaction Date</th><th>Asset Name</th><th>Ticker</th>
          <th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th><th>New Column</th></tr>
      <tr><td>1</td><td>Joint</td><td>03/02/2026</td><td>Apple Inc.</td><td>AAPL</td>
          <td>Stock</td><td>Purchase</td><td>$1,001 - $15,000</td><td>--</td><td>x</td></tr>
    </table>"""
    [trade] = senate_html.parse(html)
    assert (trade.owner, trade.tx_date, trade.ticker, trade.action, trade.description) == (
        "joint", "2026-03-02", "AAPL", "BUY", None)
