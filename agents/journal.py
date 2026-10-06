"""Monthly journal review (M5.5): your real trades vs the system's recommendations vs the S&P 500, for a month.

    python -m agents.journal                       # the pipeline runs this once, early in each month
    python -m agents.journal --month 2026-09 --dry-run
    python -m agents.journal --template            # no model: the fact sections only

Facts (SQL, rendered by code):
- your positions held during the month (my_positions): the month's return and P&L, the return since your buy, the
  S&P 500 over the same spans (closes, your fill's basis: split-adjusted, dividends not added). For a position
  linked to an alert: your entry vs the D0 open and how many days after D0 you bought, the alert's suggested exit,
  and the system's own fixed-horizon result for that trade (trade_outcomes, from D0);
- the system's book: every buy alert sent that month, bought at the D0 open, one observation per filing, 20-day
  return vs the S&P 500 once matured (else to date, approximate), split into the ones you followed and didn't;
- the S&P 500 over the month and since your first position (or since alerts began).
Claude (Claude Code) writes the review on top; notes only, no watchlist changes. Template fallback as for the
digest (agents/report.py). On the Agents page.
"""

import argparse
import logging
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from agents import report, runner
from agents.report import pct, rows
from common import log as logs
from db import connect

log = logging.getLogger(__name__)

AGENT = "journal_reviewer"
SOURCE = "agents.monthly"  # checked_at = the month reviewed, YYYY-MM
BENCHMARK = "SPY"
ALERTS_START = "alerts.start"

INSTRUCTIONS = """\
You write the owner's monthly trading journal review. The user message holds the month's facts as JSON; the owner
also sees them as tables right below what you write, so interpret rather than repeat them.
Write at most about 300 words of plain text comparing three things honestly: the owner's real trades, the system's
recommendations (its alerts, bought at the D0 open), and simply holding the S&P 500. Cover timing costs (buying
after D0, paying above the D0 open), exits vs the suggested exit, alerts not followed and how they did, and how
much of the result could be luck given the sample size. If no trades were logged, say so briefly and focus on the
system's alerts. Query the database to check anything before you rely on it. Proposals: none, or `note` only."""


def month_bounds(month: str) -> tuple[date, date]:
    start = date.fromisoformat(f"{month}-01")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


