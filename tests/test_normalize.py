import pytest

from parse import normalize as norm


@pytest.mark.parametrize("raw, expected", [
    ("P", "BUY"), ("Purchase", "BUY"),
    ("S", "SELL"), ("Sale (Full)", "SELL"),
    ("S (partial)", "SELL_PARTIAL"), ("Sale (Partial)", "SELL_PARTIAL"),
    ("E", "EXCHANGE"), ("Exchange", "EXCHANGE"),
    ("  sale  (partial) ", "SELL_PARTIAL"),
    ("Gift", None), ("", None), (None, None),
])
def test_action(raw, expected):
    assert norm.action(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("", "self"), (None, "self"), ("Self", "self"),
    ("SP", "spouse"), ("Spouse", "spouse"),
    ("JT", "joint"), ("Joint", "joint"),
    ("DC", "dependent"), ("Child", "dependent"),
    ("Trust", None),
])
def test_owner(raw, expected):
    assert norm.owner(raw) == expected


@pytest.mark.parametrize("code, expected", [
    ("ST", "stock"), ("Stock", "stock"),
    ("OP", "option"), ("Stock Option", "option"),
    ("GS", "other"), ("Non-Public Stock", "other"), ("Municipal Security", "other"), (None, "other"),
])
def test_asset_type(code, expected):
    assert norm.asset_type(code) == expected


@pytest.mark.parametrize("raw, expected", [
    ("$1,001 - $15,000", (1001, 15000)),
    ("$15,001 -  $50,000", (15001, 50000)),
    ("Over $50,000,000", (50000001, None)),
    ("Spouse/DC Over $1,000,000", (1000001, None)),
    ("$50,000,001 +", (50000001, None)),
    ("$15.00", (15, 15)),
    ("$318.74", (318, 318)),
    ("", (None, None)),
    ("Unknown", (None, None)),
])
def test_amount(raw, expected):
    assert norm.amount(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("09/04/2026", "2026-09-04"), ("1/2/2026", "2026-01-02"), ("2026-09-04", None), ("", None),
])
def test_iso_date(raw, expected):
    assert norm.iso_date(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Apple Inc. - Common Stock (AAPL)", "AAPL"),
    ("Berkshire Hathaway (BRK.B)", "BRK.B"),
    ("Cadence Bank 5.50% Series A (CADE$A)", "CADE$A"),
    ("Mercato Partners Traverse IV QP, LP (GLAS Funds, LP)", None),
    ("MARRIOTT INTL INC NOTE 5.45000% 09/15/2026 (571903BM4)", None),  # a CUSIP, not a ticker
    ("Williams Companies, Inc. (The) Common Stock", None),
    ("AvalonBay Communities (AVB) (Exchanged) VMRK - Vivmark (Received)", "AVB"),
    ("MRSH - Marsh & McLennan Companies, Inc. Common Stock", "MRSH"),
    ("SPYM", "SPYM"),
    ("NVDA CALL", "NVDA"),
    ("OKTA PUTS", "OKTA"),
    ("BRK-B - Berkshire Hathaway Inc Class B", "BRK-B"),
    ("SDZNY- Sandoz Group AG ADR", "SDZNY"),
    ("QQQ M CALL", None),  # QQQM split by the filer: no guess
    ("FORD Motor Company", None),
    ("GS Managed Structured Note Strategy", None),
    ("", None),
])
def test_ticker_in_name(raw, expected):
    assert norm.ticker_in_name(raw) == expected


def test_ticker_cell():
    assert norm.ticker(" wfc ") == "WFC"
    assert norm.ticker("--") is None
    assert norm.ticker("") is None


def test_problems_and_confidence():
    trade = norm.ParsedTrade(line_no=1, owner="self", asset_name="X", ticker=None, asset_code=None,
                             asset_type="other", action="BUY", tx_date="2026-01-02", amount_min=1001, amount_max=15000)
    assert trade.problems("2026-01-20") == [] and trade.confidence("2026-01-20") == 1.0
    assert trade.problems("2026-01-01") == ["tx_date after filing date"]  # a filer typo
    assert trade.confidence("2026-01-01") < 1.0
    trade.action = None
    assert trade.problems() == ["action"] and trade.confidence() < 1.0


@pytest.mark.parametrize("raw, expected", [
    ("NVDA CALL", True), ("OKTA PUT", True), ("SMCI CALLS", True),
    ("NVIDIA Corporation", False), ("GS Managed Structured Note Strategy", False), ("CALLAWAY GOLF", False),
])
def test_is_option_name(raw, expected):
    assert norm.is_option_name(raw) is expected
