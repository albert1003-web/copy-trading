import json
import os
import shutil
from pathlib import Path

import httpx
import pytest

from enrich import committees, members, reference, securities, tickers
from enrich import run as enrich_run

REFERENCE = Path(__file__).parent / "fixtures" / "reference"
PINS = {116: "c116"}


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
        snapshot = (REFERENCE / "committee-membership-116.yaml").read_bytes()
        self.files[committees.SNAPSHOT_URL.format(commit="c116")] = snapshot

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        if self.down:
            return httpx.Response(503)
        if str(request.url) not in self.files:
            return httpx.Response(404)
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
RENAMED = {"SQ": "XYZ", "MMC": "MRSH", "GONE": "-"}


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
    ("GONE", (None, "delisted", False)),  # "-": a dead company whose ticker was reused
    (None, (None, "none", False)),
    ("", (None, "none", False)),
])
def test_resolve(ticker, expected):
    assert tickers.resolve(ticker, LISTED, RENAMED) == expected


def test_alias_until_covers_only_earlier_trades():
    listed = {**LISTED, "GONE": False}  # the ticker now belongs to another, listed security
    until = {"GONE": "2022-07-25"}
    assert tickers.resolve("GONE", listed, RENAMED, tx_date="2021-03-29", until=until) == (None, "delisted", False)
    assert tickers.resolve("GONE", listed, RENAMED, tx_date="2026-01-05", until=until) == ("GONE", "listed", False)
    assert tickers.resolve("GONE", listed, RENAMED, until=until) == (None, "delisted", False)  # no date: alias
    assert tickers.alias_until()["PS"] == "2021-04-06" and "FB" not in tickers.alias_until()


def test_delay_days():
    assert tickers.delay_days("2026-09-01", "2026-09-28") == 27
    assert tickers.delay_days(None, "2026-09-28") is None


def test_shipped_aliases_parse():
    assert tickers.aliases()["SQ"] == "XYZ"
    assert tickers.aliases()["FB"] == "META" and tickers.aliases()["GOGL"] == tickers.DELISTED
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
    s = enrich_run.run(conn, ref_http, raw_root=tmp_path, snapshots={})

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
    assert len(sources.requests) == len(reference.SOURCES)


def test_reference_files_are_cached_for_a_week(conn, ref_http, sources, tmp_path):
    enrich_run.run(conn, ref_http, raw_root=tmp_path, snapshots=PINS)
    enrich_run.run(conn, ref_http, raw_root=tmp_path, snapshots=PINS)
    assert len(sources.requests) == len(reference.SOURCES) + 1  # the second run used the cache (+1: the snapshot)

    stale = reference.path(tmp_path, "nasdaqlisted.txt")
    old = stale.stat().st_mtime - 8 * 24 * 3600
    os.utime(stale, (old, old))
    sources.down = True
    enrich_run.run(conn, ref_http, raw_root=tmp_path, snapshots={})  # refresh fails; the stale copy is still used
    assert stale.exists()


def test_missing_reference_with_source_down_fails(conn, ref_http, sources, tmp_path):
    sources.down = True
    with pytest.raises(RuntimeError, match="no cached copy"):
        enrich_run.run(conn, ref_http, raw_root=tmp_path, snapshots={})


def test_rerun_is_idempotent_and_reparse_keeps_enrichment(conn, ref_http, tmp_path):
    from parse import run as parse_run
    from parse.normalize import ParsedTrade

    seed(conn)
    enrich_run.run(conn, ref_http, raw_root=tmp_path, snapshots={})
    enrich_run.run(conn, None, raw_root=tmp_path, snapshots={})
    assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 13

    filing = conn.execute("SELECT * FROM filings WHERE doc_id = 'H1'").fetchone()
    reparsed = ParsedTrade(1, "spouse", "NVIDIA (NVDA)", "NVDA", "ST", "stock", "BUY", "2026-09-10", 1001, 15000)
    parse_run.save(conn, filing, [reparsed])
    row = conn.execute("SELECT member_id, symbol, ticker_status FROM trades WHERE doc_id = 'H1' AND line_no = 1")
    assert tuple(row.fetchone()) == ("P000197", "NVDA", "listed")


# --- committees ------------------------------------------------------------------------------------


