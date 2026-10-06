"""Daily digest (M5.2): what happened since the last digest, on the app's Agents page. Weekdays after the close.

    python -m agents.digest                 # the pipeline runs this once per weekday, after the nightly stages
    python -m agents.digest --dry-run       # print it; write nothing
    python -m agents.digest --template      # skip the model: the fact sections only
    python -m agents.digest --api           # write the narrative with the Anthropic API instead of Claude Code

The numbers come from SQL here and are always rendered by code (render), so they're exact and complete. Claude
(Claude Code backend by default, free) writes a short "what matters" on top and may query for context. If that
fails, the digest is written from the template alone and the failure is a pipeline warning, so a day is never lost.

The window runs from the previous digest's end (source_state `agents.digest`: checked_at = the weekday covered,
changed_at = the window end, UTC) to now; the first one covers the last 24 hours. Monday's covers the weekend.
"""

import argparse
import json
import logging
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from agents import runner
from common import log as logs
from common.exit_rules import describe, parse_rule
from db import connect

log = logging.getLogger(__name__)

AGENT = "daily_digest"
SOURCE = "agents.digest"
NIGHTLY_SOURCE = "pipeline.nightly"  # pipeline/run.py: the last weekday whose nightly stages succeeded
BENCHMARK = "SPY"
HORIZONS = (5, 20, 60)
TOP_OUTCOMES = 5
TOP_N = 10  # leaderboard places worth reporting entries to and exits from
ET = ZoneInfo("America/New_York")

