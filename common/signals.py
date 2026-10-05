"""Trade features shared by the v2 score's two halves: analytics/factors.py (learns each level's effect) and
alerts/score.py (applies it to a new trade). Kept here so both bucket a trade the same way without importing
each other's internals.
"""

from collections.abc import Mapping

FACTORS = ("mcap", "delay", "amount", "committee", "kind")


def delay_level(days: int | None) -> str:
    if days is None:
        return "unknown"
    for limit, label in ((7, "<=7d"), (14, "8-14d"), (30, "15-30d"), (45, "31-45d")):
        if days <= limit:
            return label
    return ">45d"


def amount_level(amount_min: int | None) -> str:
    amount = amount_min or 0
    if amount >= 250_001:
        return "$250k+"
    if amount >= 50_001:
        return "$50k-250k"
    if amount >= 15_001:
        return "$15k-50k"
    return "<$15k"


def kind_level(trade: Mapping) -> str:
    if trade["asset_type"] == "option":
        return "call"  # the only options we copy are bought calls
    if trade["ticker_status"] == "unlisted":
        return "unlisted"
    return "etf" if trade["is_etf"] else "stock"


def levels(trade: Mapping) -> dict[str, str]:
    """Factor -> level for a trade row (needs mcap_bucket, filing_delay_days, amount_min, committee_relevant,
    asset_type, ticker_status, is_etf)."""
    return {
        "mcap": trade["mcap_bucket"] or "unknown",
        "delay": delay_level(trade["filing_delay_days"]),
        "amount": amount_level(trade["amount_min"]),
        "committee": "yes" if trade["committee_relevant"] else "no",
        "kind": kind_level(trade),
    }