@pytest.mark.parametrize("day, congress", [
    ("2019-01-02", 115), ("2019-01-03", 116), ("2020-06-01", 116), ("2021-01-03", 117), ("2026-10-01", 119),
])
def test_congress_of(day, congress):
    from datetime import date

    assert committees.congress_of(date.fromisoformat(day)) == congress


def test_membership_rolls_subcommittees_up_to_parents():
    import yaml

    data = yaml.safe_load((REFERENCE / "committee-membership-current.yaml").read_text())
    seats = {(m.member_id, m.committee_id): m for m in committees.parse_membership(data, 119, {"SSAS": "Armed"})}
    assert set(seats) == {("S001217", "SSAS"), ("R000605", "SSAS"), ("R000605", "SSBK"), ("P000048", "HSAS")}
    assert seats[("R000605", "SSAS")].role == "Chairman"
    assert seats[("S001217", "SSAS")].role is None  # chairs a subcommittee, not the committee
    assert seats[("S001217", "SSAS")].committee_name == "Armed"


def seed_sectors(conn):
    conn.executescript("""
        INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at, filer_name, state_district, doc_format)
        VALUES ('H0', 'house', '2020-03-02', '2020-03-02T13:00:00Z', 'Hon. Nancy Pelosi', 'CA12', 'electronic'),
               ('H1', 'house', '2026-09-28', '2026-09-28T13:00:00Z', 'Hon. Nancy Pelosi', 'CA11', 'electronic'),
               ('S1', 'senate', '2026-09-20', '2026-09-20T13:00:00Z', 'Rick Scott', NULL, 'electronic');
        INSERT INTO trades (doc_id, line_no, ticker, asset_type, action, tx_date, disclosure_date) VALUES
          ('H0', 1, 'JPM', 'stock', 'BUY', '2020-02-10', '2020-03-02'),
          ('H1', 1, 'JPM', 'stock', 'BUY', '2026-09-10', '2026-09-28'),
          ('S1', 1, 'LMT', 'stock', 'BUY', '2026-09-01', '2026-09-20'),
          ('S1', 2, 'BA', 'stock', 'BUY', '2026-09-01', '2026-09-20'),
          ('S1', 3, 'SPY', 'stock', 'BUY', '2026-09-01', '2026-09-20');
        INSERT INTO securities (symbol, quote_type, sector, industry, market_cap, status, updated_at) VALUES
          ('JPM', 'EQUITY', 'Financial Services', 'Banks - Diversified', 600e9, 'ok', '2026-09-30T00:00:00Z'),
          ('LMT', 'EQUITY', 'Industrials', 'Aerospace & Defense', 110e9, 'ok', '2026-09-30T00:00:00Z'),
          ('BA', 'EQUITY', 'Industrials', 'Airlines', 150e9, 'ok', '2026-09-30T00:00:00Z'),
          ('SPY', 'ETF', 'ETF', 'Large Blend', NULL, 'ok', '2026-09-30T00:00:00Z');
        INSERT INTO prices (ticker, date, close) VALUES
          ('JPM', '2020-02-28', 50), ('JPM', '2026-09-25', 150), ('JPM', '2026-09-30', 300);
    """)
    conn.commit()


def test_run_sets_sectors_buckets_committees_and_relevance(conn, ref_http, tmp_path):
    from datetime import date

    seed_sectors(conn)
    s = enrich_run.run(conn, ref_http, raw_root=tmp_path, today=date(2026, 10, 1), snapshots=PINS)

    rows = {(r["doc_id"], r["ticker"]): r for r in conn.execute("SELECT * FROM trades")}
    assert rows[("S1", "LMT")]["sector"] == "Industrials"
    assert rows[("S1", "SPY")]["sector"] == "ETF" and rows[("S1", "SPY")]["mcap_bucket"] is None
    # 600B today; JPM traded at 1/6 (2020) and 1/2 (2026) of today's price
    assert rows[("H0", "JPM")]["mcap_bucket"] == "large" and rows[("H1", "JPM")]["mcap_bucket"] == "mega"
    assert rows[("S1", "LMT")]["mcap_bucket"] == "large"  # no prices: today's cap

    relevant = {k for k, r in rows.items() if r["committee_relevant"]}
    # Rick Scott on Armed Services buys LMT (A&D); BA is Industrials but another industry. Pelosi sat on Financial
    # Services in the 116th Congress (2020), not today.
    assert relevant == {("S1", "LMT"), ("H0", "JPM")}
    assert s.committee_relevant == 2

    assert conn.execute("SELECT COUNT(*) FROM committee_memberships WHERE congress = 116").fetchone()[0] == 2
    member_committees = dict(conn.execute("SELECT member_id, committees FROM members WHERE committees IS NOT NULL"))
    assert json.loads(member_committees["S001217"]) == ["Senate Committee on Armed Services"]
    assert "P000197" not in member_committees