INSTRUCTIONS = """\
You write the owner's daily digest. The user message holds today's facts as JSON; the owner also sees them as
tables right below what you write, so don't repeat them line by line.
Write at most about 150 words of plain text (no Markdown headings or tables): what stands out and what, if
anything, needs the owner's attention or action (a watched member's new buy, a position near its exit, a sale by
the member behind a position, proposals waiting for review). Quiet day: say so in one or two sentences.
You may query the database for context, but keep it to a few queries. Return no proposals."""


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- facts ------------------------------------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    cursor = conn.execute(sql, params)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def _calendar(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT date FROM prices WHERE ticker = ? ORDER BY date", (BENCHMARK,))]


def facts(conn: sqlite3.Connection, since: str, until: str) -> dict:
    """Everything the digest reports for the window (since, until], as plain data."""
    days = _calendar(conn)
    return {
        "window": {"since": since, "until": until, "last_trading_day": days[-1] if days else None},
        "filings": _filings(conn, since, until),
        "alerts": _alerts(conn, since, until),
        "positions": _positions(conn, days),
        "exits": _rows(conn, """
            SELECT p.ticker, e.rule, e.reason, e.triggered_on, e.price, p.buy_price
            FROM exit_alerts e JOIN my_positions p ON p.position_id = e.position_id
            WHERE e.sent_at > ? AND e.sent_at <= ? ORDER BY e.triggered_on
            """, (since, until)),
        "outcomes": _outcomes(conn, days, since),
        "leaderboard": _leaderboard(conn),
        "proposals": _rows(conn, """
            SELECT p.run_id, r.agent, p.kind, p.title FROM agent_proposals p JOIN agent_runs r USING (run_id)
            WHERE p.approved IS NULL AND p.applied_at IS NULL ORDER BY p.proposal_id
            """),
    }


def _filings(conn: sqlite3.Connection, since: str, until: str) -> dict:
    window = "f.available_basis = 'seen' AND f.first_seen_at > ? AND f.first_seen_at <= ?"
    counts = {r["chamber"]: r["n"] for r in _rows(
        conn, f"SELECT f.chamber, COUNT(*) AS n FROM filings f WHERE {window} GROUP BY f.chamber", (since, until))}
    watched = _rows(conn, f"""
        SELECT f.doc_id, f.filing_date, f.doc_format, f.parse_status, f.source_url, m.name AS member
        FROM filings f JOIN watchlist w ON w.member_id = f.member_id AND w.active = 1
        JOIN members m ON m.member_id = f.member_id
        WHERE {window} ORDER BY f.first_seen_at
        """, (since, until))
    for f in watched:
        f["trades"] = _rows(conn, """
            SELECT COALESCE(t.symbol, t.ticker) AS symbol, t.asset_name, t.asset_type, t.action, t.owner,
                   t.tx_date, t.amount_min, t.amount_max, t.filing_delay_days,
                   (SELECT MAX(a.score) FROM alerts a WHERE a.trade_id = t.trade_id) AS alert_score
            FROM trades t WHERE t.doc_id = ? ORDER BY t.line_no
            """, (f["doc_id"],))
    return {"by_chamber": counts, "watched": watched}


def _alerts(conn: sqlite3.Connection, since: str, until: str) -> dict:
    by_rule = {r["rule"] or "other": r["n"] for r in _rows(
        conn, "SELECT rule, COUNT(*) AS n FROM alerts WHERE sent_at > ? AND sent_at <= ? GROUP BY rule",
        (since, until))}
    scans = conn.execute("SELECT COUNT(*) FROM filing_alerts WHERE sent_at > ? AND sent_at <= ?",
                         (since, until)).fetchone()[0]
    return {"trades_by_rule": by_rule, "scanned_filings": scans}


def _close_on_or_before(conn: sqlite3.Connection, ticker: str, day: str) -> tuple[str, float] | None:
    row = conn.execute("SELECT date, close FROM prices WHERE ticker = ? AND date <= ? AND close IS NOT NULL "
                       "ORDER BY date DESC LIMIT 1", (ticker, day)).fetchone()
    return (row[0], row[1]) if row else None


def _positions(conn: sqlite3.Connection, days: list[str]) -> list[dict]:
    """Open positions on the same price basis as your fill (split-adjusted closes, dividends not added)."""
    out = []
    last_day = days[-1] if days else "9999-12-31"
    for p in _rows(conn, """
            SELECT p.position_id, p.ticker, p.buy_date, p.buy_price, p.shares, p.exit_rule,
                   e.triggered_on, e.reason AS exit_reason
            FROM my_positions p LEFT JOIN exit_alerts e ON e.position_id = p.position_id
            WHERE p.status = 'open' ORDER BY p.buy_date, p.position_id
            """):
        ticker = p["ticker"].upper()
        last = _close_on_or_before(conn, ticker, last_day)
        spy_buy = _close_on_or_before(conn, BENCHMARK, p["buy_date"])
        spy_last = _close_on_or_before(conn, BENCHMARK, last_day)
        rule = parse_rule(p["exit_rule"])
        held = sum(1 for d in days if d > p["buy_date"])
        if p["triggered_on"]:
            status = f"exit triggered {p['triggered_on']} ({p['exit_reason']})"
        elif rule is not None:
            status = f"watching: {describe(rule)}"
        else:
            status = "not watched"
        out.append({
            "ticker": ticker, "buy_date": p["buy_date"], "buy_price": p["buy_price"], "shares": p["shares"],
            "last_close": last[1] if last else None, "last_date": last[0] if last else None,
            "return": last[1] / p["buy_price"] - 1 if last else None,
            "spy_return": spy_last[1] / spy_buy[1] - 1 if spy_buy and spy_last else None,
            "days_held": held, "max_hold": rule.max_hold if rule else None, "status": status,
        })
    return out


def _outcomes(conn: sqlite3.Connection, days: list[str], since: str) -> list[dict]:
    """Watched members' or alerted copyable buys that reached D0 + h on a trading day in the window, one line per
    filing and symbol (its trades share D0, so they're averaged). From D0 only; trade-date returns never appear."""
    since_day = datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone(ET).date().isoformat()
    index = {d: i for i, d in enumerate(days)}
    found = []
    for h in HORIZONS:
        d0_days = [days[i - h] for i, d in enumerate(days) if d > since_day and i >= h]
        if not d0_days:
            continue
        marks = ", ".join("?" * len(d0_days))
        for r in _rows(conn, f"""
                SELECT t.doc_id, m.name AS member, COALESCE(t.symbol, t.ticker) AS symbol, o.d0_date,
                       COUNT(*) AS n_trades, AVG(o.ret_{h}) AS ret, AVG(o.abn_ret_{h}) AS abn_ret
                FROM trade_outcomes o JOIN trades t ON t.trade_id = o.trade_id
                LEFT JOIN members m ON m.member_id = t.member_id
                WHERE o.copyable = 1 AND o.abn_ret_{h} IS NOT NULL AND o.d0_date IN ({marks})
                  AND (EXISTS (SELECT 1 FROM watchlist w WHERE w.member_id = t.member_id AND w.active = 1)
                       OR EXISTS (SELECT 1 FROM alerts a WHERE a.trade_id = t.trade_id))
                GROUP BY t.doc_id, COALESCE(t.symbol, t.ticker), o.d0_date, m.name
                """, d0_days):
            r["horizon"] = h
            r["end_date"] = days[index[r["d0_date"]] + h]
            found.append(r)
    found.sort(key=lambda r: abs(r["abn_ret"]), reverse=True)
    return found[:TOP_OUTCOMES]


def _leaderboard(conn: sqlite3.Connection) -> dict:
    snapshots = [r[0] for r in conn.execute("SELECT DISTINCT as_of FROM member_scores ORDER BY as_of DESC LIMIT 2")]
    if not snapshots:
        return {"as_of": None}
    sql = """SELECT s.member_id, m.name, s.rank, s.shrunk_score, COALESCE(w.active, 0) AS watched
             FROM member_scores s JOIN members m USING (member_id) LEFT JOIN watchlist w USING (member_id)
             WHERE s.as_of = ?"""
    now = {r["member_id"]: r for r in _rows(conn, sql, (snapshots[0],))}
    before = {r["member_id"]: r for r in _rows(conn, sql, (snapshots[1],))} if len(snapshots) > 1 else {}

    def top(rows):
        return {k for k, r in rows.items() if r["rank"] is not None and r["rank"] <= TOP_N}

    entered = sorted(top(now) - top(before), key=lambda k: now[k]["rank"]) if before else []
    left = sorted(top(before) - top(now), key=lambda k: before[k]["rank"]) if before else []
    watched = [{"name": r["name"], "rank": r["rank"], "score": r["shrunk_score"],
                "rank_before": before.get(k, {}).get("rank"), "score_before": before.get(k, {}).get("shrunk_score")}
               for k, r in sorted(now.items(), key=lambda kv: (kv[1]["rank"] is None, kv[1]["rank"] or 0))
               if r["watched"]]
    return {
        "as_of": snapshots[0], "previous": snapshots[1] if before else None,
        "entered_top": [{"name": now[k]["name"], "rank": now[k]["rank"], "watched": bool(now[k]["watched"])}
                        for k in entered],
        "left_top": [{"name": before[k]["name"], "rank_before": before[k]["rank"],
                      "rank": now.get(k, {}).get("rank")} for k in left],
        "watched": watched,
    }


# --- rendering --------------------------------------------------------------------------------------------------


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1%}"


