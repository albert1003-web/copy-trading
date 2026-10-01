"""Source values -> the normalized enums in CLAUDE.md (action, owner, asset_type, amount range, dates).

Every function returns None for a value it doesn't recognize; the caller then lowers the row's
confidence and the filing goes to needs_review instead of guessing.
"""

import re
from dataclasses import dataclass, fields
from datetime import datetime

ACTIONS = {
    "p": "BUY",
    "purchase": "BUY",
    "s": "SELL",
    "sale": "SELL",
    "sale (full)": "SELL",
    "s (partial)": "SELL_PARTIAL",
    "sale (partial)": "SELL_PARTIAL",
    "e": "EXCHANGE",
    "exchange": "EXCHANGE",
}

OWNERS = {
    "": "self",
    "self": "self",
    "sp": "spouse",
    "spouse": "spouse",
    "jt": "joint",
    "joint": "joint",
    "dc": "dependent",
    "child": "dependent",
    "dependent": "dependent",
}

# House asset-type codes (fd.house.gov/reference/asset-type-codes.aspx) and Senate "Asset Type" text.
STOCK_TYPES = {"st", "stock"}
OPTION_TYPES = {"op", "stock option", "option", "options"}

DOLLARS = re.compile(r"\$\s*([\d,]+)")
# A ticker inside an asset name: "Apple Inc. (AAPL)", "Cadence Bank (CADE$A)" (not a CUSIP like (571903BM4)),
# or leading "MRSH - Marsh & McLennan ...", or a name that is only a ticker ("SPYM").
TICKER_IN_PARENS = re.compile(r"\(([A-Z][A-Z0-9.$/-]{0,7})\)")
TICKER_LEADING = re.compile(r"^([A-Z][A-Z.]{0,5})(?: - |$)")


@dataclass
class ParsedTrade:
    line_no: int
    owner: str | None
    asset_name: str
    ticker: str | None
    asset_code: str | None
    asset_type: str
    action: str | None
    tx_date: str | None
    amount_min: int | None
    amount_max: int | None
    description: str | None = None

    def problems(self, filed_on: str | None = None) -> list[str]:
        """Fields we couldn't normalize, or that can't be right (traded after the filing date, a filer typo).
        Any problem lowers the row's confidence and sends the filing to needs_review."""
        required = ("owner", "action", "tx_date", "amount_min")
        found = [name for name in required if getattr(self, name) is None]
        if self.tx_date and filed_on and self.tx_date > filed_on:
            found.append("tx_date after filing date")
        return found

    def confidence(self, filed_on: str | None = None) -> float:
        return 1.0 if not self.problems(filed_on) else 0.5

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def clean(text: str | None) -> str:
    """Collapses runs of whitespace (including non-breaking spaces) into single spaces."""
    return " ".join((text or "").split())


def action(raw: str | None) -> str | None:
    return ACTIONS.get(clean(raw).lower())


def owner(raw: str | None) -> str | None:
    return OWNERS.get(clean(raw).lower())


def asset_type(code: str | None) -> str:
    code = clean(code).lower()
    if code in STOCK_TYPES:
        return "stock"
    if code in OPTION_TYPES:
        return "option"
    return "other"


def amount(raw: str | None) -> tuple[int | None, int | None]:
    """'$1,001 - $15,000' -> (1001, 15000); 'Over $50,000,000' -> (50000001, None)."""
    text = clean(raw)
    values = [int(v.replace(",", "")) for v in DOLLARS.findall(text)]
    if not values:
        return None, None
    if len(values) >= 2:
        return values[0], values[1]
    if "over" in text.lower():
        return values[0] + 1, None
    if text.endswith("+"):
        return values[0], None
    return values[0], values[0]  # an exact amount


def iso_date(raw: str | None) -> str | None:
    """'09/04/2026' -> '2026-09-04'."""
    try:
        return datetime.strptime(clean(raw), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def ticker(raw: str | None) -> str | None:
    """Senate ticker cell: '--' or blank means none."""
    text = clean(raw).upper()
    return text if text and text != "--" else None


def ticker_in_name(asset: str | None) -> str | None:
    """The last parenthesized ticker in an asset name, else a leading one. M1.4 validates it."""
    text = clean(asset)
    if found := TICKER_IN_PARENS.findall(text):
        return found[-1]
    match = TICKER_LEADING.match(text)
    return match.group(1) if match else None
