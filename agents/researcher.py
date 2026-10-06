"""Signal researcher (M5.3): a short research brief appended to high-score buy alerts.

The alerts stage calls brief() (injected by pipeline/run.py, so alerts never imports agents) for each filing whose
buys include a score of at least MIN_SCORE, before the email goes out:
- facts, gathered here: the next earnings date (Yahoo, via yfinance), the member's committees in the trade's
  Congress, the move since the trade date (context only), the member's history in the symbol and leaderboard row;
- a narrative from Claude (Claude Code backend by default, free) with recent news found by web search.
The email waits at most TIMEOUT_SECONDS for the narrative; without it, the facts still go in. A brief already
written for the same filing in the last day is reused, so a send that failed and retries isn't researched twice.

Display only: nothing here feeds the score (hard rule 3), and the brief gives context, not trading advice.
"""

import concurrent.futures
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from agents import runner

log = logging.getLogger(__name__)

AGENT = "signal_researcher"
MIN_SCORE = 60  # v2: 50 = no edge, 10 points per 1% expected 20-day excess over the S&P 500
TIMEOUT_SECONDS = 180
WEB_SEARCHES = 5
EARNINGS_TIMEOUT = 15
HORIZON_DAYS = 20  # the score's horizon in trading days; earnings inside it are flagged
REUSE_HOURS = 24
ET = ZoneInfo("America/New_York")

INSTRUCTIONS = """\
You write a short research brief that is appended to a trade alert email. The user message holds the alert's
buys and facts already gathered (earnings dates, committees, price moves, the member's history) as JSON; the owner
sees those facts as lines below your brief, so don't repeat them.
Search the web for material news on each company from the last 30 days (earnings, guidance, deals, lawsuits,
regulation, government contracts) and anything linking the company to the member's committees. Check each
article's publication date: search results often include old pieces; ignore anything older than 60 days.
Write at most about 120 words of plain text: what's material, with dates, and say plainly when you found nothing.
No buy or sell advice. End with a line "Sources:" followed by the URLs you relied on, one per line.
Return no proposals."""


@dataclass
class Brief:
    narrative: str | None
    fact_lines: list[str]
    run_id: int | None = None
    error: str | None = None
    reused: bool = False
    facts: dict = field(default_factory=dict, repr=False)


# --- facts ------------------------------------------------------------------------------------------------------


def congress_of(day: date) -> int:
    """The Congress in session on a day (same rule as enrich/committees.py; kept here so stages stay decoupled)."""
    congress = (day.year - 1789) // 2 + 1
    if day.year % 2 == 1 and day.month == 1 and day.day < 3:
        congress -= 1
    return congress


def yahoo_earnings(symbol: str, today: date) -> dict:
    """{"next": ISO date | None, "last": ISO date | None} from Yahoo. Raises on failure."""
    import yfinance as yf

    frame = yf.Ticker(symbol).get_earnings_dates(limit=8)
    days = sorted({d.date() for d in frame.index}) if frame is not None else []
    upcoming = [d for d in days if d >= today]
    past = [d for d in days if d < today]
    return {"next": upcoming[0].isoformat() if upcoming else None, "last": past[-1].isoformat() if past else None}


def _earnings(symbol: str, today: date, earnings: Callable[[str, date], dict]) -> dict:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        result = pool.submit(earnings, symbol, today).result(timeout=EARNINGS_TIMEOUT)
    except Exception as e:  # network, Yahoo changes, timeouts: the alert must still go out
        log.warning("Earnings dates for %s unavailable: %s", symbol, e)
        return {"next": None, "last": None, "error": f"{type(e).__name__}"}
    finally:
        pool.shutdown(wait=False)
    if result.get("next"):
        weekdays = int(np.busday_count(today, date.fromisoformat(result["next"])))
        result["trading_days_away"] = weekdays  # weekdays; holidays not removed
        result["inside_horizon"] = weekdays <= HORIZON_DAYS
    return result