def _amount(low: int | None, high: int | None) -> str:
    from alerts.email import amount

    return amount(low, high)


def render(f: dict) -> str:
    """The fact sections as plain text (the app shows it in a monospace card)."""
    out: list[str] = []
    w = f["window"]
    out.append(f"Since {w['since'][:16].replace('T', ' ')} UTC"
               + (f" (prices through {w['last_trading_day']})" if w["last_trading_day"] else ""))

    fl = f["filings"]
    total = sum(fl["by_chamber"].values())
    counts = ", ".join(f"{n} {c}" for c, n in sorted(fl["by_chamber"].items()))
    out += ["", "NEW FILINGS", f"{total} new PTR filing(s)" + (f" ({counts})" if total else "") + "."]
    if not fl["watched"]:
        out.append("None from watched members.")
    for filing in fl["watched"]:
        kind = " [scanned: no trade rows yet]" if filing["doc_format"] == "scanned" else ""
        out.append(f"- {filing['member']}, filed {filing['filing_date'] or '?'}{kind}")
        for t in filing["trades"]:
            score = f", alert score {t['alert_score']:.0f}" if t["alert_score"] is not None else ""
            delay = f", {t['filing_delay_days']}d delay" if t["filing_delay_days"] is not None else ""
            out.append(f"    {t['action']:<12} {t['symbol'] or t['asset_name'] or '?'}, "
                       f"{_amount(t['amount_min'], t['amount_max'])}, traded {t['tx_date'] or '?'}{delay}{score}")

    al = f["alerts"]
    sent = sum(al["trades_by_rule"].values())
    parts = [f"{n} {r.replace('_', ' ')}" for r, n in sorted(al["trades_by_rule"].items())]
    if al["scanned_filings"]:
        parts.append(f"{al['scanned_filings']} scanned-filing notice(s)")
    out += ["", "ALERTS SENT", (f"{sent} trade alert(s): " + ", ".join(parts)) if parts else "None."]

    out += ["", "OPEN POSITIONS"]
    if not f["positions"]:
        out.append("None logged.")
    for p in f["positions"]:
        held = f"day {p['days_held']}" + (f" of {p['max_hold']}" if p["max_hold"] else "")
        price = f"${p['last_close']:,.2f} on {p['last_date']}" if p["last_close"] is not None else "no price yet"
        out.append(f"- {p['ticker']:<6} {p['shares']:g} @ ${p['buy_price']:,.2f} ({p['buy_date']}): {price}, "
                   f"{_pct(p['return'])} vs buy, S&P 500 {_pct(p['spy_return'])}; {held}; {p['status']}")

    out += ["", "EXITS TRIGGERED"]
    if not f["exits"]:
        out.append("None.")
    for e in f["exits"]:
        price = f" at ${e['price']:,.2f}" if e["price"] is not None else ""
        out.append(f"- {e['ticker'].upper()}: {e['reason']}{price} on {e['triggered_on']} ({e['rule']})")

    out += ["", "OUTCOMES REACHED (watched or alerted buys, from D0)"]
    if not f["outcomes"]:
        out.append("None reached a 5, 20 or 60 trading-day mark.")
    for o in f["outcomes"]:
        trades = f" ({o['n_trades']} trades)" if o["n_trades"] > 1 else ""
        out.append(f"- {o['symbol']:<6} {o['member'] or '?'}{trades}, D0 {o['d0_date']}, {o['horizon']}d: "
                   f"{_pct(o['ret'])}, {_pct(o['abn_ret'])} vs S&P 500")

    lb = f["leaderboard"]
    out += ["", "LEADERBOARD (20-day, shrunk)"]
    if lb["as_of"] is None:
        out.append("No leaderboard yet.")
    else:
        if lb["previous"] is None:
            out.append(f"First snapshot ({lb['as_of']}).")
        for e in lb["entered_top"]:
            out.append(f"- Entered the top {TOP_N}: {e['name']} (#{e['rank']}){' [watched]' if e['watched'] else ''}")
        for e in lb["left_top"]:
            now = f"#{e['rank']}" if e["rank"] is not None else "unranked"
            out.append(f"- Left the top {TOP_N}: {e['name']} (#{e['rank_before']} -> {now})")
        if lb["previous"] and not lb["entered_top"] and not lb["left_top"]:
            out.append(f"No changes in the top {TOP_N} since {lb['previous']}.")
        for m in lb["watched"]:
            rank = f"#{m['rank']}" if m["rank"] is not None else "unranked"
            moved = f" (was #{m['rank_before']})" if m["rank_before"] not in (None, m["rank"]) else ""
            out.append(f"  Watched: {m['name']} {rank}{moved}, score {_pct(m['score'])}")

    out += ["", "WAITING FOR YOUR REVIEW"]
    if not f["proposals"]:
        out.append("No pending proposals.")
    else:
        out.append(f"{len(f['proposals'])} pending proposal(s) on the Agents page:")
        out += [f"- {p['title']} ({p['agent']}, run {p['run_id']})" for p in f["proposals"]]
    return "\n".join(out)


