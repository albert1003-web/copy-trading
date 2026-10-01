"""v1 rule-based signal score (0-100) for a buy, with the reasons shown in the alert email.

  base    stock buy 50, bought calls 40 (acted on as the underlying stock; no options in the Roth)
  amount  by amount_min: $1k +0, $15k +5, $50k +10, $100k +13, $250k +16, $500k+ +20
  delay   days from trade to disclosure: <=7 +15, <=14 +10, <=30 +5, <=45 +0, later -5
  asset   listed stock +15, listed ETF +5, ticker not on NYSE/Nasdaq -10

v2 (M3.5) replaces this with the leaderboard and outcome data.
"""

from collections.abc import Mapping

AMOUNT_POINTS = [(500_001, 20), (250_001, 16), (100_001, 13), (50_001, 10), (15_001, 5)]
DELAY_POINTS = [(7, 15), (14, 10), (30, 5), (45, 0)]
LATE_DELAY_POINTS = -5


def is_call(trade: Mapping) -> bool:
    """A bought call option: 'Option Type: Call' (Senate asset name) or 'call options' (House description)."""
    if trade["asset_type"] != "option":
        return False
    text = f"{trade['asset_name'] or ''} {trade['description'] or ''}".lower()
    return "call" in text and "put" not in text


def score(trade: Mapping) -> tuple[int, list[str]]:
    reasons = []
    if is_call(trade):
        points = 40
        reasons.append("bought calls (as the stock) 40")
    else:
        points = 50
        reasons.append("buy 50")

    amount = trade["amount_min"] or 0
    bonus = next((p for low, p in AMOUNT_POINTS if amount >= low), 0)
    points += bonus
    reasons.append(f"amount +{bonus}")

    delay = trade["filing_delay_days"]
    if delay is None:
        reasons.append("delay unknown +0")
    else:
        bonus = next((p for limit, p in DELAY_POINTS if delay <= limit), LATE_DELAY_POINTS)
        points += bonus
        reasons.append(f"disclosed after {delay}d {bonus:+d}")

    if trade["ticker_status"] in ("listed", "renamed"):
        bonus = 5 if trade["is_etf"] else 15
        reasons.append(f"listed {'ETF' if trade['is_etf'] else 'stock'} +{bonus}")
    else:
        bonus = -10
        reasons.append("not on NYSE/Nasdaq -10")
    points += bonus
    return max(0, min(100, points)), reasons
