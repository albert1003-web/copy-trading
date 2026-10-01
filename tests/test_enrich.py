import json
import os
import shutil
from pathlib import Path

import httpx
import pytest

from enrich import members, reference, tickers
from enrich import run as enrich_run

REFERENCE = Path(__file__).parent / "fixtures" / "reference"


@pytest.fixture(scope="module")
def people():
    current = json.loads((REFERENCE / "legislators-current.json").read_text())
    historical = json.loads((REFERENCE / "legislators-historical.json").read_text())
    return members.load(current, historical)


class FakeReferenceSources:
    """Serves the trimmed reference fixtures at their real URLs."""

    def __init__(self):
        self.requests: list[str] = []
        self.down = False
        self.files = {url: (REFERENCE / name).read_bytes() for name, url in reference.SOURCES.items()}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.down:
            return httpx.Response(503)
        return httpx.Response(200, content=self.files[str(request.url)])


@pytest.fixture
def sources():
    return FakeReferenceSources()


@pytest.fixture
def ref_http(sources):
    client = httpx.Client(transport=httpx.MockTransport(sources.handler))
    yield client
    client.close()


# --- members ---------------------------------------------------------------------------------------


def test_load_keeps_recent_members_only(people):
    ids = {p.member_id: p for p in people}
    assert "B000575" in ids and not ids["B000575"].active  # Roy Blunt, a former senator (term ended 2023)
    assert all(p.name != "John Andrew Peters" for p in people)  # left Congress in 1873
    assert ids["P000197"].house_seats == {"CA11", "CA12"}  # CA12 before the 2023 redistricting
    assert ids["B001323"].house_seats == {"AK00"}  # at-large
    assert (ids["S001217"].chamber, ids["S001217"].party, ids["S001217"].state) == ("senate", "R", "FL")


@pytest.mark.parametrize("filer, chamber, seat, expected", [
    ("A. Mitchell McConnell, Jr.", "senate", None, "M000355"),
    ("M. Michael Rounds", "senate", None, "R000605"),
    ("Rick Scott", "senate", None, "S001217"),
    ("Tim Scott", "senate", None, "S001184"),
    ("James Banks", "senate", None, "B001299"),
    ("Gary C Peters", "senate", None, "P000595"),  # not Scott Peters of the House
    ("Roy Blunt", "senate", None, "B000575"),  # former senator
    ("Hon. Scott H. Peters", "house", "CA50", "P000608"),
    ("Hon. James D Jordan", "house", "OH04", "J000289"),
    ("Hon. John J Mr McGuire III", "house", "VA05", "M001239"),
    ("Hon. August Lee Pfluger II", "house", "TX11", "P000048"),
    ("Hon. Nicholas J. Begich III", "house", "AK00", "B001323"),
    ("Hon. Nancy Pelosi", "house", "CA99", "P000197"),  # wrong district: falls back to last name + state
    ("Hon. Nobody Atall", "house", "CA11", None),
    ("Scott", "senate", None, None),  # two Senator Scotts and no first name to tell them apart
])
def test_match(people, filer, chamber, seat, expected):
    assert members.match(filer, chamber, seat, people) == expected


def test_alias_overrides_the_rules(people, tmp_path):
    path = tmp_path / "aliases.csv"
    path.write_text("filer_name,bioguide\nHon. Nobody Atall,P000197\n")
    assert members.match("Hon. Nobody Atall", "house", "CA11", people, members.aliases(path)) == "P000197"


# --- tickers ---------------------------------------------------------------------------------------


LISTED = {"AAPL": False, "BRK.B": False, "EFC$D": False, "SPY": True, "XYZ": False, "MRSH": False}
RENAMED = {"SQ": "XYZ", "MMC": "MRSH"}


@pytest.mark.parametrize("ticker, expected", [
    ("AAPL", ("AAPL", "listed", False)),
    (" aapl ", ("AAPL", "listed", False)),
    ("BRK.B", ("BRK.B", "listed", False)),
    ("BRK-B", ("BRK.B", "listed", False)),
    ("BRK/B", ("BRK.B", "listed", False)),
    ("EFC$D", ("EFC$D", "listed", False)),
    ("SPY", ("SPY", "listed", True)),
    ("SQ", ("XYZ", "renamed", False)),
    ("MMC", ("MRSH", "renamed", False)),
    ("TGOPY", ("TGOPY", "unlisted", False)),
    (None, (None, "none", False)),
    ("", (None, "none", False)),
])
def test_resolve(ticker, expected):
    assert tickers.resolve(ticker, LISTED, RENAMED) == expected


def test_delay_days():
    assert tickers.delay_days("2026-09-01", "2026-09-28") == 27
    assert tickers.delay_days(None, "2026-09-28") is None