def _close(conn: sqlite3.Connection, symbol: str, on_or_before: str | None = None) -> tuple[str, float] | None:
    sql = "SELECT date, close FROM prices WHERE ticker = ? AND close IS NOT NULL"
    params: list = [symbol]
    if on_or_before:
        sql += " AND date <= ?"
        params.append(on_or_before)
    row = conn.execute(sql + " ORDER BY date DESC LIMIT 1", params).fetchone()
    return (row[0], row[1]) if row else None


def _move(conn: sqlite3.Connection, symbol: str, bars: int) -> float | None:
    rows = conn.execute("SELECT close FROM prices WHERE ticker = ? AND close IS NOT NULL ORDER BY date DESC LIMIT ?",
                        (symbol, bars + 1)).fetchall()
    return rows[0][0] / rows[-1][0] - 1 if len(rows) == bars + 1 else None


def facts(conn: sqlite3.Connection, buys: list[tuple[dict, int]], *,
          earnings: Callable[[str, date], dict] = yahoo_earnings, today: date | None = None) -> dict:
    """The deterministic half of the brief, for one filing's high-score buys."""
    today = today or datetime.now(ET).date()
    first = buys[0][0]
    member_id = first.get("member_id")
    tx_day = date.fromisoformat(first["tx_date"]) if first.get("tx_date") else today
    congress = congress_of(tx_day)
    committees = [r[0] for r in conn.execute(
        "SELECT committee_name FROM committee_memberships WHERE member_id = ? AND congress = ? ORDER BY committee_name",
        (member_id, congress))]
    board = conn.execute(
        "SELECT rank, shrunk_score, n_filings, as_of FROM member_scores WHERE member_id = ? "
        "AND as_of = (SELECT MAX(as_of) FROM member_scores)", (member_id,)).fetchone()

    symbols = []
    for trade, score in buys:
        symbol = trade["symbol"]
        if any(s["symbol"] == symbol for s in symbols):
            continue
        company = conn.execute("SELECT name, sector, industry, market_cap FROM securities WHERE symbol = ?",
                               (symbol,)).fetchone()
        last = _close(conn, symbol)
        at_trade = _close(conn, symbol, trade.get("tx_date")) if trade.get("tx_date") else None
        history = conn.execute(
            """SELECT COUNT(*), MAX(t.tx_date) FROM trades t
               WHERE t.member_id = ? AND t.symbol = ? AND t.doc_id <> ?""",
            (member_id, symbol, trade["doc_id"])).fetchone()
        last_action = conn.execute(
            """SELECT t.action, t.tx_date FROM trades t WHERE t.member_id = ? AND t.symbol = ? AND t.doc_id <> ?
               ORDER BY t.tx_date DESC LIMIT 1""", (member_id, symbol, trade["doc_id"])).fetchone()
        symbols.append({
            "symbol": symbol, "score": score, "asset": trade.get("asset_name"),
            "company": company[0] if company else None, "sector": company[1] if company else trade.get("sector"),
            "industry": company[2] if company else trade.get("industry"),
            "market_cap": company[3] if company else None, "mcap_bucket": trade.get("mcap_bucket"),
            "committee_relevant": bool(trade.get("committee_relevant")),
            "earnings": _earnings(symbol, today, earnings),
            "last_close": last[1] if last else None, "last_date": last[0] if last else None,
            "since_trade_date": last[1] / at_trade[1] - 1 if last and at_trade else None,
            "move_5d": _move(conn, symbol, 5), "move_20d": _move(conn, symbol, 20),
            "prior_trades": history[0], "prior_last": dict(action=last_action[0], tx_date=last_action[1])
            if last_action else None,
        })
    return {
        "member": first.get("member_name"), "chamber": first.get("chamber"), "doc_id": first["doc_id"],
        "tx_date": first.get("tx_date"), "disclosure_date": first.get("disclosure_date"),
        "congress": congress, "committees": committees,
        "leaderboard": dict(zip(("rank", "shrunk_score", "n_filings", "as_of"), board, strict=True)) if board else None,
        "symbols": symbols,
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1%}"


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def render(f: dict) -> list[str]:
    """Short factual lines for the email."""
    lines = []
    committees = ", ".join(f["committees"]) or "none on record"
    lines.append(f"Committees ({_ordinal(f['congress'])} Congress): {committees}")
    if f["leaderboard"]:
        lb = f["leaderboard"]
        rank = f"#{lb['rank']}" if lb["rank"] is not None else "unranked (under 20 filings)"
        lines.append(f"Leaderboard: {rank}, shrunk 20-day excess {_pct(lb['shrunk_score'])} "
                     f"over {lb['n_filings']} filings")
    for s in f["symbols"]:
        e = s["earnings"]
        if e.get("next"):
            when = f"next earnings {e['next']}"
            if e.get("inside_horizon"):
                when += f" (~{e['trading_days_away']} trading days: inside the {HORIZON_DAYS}-day horizon)"
        else:
            when = "next earnings date unavailable"
        if e.get("last"):
            when += f"; last {e['last']}"
        name = f" ({s['company']})" if s["company"] else ""
        lines.append(f"{s['symbol']}{name}: {when}")
        sector = " / ".join(x for x in (s["sector"], s["industry"]) if x)
        if sector:
            lines.append(f"  {sector}" + ("; matches a committee's sector" if s["committee_relevant"] else ""))
        lines.append(f"  Since the trade date {_pct(s['since_trade_date'])} (context only); "
                     f"last 5 days {_pct(s['move_5d'])}, 20 days {_pct(s['move_20d'])}")
        if s["prior_trades"]:
            last = s["prior_last"]
            lines.append(f"  {f['member']}'s earlier {s['symbol']} trades: {s['prior_trades']} "
                         f"(latest {last['action']} {last['tx_date']})")
        else:
            lines.append(f"  First disclosed {s['symbol']} trade by {f['member']}")
    return lines


