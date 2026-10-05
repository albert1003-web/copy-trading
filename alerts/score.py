"""Alert scores (0-100) for a buy, with the reasons shown in the email, plus a suggested entry.

v2 (M3.5), used once the nightly analytics have run: the expected 20-day excess return vs the S&P 500 from D0,
  expected = the member's shrunk leaderboard score (member_horizon_stats, h = 20; the pooled mean if the member
             has fewer than MIN_MEMBER_FILINGS, as the leaderboard ranks no one below that) + for each trade
             feature (common/signals.py), its level's shrunk effect (signal_factors.effect: 0 for a factor with
             no reliable spread)
  score    = 50 + 1000 * expected, clamped to 0-100: 50 is no edge; each 1% of expected excess is 10 points.
The suggested entry comes from entry_delays (M3.2): the member's best delay, else the trade's market-cap
bucket's, else all buys'.

v1 (rule-based), the fallback before any analytics exist:
  base    stock buy 50, bought calls 40 (acted on as the underlying stock; no options in the Roth)
  amount  by amount_min: $1k +0, $15k +5, $50k +10, $100k +13, $250k +16, $500k+ +20
  delay   days from trade to disclosure: <=7 +15, <=14 +10, <=30 +5, <=45 +0, later -5
  asset   listed stock +15, listed ETF +5, ticker not on NYSE/Nasdaq -10
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from common.signals import levels

AMOUNT_POINTS = [(500_001, 20), (250_001, 16), (100_001, 13), (50_001, 10), (15_001, 5)]
DELAY_POINTS = [(7, 15), (14, 10), (30, 5), (45, 0)]
LATE_DELAY_POINTS = -5


def is_call(trade: Mapping) -> bool:
    """A bought call option: 'Option Type: Call' (Senate asset name) or 'call options' (House description)."""
    if trade["asset_type"] != "option":
        return False
    text = f"{trade['asset_name'] or ''} {trade['description'] or ''}".lower()
    return "call" in text and "put" not in text


def score_v1(trade: Mapping) -> tuple[int, list[str]]:
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


# --- v2 -------------------------------------------------------------------------------------------

POINTS_PER_EXCESS = 1000  # 1% expected excess = 10 points
MIN_MEMBER_FILINGS = 20  # analytics/leaderboard.py MIN_N: fewer filings and the member's record isn't used
FACTOR_LABELS = {"mcap": "size", "delay": "disclosed", "amount": "amount", "committee": "committee overlap",
                 "kind": "kind"}


@dataclass
class Model:
    pooled: float  # mean 20-day excess of all copyable filings (what effects are measured against)
    members: dict[str, tuple[float, int]]  # member_id -> (shrunk 20-day excess, filings)
    factors: dict[tuple[str, str], tuple[float, int]]  # (factor, level) -> (effect on excess, filings)
    delays: dict[tuple[str, str], tuple[int | None, float | None, int]]  # (group_type, key) -> (best_k, gain, n)


def load_model(conn: sqlite3.Connection) -> Model | None:
    """The v2 inputs from the analytics tables, or None before they exist (then v1 scores)."""
    rows = conn.execute("SELECT * FROM signal_factors").fetchall()
    pooled = next((r["shrunk"] for r in rows if r["factor"] == "all"), None)
    if pooled is None:
        return None
    factors = {(r["factor"], r["level"]): (r["effect"], r["n"]) for r in rows if r["factor"] != "all"}
    members = {r["member_id"]: (r["shrunk_score"], r["n_filings"]) for r in conn.execute(
        "SELECT member_id, shrunk_score, n_filings FROM member_horizon_stats "
        "WHERE horizon = 20 AND as_of = (SELECT MAX(as_of) FROM member_horizon_stats)")}
    delays = {(r["group_type"], r["group_key"]): (r["best_k"], r["gain"], r["n"])
              for r in conn.execute("SELECT * FROM entry_delays")}
    return Model(pooled, members, factors, delays)


def score_v2(trade: Mapping, model: Model) -> tuple[int, list[str]]:
    member = model.members.get(trade["member_id"])
    if member and member[1] >= MIN_MEMBER_FILINGS:
        expected = member[0]
        reasons = [f"member {member[0]:+.2%} ({member[1]} filings)"]
    else:
        expected = model.pooled
        history = f"only {member[1]} filings" if member else "no history"
        reasons = [f"member: {history}, using the average {model.pooled:+.2%}"]
    for factor, level in levels(trade).items():
        found = model.factors.get((factor, level))
        if found is None:
            continue
        effect = found[0]
        expected += effect
        if round(effect, 4):  # only the features that move it (most have no reliable effect)
            reasons.append(f"{FACTOR_LABELS[factor]} {level} {effect:+.2%}")
    reasons.insert(0, f"expected 20-day excess vs S&P 500 {expected:+.2%}")
    return max(0, min(100, round(50 + POINTS_PER_EXCESS * expected))), reasons


def score(trade: Mapping, model: Model | None = None) -> tuple[int, list[str]]:
    """v2 when the analytics model exists, else v1 (and the reasons say so)."""
    if model is None:
        points, reasons = score_v1(trade)
        return points, reasons + ["v1 rules: no outcome history yet"]
    return score_v2(trade, model)


def suggested_entry(trade: Mapping, model: Model | None) -> str | None:
    """When to buy, from open-inflation history: the member's, else the size bucket's, else all buys'."""
    if model is None:
        return None
    bucket = trade["mcap_bucket"] or "unknown"
    for key, basis in (
        (("member", trade["member_id"]), "this member's {n} filings"),
        (("mcap", bucket), f"{bucket}-cap buys, {{n}} filings"),
        (("all", "all"), "all buys, {n} filings"),
    ):
        found = model.delays.get(key)
        if found is None or found[0] is None:
            continue
        best_k, gain, n = found
        if best_k == 0:
            return f"Buy at the next open (D0); waiting hasn't paid off ({basis.format(n=n)})"
        days = "day" if best_k == 1 else "days"
        return (f"Consider waiting {best_k} trading {days} after D0: opens averaged {gain:.2%} lower "
                f"({basis.format(n=n)})")
    return None