def test_missing_snapshot_only_drops_that_congress(conn, sources, ref_http, tmp_path):
    from datetime import date

    seed_sectors(conn)
    enrich_run.run(conn, ref_http, raw_root=tmp_path, today=date(2026, 10, 1), snapshots={116: "unknown"})
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE committee_relevant = 1").fetchone()[0] == 1


# --- securities ------------------------------------------------------------------------------------


class FakeInfo:
    def __init__(self, data):
        self.data, self.calls = data, []

    def info(self, symbol):
        self.calls.append(symbol)
        value = self.data.get(symbol, {})
        if isinstance(value, Exception):
            raise value
        return value


def test_securities_refresh_budget_staleness_and_types(conn):
    from datetime import date

    conn.executescript("""
        INSERT INTO filings (doc_id, chamber, first_seen_at) VALUES ('F', 'house', '2026-09-01T00:00:00Z');
        INSERT INTO trades (doc_id, line_no, symbol, ticker_status) VALUES
          ('F', 1, 'LMT', 'listed'), ('F', 2, 'SPY', 'listed'), ('F', 3, 'OLDCO', 'unlisted'),
          ('F', 4, 'BOOM', 'listed'), ('F', 5, NULL, 'none'), ('F', 6, 'JPM', 'listed');
        INSERT INTO securities (symbol, status, updated_at) VALUES ('JPM', 'ok', '2026-09-01T00:00:00Z');
    """)
    source = FakeInfo({
        "LMT": {"quoteType": "EQUITY", "longName": "Lockheed", "sector": "Industrials",
                "industry": "Aerospace & Defense", "marketCap": 1.1e11, "sharesOutstanding": 2.3e8},
        "SPY": {"quoteType": "ETF", "longName": "SPDR", "category": "Large Blend", "sharesOutstanding": 9e8},
        "BOOM": RuntimeError("timeout"),
    })
    s = securities.refresh(conn, source, today=date(2026, 10, 1), now=lambda: "2026-10-01T00:00:00Z",
                           pause=lambda: None)
    assert sorted(source.calls) == ["BOOM", "LMT", "OLDCO", "SPY"]  # JPM is fresh
    assert (s.ok, s.missing, s.failed) == (2, 1, 1)
    rows = {r["symbol"]: r for r in conn.execute("SELECT * FROM securities")}
    assert (rows["SPY"]["sector"], rows["SPY"]["industry"], rows["SPY"]["market_cap"]) == ("ETF", "Large Blend", None)
    assert rows["OLDCO"]["status"] == "missing" and "BOOM" not in rows

    # 40 days later: missing symbols are retried (30 d), ok ones aren't (90 d); the budget caps a run
    due = securities.due_symbols(conn, date(2026, 11, 10))
    assert due == ["BOOM", "OLDCO"]
    source.calls.clear()
    securities.refresh(conn, source, today=date(2026, 11, 10), limit=1, pause=lambda: None)
    assert source.calls == ["BOOM"]


@pytest.mark.parametrize("cap, bucket", [
    (None, None), (0, None), (250e6, "micro"), (300e6, "small"), (5e9, "mid"), (10e9, "large"), (2e12, "mega"),
])
def test_bucket(cap, bucket):
    assert securities.bucket(cap) == bucket


def test_alias_dash_marks_a_known_non_member(conn, people):
    conn.execute("INSERT INTO filings (doc_id, chamber, first_seen_at, filer_name, state_district) "
                 "VALUES ('C1', 'house', '2022-06-01T00:00:00Z', 'Richard B. Reisdorf', 'MN01')")
    assert members.assign(conn, people, {"Richard B. Reisdorf": "-"}) == []
    assert conn.execute("SELECT member_id FROM filings WHERE doc_id = 'C1'").fetchone()[0] is None
    assert members.assign(conn, people, {}) == ["Richard B. Reisdorf"]