# --- the run ----------------------------------------------------------------------------------------------------


@dataclass
class Summary:
    run_id: int | None = None
    since: str = ""
    until: str = ""
    narrative: bool = False
    fallback_reason: str | None = None
    text: str = field(default="", repr=False)


def _state(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    row = conn.execute("SELECT checked_at, changed_at FROM source_state WHERE source = ?", (SOURCE,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def due(conn: sqlite3.Connection) -> bool:
    """A digest is due once per weekday, after that day's nightly stages succeeded."""
    nightly = conn.execute("SELECT checked_at FROM source_state WHERE source = ?", (NIGHTLY_SOURCE,)).fetchone()
    if not nightly or not nightly[0]:
        return False
    covered, _ = _state(conn)
    return covered is None or covered < nightly[0]


def run(
    conn: sqlite3.Connection,
    *,
    backend: str = runner.CLAUDE_CODE,
    template_only: bool = False,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    agent_runner: Callable[..., runner.RunResult] = runner.run_agent,
) -> Summary:
    moment = now()
    covered, last_end = _state(conn)
    since = last_end or stamp(moment - timedelta(hours=24))
    until = stamp(moment)
    f = facts(conn, since, until)
    text = render(f)
    summary = Summary(since=since, until=until, text=text)

    if not template_only:
        prompt = ("Today's digest facts (JSON):\n" + json.dumps(f, default=str)
                  + "\n\nWrite the digest's opening as instructed.")
        log_conn = connect(":memory:") if dry_run else conn
        result = agent_runner(log_conn, agent=AGENT, prompt=prompt, instructions=INSTRUCTIONS, web_search_uses=0,
                              effort="medium", max_turns=8, backend=backend)
        if result.status == "ok" and result.summary.strip():
            summary.narrative = True
            summary.text = result.summary.strip() + "\n\n" + text
            if not dry_run:
                summary.run_id = result.run_id
                conn.execute("UPDATE agent_runs SET output = ? WHERE run_id = ?", (summary.text, result.run_id))
        else:
            summary.fallback_reason = result.error or "the narrative came back empty"
            log.warning("Digest narrative failed (%s); writing it from the template", summary.fallback_reason)

    if not summary.narrative:
        if template_only:
            summary.text = text
        else:
            summary.text = f"(Written from the template: {summary.fallback_reason})\n\n{text}"
        if not dry_run:
            inputs = {"since": since, "until": until, "template_only": template_only,
                      "fallback_reason": summary.fallback_reason}
            summary.run_id = runner.start_run(conn, AGENT, inputs, None, lambda: until)
            runner.finish_run(conn, summary.run_id, summary.text, [], now=lambda: until)

    if not dry_run:
        nightly = conn.execute("SELECT checked_at FROM source_state WHERE source = ?", (NIGHTLY_SOURCE,)).fetchone()
        day = (nightly[0] if nightly and nightly[0] else None) or moment.astimezone(ET).date().isoformat()
        conn.execute(
            "INSERT INTO source_state (source, checked_at, changed_at) VALUES (?, ?, ?) "
            "ON CONFLICT (source) DO UPDATE SET checked_at = excluded.checked_at, changed_at = excluded.changed_at",
            (SOURCE, max(day, covered or ""), until),
        )
        conn.commit()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the digest; write nothing")
    parser.add_argument("--template", action="store_true", help="no model: the fact sections only")
    parser.add_argument("--api", action="store_true", help="use the Anthropic API instead of Claude Code")
    args = parser.parse_args(argv)
    logs.setup()
    s = run(connect(), backend=runner.API if args.api else runner.CLAUDE_CODE, template_only=args.template,
            dry_run=args.dry_run)
    print(s.text)
    if s.run_id:
        print(f"\nLogged as agent run {s.run_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