# --- the brief --------------------------------------------------------------------------------------------------


def _reusable(conn: sqlite3.Connection, doc_id: str) -> tuple[int, str] | None:
    since = (datetime.now(UTC) - timedelta(hours=REUSE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        "SELECT run_id, output FROM agent_runs WHERE agent = ? AND status = 'ok' AND started_at >= ? "
        "AND json_extract(inputs, '$.doc_id') = ? ORDER BY run_id DESC LIMIT 1", (AGENT, since, doc_id)).fetchone()
    return (row[0], row[1]) if row and row[1] else None


def brief(
    conn: sqlite3.Connection,
    buys: list[tuple[dict, int]],
    *,
    backend: str = runner.CLAUDE_CODE,
    agent_runner: Callable[..., runner.RunResult] = runner.run_agent,
    earnings: Callable[[str, date], dict] = yahoo_earnings,
) -> Brief | None:
    """A brief for one filing's buys, or None if none scores at least MIN_SCORE."""
    high = [(t, s) for t, s in buys if s is not None and s >= MIN_SCORE and t.get("symbol")]
    if not high:
        return None
    f = facts(conn, high, earnings=earnings)
    lines = render(f)
    doc_id = f["doc_id"]
    if reuse := _reusable(conn, doc_id):
        return Brief(reuse[1], lines, run_id=reuse[0], reused=True, facts=f)

    prompt = "Alert facts (JSON):\n" + json.dumps(f, default=str) + "\n\nWrite the research brief as instructed."
    result = agent_runner(conn, agent=AGENT, prompt=prompt, instructions=INSTRUCTIONS, web_search_uses=WEB_SEARCHES,
                          effort="medium", max_turns=10, timeout=TIMEOUT_SECONDS, backend=backend,
                          extra_inputs={"doc_id": doc_id, "symbols": [s["symbol"] for s in f["symbols"]]})
    if result.status == "ok" and result.summary.strip():
        return Brief(result.summary.strip(), lines, run_id=result.run_id, facts=f)
    log.warning("Research brief for %s failed (%s); sending the facts only", doc_id, result.error)
    return Brief(None, lines, run_id=result.run_id, error=result.error or "empty brief", facts=f)