def test_shipped_aliases_parse():
    assert tickers.aliases()["SQ"] == "XYZ"
    assert members.aliases() == {} or all(members.aliases().values())


def test_symbol_lists_skip_test_issues_and_footer(tmp_path):
    shutil.copytree(REFERENCE, tmp_path / "reference")
    listed = reference.symbols(tmp_path)
    assert listed["BRK.B"] is False and listed["SPY"] is True and listed["QQQ"] is True
    assert "ZZZT" not in listed
    assert not any(s.startswith("File Creation") for s in listed)


# --- the whole pass --------------------------------------------------------------------------------


def seed(conn):
    conn.executescript("""
        INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at, filer_name, state_district, doc_format)
        VALUES ('H1', 'house', '2026-09-28', '2026-09-28T13:00:00Z', 'Hon. Nancy Pelosi', 'CA11', 'electronic'),
               ('S1', 'senate', '2026-09-20', '2026-09-20T13:00:00Z', 'Rick Scott', NULL, 'electronic'),
               ('X1', 'house', '2026-09-21', '2026-09-21T13:00:00Z', 'Hon. Nobody Atall', 'CA01', 'scanned');
        INSERT INTO trades (doc_id, line_no, ticker, asset_type, action, owner, tx_date, disclosure_date, amount_min)
        VALUES ('H1', 1, 'NVDA', 'stock', 'BUY', 'spouse', '2026-09-10', '2026-09-28', 1001),
               ('H1', 2, 'SQ', 'stock', 'SELL', 'spouse', '2026-09-11', '2026-09-28', 1001),
               ('H1', 3, NULL, 'other', 'BUY', 'spouse', NULL, '2026-09-28', 1001),
               ('S1', 1, 'TGOPY', 'stock', 'BUY', 'self', '2026-09-01', '2026-09-20', 1001);
    """)
    conn.commit()


def test_run_enriches_members_trades_and_delays(conn, ref_http, sources, tmp_path):
    seed(conn)
    s = enrich_run.run(conn, ref_http, raw_root=tmp_path)

    assert s.members == 13
    assert s.unmatched_filers == ["Hon. Nobody Atall"]
    assert s.ticker_status == {"listed": 1, "renamed": 1, "none": 1, "unlisted": 1}
    assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 13
    filed = dict(conn.execute("SELECT doc_id, member_id FROM filings").fetchall())
    assert filed == {"H1": "P000197", "S1": "S001217", "X1": None}
    rows = conn.execute(
        "SELECT member_id, symbol, ticker_status, filing_delay_days FROM trades ORDER BY doc_id, line_no"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("P000197", "NVDA", "listed", 18),
        ("P000197", "XYZ", "renamed", 17),
        ("P000197", None, "none", None),
        ("S001217", "TGOPY", "unlisted", 19),
    ]
    assert len(sources.requests) == 4


def test_reference_files_are_cached_for_a_week(conn, ref_http, sources, tmp_path):
    enrich_run.run(conn, ref_http, raw_root=tmp_path)
    enrich_run.run(conn, ref_http, raw_root=tmp_path)
    assert len(sources.requests) == 4  # the second run used the cache

    stale = reference.path(tmp_path, "nasdaqlisted.txt")
    old = stale.stat().st_mtime - 8 * 24 * 3600
    os.utime(stale, (old, old))
    sources.down = True
    enrich_run.run(conn, ref_http, raw_root=tmp_path)  # refresh fails; the stale copy is still used
    assert stale.exists()


def test_missing_reference_with_source_down_fails(conn, ref_http, sources, tmp_path):
    sources.down = True
    with pytest.raises(RuntimeError, match="no cached copy"):
        enrich_run.run(conn, ref_http, raw_root=tmp_path)


def test_rerun_is_idempotent_and_reparse_keeps_enrichment(conn, ref_http, tmp_path):
    from parse import run as parse_run
    from parse.normalize import ParsedTrade

    seed(conn)
    enrich_run.run(conn, ref_http, raw_root=tmp_path)
    enrich_run.run(conn, None, raw_root=tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 13

    filing = conn.execute("SELECT * FROM filings WHERE doc_id = 'H1'").fetchone()
    reparsed = ParsedTrade(1, "spouse", "NVIDIA (NVDA)", "NVDA", "ST", "stock", "BUY", "2026-09-10", 1001, 15000)
    parse_run.save(conn, filing, [reparsed])
    row = conn.execute("SELECT member_id, symbol, ticker_status FROM trades WHERE doc_id = 'H1' AND line_no = 1")
    assert tuple(row.fetchone()) == ("P000197", "NVDA", "listed")