def previous_month(day: date) -> str:
    return (day.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def due(conn: sqlite3.Connection) -> str | None:
    """The month to review, or None: once a month, after the first nightly run of the next month."""
    nightly = report.nightly_day(conn)
    if not nightly:
        return None
    month = previous_month(date.fromisoformat(nightly))
    done, _ = report.marker(conn, SOURCE)
    return month if done is None or done < month else None


# --- facts ------------------------------------------------------------------------------------------------------


def _close(conn: sqlite3.Connection, ticker: str, day: str) -> tuple[str, float] | None:
    row = conn.execute("SELECT date, close FROM prices WHERE ticker = ? AND date <= ? AND close IS NOT NULL "
                       "ORDER BY date DESC LIMIT 1", (ticker.upper(), day)).fetchone()
    return (row[0], row[1]) if row else None


def _ratio(a: tuple | None, b: tuple | None) -> float | None:
    return a[1] / b[1] - 1 if a and b else None


def _positions(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    before = (start - timedelta(days=1)).isoformat()
    out = []
    for p in rows(conn, """
            SELECT p.*, a.sent_at AS alert_sent, a.score AS alert_score, a.suggested_exit,
                   o.d0_date, o.d0_open, o.ret_20 AS system_ret_20, o.abn_ret_20 AS system_abn_20,
                   COALESCE(t.symbol, t.ticker) AS source_symbol, m.name AS member
            FROM my_positions p
            LEFT JOIN alerts a ON a.trade_id = p.trade_id
            LEFT JOIN trade_outcomes o ON o.trade_id = p.trade_id
            LEFT JOIN trades t ON t.trade_id = p.trade_id
            LEFT JOIN members m ON m.member_id = t.member_id
            WHERE p.buy_date <= ? AND (p.sell_date IS NULL OR p.sell_date >= ?)
            ORDER BY p.buy_date, p.position_id
            """, (end.isoformat(), start.isoformat())):
        ticker = p["ticker"].upper()
        sold = p["status"] == "closed" and p["sell_date"] and p["sell_date"] <= end.isoformat()
        exit_day = p["sell_date"] if sold else end.isoformat()
        exit_price = (exit_day, p["sell_price"]) if sold else _close(conn, ticker, end.isoformat())
        bought_before = p["buy_date"] < start.isoformat()
        start_price = _close(conn, ticker, before) if bought_before else (p["buy_date"], p["buy_price"])
        spy_start = _close(conn, BENCHMARK, before if bought_before else p["buy_date"])
        spy_end = _close(conn, BENCHMARK, exit_day)
        entry = (p["buy_date"], p["buy_price"])
        d0_gap = None
        if p["d0_date"]:
            d0_gap = conn.execute("SELECT COUNT(*) FROM prices WHERE ticker = ? AND date > ? AND date <= ?",
                                  (BENCHMARK, p["d0_date"], p["buy_date"])).fetchone()[0]
        out.append({
            "ticker": ticker, "shares": p["shares"], "buy_date": p["buy_date"], "buy_price": p["buy_price"],
            "status": "sold" if sold else "open", "sell_date": p["sell_date"] if sold else None,
            "exit_rule": p["exit_rule"], "month_return": _ratio(exit_price, start_price),
            "month_spy": _ratio(spy_end, spy_start),
            "month_pnl": p["shares"] * (exit_price[1] - start_price[1]) if exit_price and start_price else None,
            "since_buy": _ratio(exit_price, entry),
            "since_buy_spy": _ratio(spy_end, _close(conn, BENCHMARK, p["buy_date"])),
            "alert": None if not p["trade_id"] else {
                "member": p["member"], "symbol": p["source_symbol"], "sent": p["alert_sent"],
                "score": p["alert_score"], "suggested_exit": p["suggested_exit"], "d0_date": p["d0_date"],
                "entry_vs_d0_open": p["buy_price"] / p["d0_open"] - 1 if p["d0_open"] else None,
                "trading_days_after_d0": d0_gap,
                "system_ret_20": p["system_ret_20"], "system_abn_20": p["system_abn_20"],
            },
        })
    return out


def _system_book(conn: sqlite3.Connection, start: date, end: date) -> list[dict]:
    """Buy alerts sent in the month, one entry per filing, from the D0 open."""
    lo = f"{start.isoformat()}T00:00:00Z"
    hi = f"{(end + timedelta(days=1)).isoformat()}T00:00:00Z"
    book = []
    for f in rows(conn, """
            SELECT t.doc_id, m.name AS member, GROUP_CONCAT(DISTINCT t.symbol) AS symbols, MAX(a.score) AS score,
                   MIN(o.d0_date) AS d0_date, AVG(o.ret_20) AS ret_20, AVG(o.abn_ret_20) AS abn_20,
                   COUNT(o.ret_20) AS matured, COUNT(*) AS n_trades,
                   MAX(EXISTS (SELECT 1 FROM my_positions p WHERE p.trade_id = t.trade_id)) AS followed
            FROM alerts a JOIN trades t ON t.trade_id = a.trade_id
            LEFT JOIN trade_outcomes o ON o.trade_id = t.trade_id AND o.copyable = 1
            LEFT JOIN members m ON m.member_id = t.member_id
            WHERE a.rule = 'watchlist_buy' AND a.sent_at >= ? AND a.sent_at < ?
            GROUP BY t.doc_id, m.name ORDER BY MIN(a.sent_at)
            """, (lo, hi)):
        to_date = None
        if f["matured"] < f["n_trades"] and f["d0_date"]:  # not matured: an approximate return to date
            moves = []
            for sym in (f["symbols"] or "").split(","):
                d0 = conn.execute("SELECT open FROM prices WHERE ticker = ? AND date = ?", (sym, f["d0_date"])
                                  ).fetchone()
                last = _close(conn, sym, end.isoformat())
                if d0 and d0[0] and last:
                    moves.append(last[1] / d0[0] - 1)
            to_date = sum(moves) / len(moves) if moves else None
        book.append({**f, "followed": bool(f["followed"]), "to_date_approx": to_date})
    return book


def facts(conn: sqlite3.Connection, month: str) -> dict:
    start, end = month_bounds(month)
    before = (start - timedelta(days=1)).isoformat()
    first_position = conn.execute("SELECT MIN(buy_date) FROM my_positions").fetchone()[0]
    alerts_start = report.marker(conn, ALERTS_START)[0]
    since = first_position or (alerts_start[:10] if alerts_start else None)
    positions = _positions(conn, start, end)
    book = _system_book(conn, start, end)
    pnl = [p["month_pnl"] for p in positions if p["month_pnl"] is not None]
    matured = [b for b in book if b["abn_20"] is not None]
    return {
        "month": month, "start": start.isoformat(), "end": end.isoformat(),
        "positions": positions,
        "totals": {
            "month_pnl": sum(pnl) if pnl else None,
            "positions": len(positions), "sold": sum(p["status"] == "sold" for p in positions),
            "followed_alerts": sum(b["followed"] for b in book), "alerts": len(book),
            "system_mean_abn_20": sum(b["abn_20"] for b in matured) / len(matured) if matured else None,
            "system_matured": len(matured),
        },
        "system_book": book,
        "spy": {
            "month": _ratio(_close(conn, BENCHMARK, end.isoformat()), _close(conn, BENCHMARK, before)),
            "since": since,
            "since_start": _ratio(_close(conn, BENCHMARK, end.isoformat()), _close(conn, BENCHMARK, since))
            if since else None,
        },
    }


# --- rendering --------------------------------------------------------------------------------------------------


def render(f: dict) -> str:
    out = [f"MONTH {f['month']} ({f['start']} to {f['end']})"]
    t, spy = f["totals"], f["spy"]
    out += ["", "SUMMARY"]
    out.append(f"  S&P 500 this month: {pct(spy['month'])}" + (
        f"; since {spy['since']}: {pct(spy['since_start'])}" if spy["since"] else ""))
    if t["positions"]:
        out.append(f"  Your positions: {t['positions']} held ({t['sold']} sold this month); month P&L "
                   + (f"${t['month_pnl']:+,.0f}" if t["month_pnl"] is not None else "n/a"))
    else:
        out.append("  Your positions: none logged for this month (log buys on the Positions page).")
    out.append(f"  Buy alerts: {t['alerts']} filing(s), {t['followed_alerts']} followed; system 20-day excess "
               f"{pct(t['system_mean_abn_20'])} over {t['system_matured']} matured filing(s)")

    out += ["", "YOUR TRADES"]
    if not f["positions"]:
        out.append("  None.")
    for p in f["positions"]:
        state = f"sold {p['sell_date']}" if p["status"] == "sold" else "open"
        out.append(f"- {p['ticker']} {p['shares']:g} @ ${p['buy_price']:,.2f} ({p['buy_date']}, {state}): "
                   f"month {pct(p['month_return'])} vs S&P 500 {pct(p['month_spy'])}"
                   + (f", P&L ${p['month_pnl']:+,.0f}" if p["month_pnl"] is not None else "")
                   + f"; since buy {pct(p['since_buy'])} vs {pct(p['since_buy_spy'])}")
        a = p["alert"]
        if a:
            late = a["trading_days_after_d0"]
            timing = (f"bought {late} trading day(s) after D0" if late else "bought on D0") if late is not None else ""
            out.append(f"    From {a['member']}'s {a['symbol']} alert (score {a['score'] or '?'}); {timing}, "
                       f"{pct(a['entry_vs_d0_open'])} vs the D0 open; system 20-day result "
                       f"{pct(a['system_ret_20'])} ({pct(a['system_abn_20'])} vs S&P 500)")
            if a["suggested_exit"]:
                out.append(f"    Suggested exit at the time: {a['suggested_exit']}")
        elif p["exit_rule"]:
            out.append(f"    Not linked to an alert; exit rule: {p['exit_rule']}")

    out += ["", "THE SYSTEM'S ALERTS (bought at the D0 open, per filing)"]
    if not f["system_book"]:
        out.append("  No buy alerts this month.")
    for b in f["system_book"]:
        result = (f"20d {pct(b['ret_20'])}, {pct(b['abn_20'])} vs S&P 500" if b["abn_20"] is not None
                  else f"to date ~{pct(b['to_date_approx'])} (not matured)")
        out.append(f"- {b['member']}: {b['symbols']} (D0 {b['d0_date'] or '?'}, score {b['score'] or '?'}): {result}"
                   + (" [followed]" if b["followed"] else ""))
    return "\n".join(out)


# --- the run ----------------------------------------------------------------------------------------------------


@dataclass
class Summary:
    month: str | None = None
    run_id: int | None = None
    narrative: bool = False
    fallback_reason: str | None = None
    text: str = field(default="", repr=False)


def run(
    conn: sqlite3.Connection,
    *,
    month: str | None = None,
    backend: str = runner.CLAUDE_CODE,
    template_only: bool = False,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    agent_runner: Callable[..., runner.RunResult] = runner.run_agent,
) -> Summary:
    moment = now()
    month = month or due(conn) or previous_month(moment.date())
    f = facts(conn, month)
    written = report.write(
        conn, agent=AGENT, facts=f, text=render(f), instructions=INSTRUCTIONS,
        task="Write this month's journal review as instructed.", inputs={"month": month},
        stamp=moment.strftime("%Y-%m-%dT%H:%M:%SZ"), backend=backend, effort="high", max_turns=15, timeout=600,
        template_only=template_only, dry_run=dry_run, agent_runner=agent_runner)
    if not dry_run:
        report.set_marker(conn, SOURCE, max(month, report.marker(conn, SOURCE)[0] or ""))
    return Summary(month, written.run_id, written.narrative, written.fallback_reason, written.text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--month", help="YYYY-MM (default: the month due, else last month)")
    parser.add_argument("--dry-run", action="store_true", help="print the review; write nothing")
    parser.add_argument("--template", action="store_true", help="no model: the fact sections only")
    parser.add_argument("--api", action="store_true", help="use the Anthropic API instead of Claude Code")
    args = parser.parse_args(argv)
    logs.setup()
    s = run(connect(), month=args.month, backend=runner.API if args.api else runner.CLAUDE_CODE,
            template_only=args.template, dry_run=args.dry_run)
    print(s.text)
    if s.run_id:
        print(f"\nLogged as agent run {s.run_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
