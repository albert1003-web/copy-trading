"""Weekly strategy review (M5.5): leaderboard and backtest changes, how the alerts have done, and watchlist /
exit-rule proposals backed by evidence. On the Agents page; proposals are approved there one by one.

    python -m agents.strategist                # the pipeline runs this once a week, after Friday's nightly stages
    python -m agents.strategist --dry-run      # print it; write nothing
    python -m agents.strategist --template     # no model: the fact sections only

Facts (SQL, rendered by code): the leaderboard now vs about a week earlier, alert performance from D0 (one
observation per filing), the exit-rule recommendation and the walk-forward books vs last week's review (kept in
that run's inputs.snapshot), the watchlist, pending proposals and data caveats. Claude (Claude Code by default)
writes the review on top and may propose watchlist changes (validated: known member, evidence, not already
watched / proposed) and exit-rule ideas as notes. Template fallback as for the digest (agents/report.py).
"""

import argparse
import json
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

AGENT = "strategy_analyst"
SOURCE = "agents.weekly"  # checked_at = the ISO week reviewed, e.g. 2026-W40
TOP = 15
LOOKBACK_DAYS = 7
RECENT_DAYS = 90
HORIZONS = (5, 20)

INSTRUCTIONS = """\
You write the owner's weekly strategy review. The user message holds this week's facts as JSON; the owner also
sees them as tables right below what you write, so interpret rather than repeat them.
Write at most about 400 words of plain text: what changed on the leaderboard and why it matters, whether the
alerts are working (from D0, per filing, vs the S&P 500), the exit-rule picture, and the risks (small samples,
one filing driving a result, survivorship bias in free price data). Query the database to check anything before
you rely on it.
Proposals: watchlist_add / watchlist_remove only for ranked members (at least 20 filings) where the leaderboard
evidence clearly supports it, each claim citing the SQL you ran; don't repeat a pending proposal. Exit-rule ideas
are `note` proposals (the exit rules are recomputed nightly and can't be changed directly). No proposals is fine."""


def week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def target_week(nightly: date) -> str:
    """The week whose Friday is the latest one on or before the last nightly run."""
    friday = nightly - timedelta(days=(nightly.weekday() - 4) % 7)
    return week_key(friday)


def due(conn: sqlite3.Connection) -> str | None:
    """The ISO week to review, or None: once a week, after that week's Friday nightly stages."""
    nightly = report.nightly_day(conn)
    if not nightly:
        return None
    week = target_week(date.fromisoformat(nightly))
    done, _ = report.marker(conn, SOURCE)
    return week if done is None or done < week else None


# --- facts ------------------------------------------------------------------------------------------------------


def _leaderboard(conn: sqlite3.Connection) -> dict:
    latest = conn.execute("SELECT MAX(as_of) FROM member_scores").fetchone()[0]
    if latest is None:
        return {"as_of": None}
    cutoff = (date.fromisoformat(latest) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    before = conn.execute("SELECT MAX(as_of) FROM member_scores WHERE as_of <= ?", (cutoff,)).fetchone()[0]
    sql = """SELECT s.member_id, m.name, s.rank, s.shrunk_score, s.mean_abn_ret, s.hit_rate, s.n_filings,
                    s.consistency, COALESCE(w.active, 0) AS watched
             FROM member_scores s JOIN members m USING (member_id) LEFT JOIN watchlist w USING (member_id)
             WHERE s.as_of = ?"""
    now = {r["member_id"]: r for r in rows(conn, sql, (latest,))}
    then = {r["member_id"]: r for r in rows(conn, sql, (before,))} if before else {}

    def entry(k):
        r, b = now[k], then.get(k, {})
        return {**{x: r[x] for x in ("member_id", "name", "rank", "shrunk_score", "mean_abn_ret", "hit_rate",
                                       "n_filings", "consistency")},
                "watched": bool(r["watched"]), "rank_before": b.get("rank"), "score_before": b.get("shrunk_score")}

    ranked = sorted((k for k, r in now.items() if r["rank"] is not None), key=lambda k: now[k]["rank"])
    return {
        "as_of": latest, "compared_with": before,
        "top": [entry(k) for k in ranked[:TOP]],
        "newly_ranked": [entry(k) for k in ranked if then and then.get(k, {}).get("rank") is None],
        "dropped": [{"name": then[k]["name"], "rank_before": then[k]["rank"]} for k in then
                    if then[k]["rank"] is not None and (k not in now or now[k]["rank"] is None)],
        "watched": [entry(k) for k, r in now.items() if r["watched"]],
        "n_ranked": len(ranked),
    }


def _alert_performance(conn: sqlite3.Connection, today: date) -> dict:
    """Alerted copyable buys from D0, one observation per filing (its trades averaged). Only filings detected live
    count: an alert for a backfilled filing went out long after D0, so it says nothing about the alerts."""
    out = {}
    recent = (today - timedelta(days=RECENT_DAYS)).isoformat()
    for h in HORIZONS:
        filings = rows(conn, f"""
            SELECT t.doc_id, MIN(o.d0_date) AS d0, AVG(o.abn_ret_{h}) AS abn
            FROM alerts a JOIN trades t ON t.trade_id = a.trade_id JOIN trade_outcomes o ON o.trade_id = t.trade_id
            JOIN filings f ON f.doc_id = t.doc_id
            WHERE a.rule = 'watchlist_buy' AND o.copyable = 1 AND o.abn_ret_{h} IS NOT NULL
              AND f.available_basis = 'seen'  -- live alerts only: a backfilled filing's alert came long after D0
            GROUP BY t.doc_id""")
        for label, subset in (("all", filings), ("recent", [f for f in filings if f["d0"] >= recent])):
            n = len(subset)
            out[f"{label}_{h}d"] = {
                "filings": n,
                "mean_abn_ret": sum(f["abn"] for f in subset) / n if n else None,
                "hit_rate": sum(f["abn"] > 0 for f in subset) / n if n else None,
            }
    week_ago = (datetime.combine(today, datetime.min.time(), UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out["this_week"] = rows(conn, """
        SELECT m.name AS member, GROUP_CONCAT(DISTINCT t.symbol) AS symbols, MAX(a.score) AS score,
               MIN(a.sent_at) AS sent
        FROM alerts a JOIN trades t ON t.trade_id = a.trade_id LEFT JOIN members m ON m.member_id = t.member_id
        WHERE a.sent_at >= ? AND a.rule = 'watchlist_buy' GROUP BY t.doc_id, m.name ORDER BY sent""", (week_ago,))
    return out


def _exits(conn: sqlite3.Connection) -> dict:
    rec = rows(conn, "SELECT label, description, confidence, reason, train_window, train_score FROM exit_rules "
                     "WHERE recommended = 1")
    books = rows(conn, """
        SELECT e.book, e.test_window, e.n_filings, e.mean_abn_ret, e.hit_rate, e.total_return, e.spy_return,
               e.max_drawdown
        FROM exit_backtests e
        JOIN (SELECT book, MAX(run_id) AS run_id FROM exit_backtests GROUP BY book) last ON last.run_id = e.run_id
        ORDER BY e.book""")  # each book's last row is its whole-span ('all') row
    return {"recommended": rec[0] if rec else None, "books": books}


def _previous_snapshot(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT inputs FROM agent_runs WHERE agent = ? AND status = 'ok' ORDER BY run_id DESC LIMIT 1",
                       (AGENT,)).fetchone()
    try:
        return json.loads(row[0]).get("snapshot") if row and row[0] else None
    except json.JSONDecodeError:
        return None


def snapshot(f: dict) -> dict:
    """What next week's review compares against."""
    rec = f["exits"]["recommended"]
    return {
        "week": f["week"],
        "recommended": {"label": rec["label"], "confidence": rec["confidence"]} if rec else None,
        "books": {b["book"]: b["mean_abn_ret"] for b in f["exits"]["books"]},
        "leaderboard_as_of": f["leaderboard"]["as_of"],
    }


def facts(conn: sqlite3.Connection, week: str, today: date) -> dict:
    coverage = {r["status"]: r["n"]
                for r in rows(conn, "SELECT status, COUNT(*) AS n FROM price_coverage GROUP BY status")}
    scanned = conn.execute("SELECT COUNT(*) FROM filings WHERE doc_format = 'scanned' AND parse_method IS NULL"
                           ).fetchone()[0]
    return {
        "week": week,
        "leaderboard": _leaderboard(conn),
        "alerts": _alert_performance(conn, today),
        "exits": _exits(conn),
        "previous": _previous_snapshot(conn),
        "watchlist": rows(conn, """SELECT m.member_id, m.name, w.added_at, w.reason FROM watchlist w
                                   JOIN members m USING (member_id) WHERE w.active = 1 ORDER BY m.name"""),
        "pending_proposals": rows(conn, """
            SELECT p.kind, p.member_id, p.title, r.agent FROM agent_proposals p JOIN agent_runs r USING (run_id)
            WHERE p.approved IS NULL AND p.applied_at IS NULL ORDER BY p.proposal_id"""),
        "caveats": {"price_coverage": coverage, "scanned_unread": scanned,
                    "survivorship": "free price data lacks delisted tickers"},
    }


# --- rendering --------------------------------------------------------------------------------------------------


def render(f: dict) -> str:
    out = [f"WEEK {f['week']}"]
    lb = f["leaderboard"]
    out += ["", "LEADERBOARD (20-day excess vs the S&P 500 from D0, shrunk)"]
    if lb["as_of"] is None:
        out.append("No leaderboard yet.")
    else:
        since = f" vs {lb['compared_with']}" if lb["compared_with"] else " (no snapshot a week earlier)"
        out.append(f"As of {lb['as_of']}{since}; {lb['n_ranked']} ranked members (20+ filings).")
        for m in lb["top"]:
            moved = ""
            if lb["compared_with"]:
                moved = " (new)" if m["rank_before"] is None else (
                    f" (was #{m['rank_before']})" if m["rank_before"] != m["rank"] else "")
            out.append(f"  #{m['rank']:<3} {m['name']:<28} score {pct(m['shrunk_score'], 2)}{moved}; "
                       f"mean {pct(m['mean_abn_ret'])}, hit {m['hit_rate']:.0%}, {m['n_filings']} filings"
                       + (" [watched]" if m["watched"] else ""))
        for m in lb["dropped"]:
            out.append(f"  Dropped out of the ranking: {m['name']} (was #{m['rank_before']})")
        for m in lb["watched"]:
            if m["rank"] is None:
                out.append(f"  Watched, unranked: {m['name']} ({m['n_filings']} filings, score "
                           f"{pct(m['shrunk_score'], 2)})")

    a = f["alerts"]
    out += ["", "ALERT PERFORMANCE (live-detected alerted buys, per filing, from D0 vs the S&P 500)"]
    for h in HORIZONS:
        for label, name in (("all", "all time"), ("recent", f"last {RECENT_DAYS} days")):
            s = a[f"{label}_{h}d"]
            if s["filings"]:
                out.append(f"  {h}d, {name}: {s['filings']} filings, mean {pct(s['mean_abn_ret'])}, "
                           f"beat the S&P 500 in {s['hit_rate']:.0%}")
            else:
                out.append(f"  {h}d, {name}: no matured filings yet")
    out.append(f"  This week: {len(a['this_week'])} buy alert email(s)"
               + "".join(f"\n    {x['member']}: {x['symbols']} (score {x['score']:.0f})" for x in a["this_week"]))

    ex, prev = f["exits"], f["previous"] or {}
    out += ["", "EXITS (walk-forward, out of sample)"]
    rec = ex["recommended"]
    if rec:
        was = prev.get("recommended")
        changed = (f" (last week: {was['label']}, {was['confidence']})"
                   if was and (was["label"], was["confidence"]) != (rec["label"], rec["confidence"]) else "")
        out.append(f"  Recommended: {rec['description']} [{rec['confidence']} confidence]{changed}")
    else:
        out.append("  No exit-rule recommendation yet.")
    for b in ex["books"]:
        before = (prev.get("books") or {}).get(b["book"])
        delta = f" (last week {pct(before, 2)})" if before is not None and before != b["mean_abn_ret"] else ""
        out.append(f"  {b['book']:<13} {b['n_filings']} filings, mean excess {pct(b['mean_abn_ret'], 2)}{delta}, "
                   f"hit {b['hit_rate']:.0%}")

    out += ["", "WATCHLIST"]
    out += [f"  {w['name']} (since {w['added_at'][:10]})" for w in f["watchlist"]] or ["  Empty."]
    if f["pending_proposals"]:
        out += ["", f"PENDING PROPOSALS ({len(f['pending_proposals'])})"]
        out += [f"  {p['title']} ({p['agent']})" for p in f["pending_proposals"]]
    c = f["caveats"]
    cov = c["price_coverage"]
    out += ["", "CAVEATS",
            f"  Price coverage: {cov.get('ok', 0)} ok, {cov.get('partial', 0)} partial, {cov.get('missing', 0)} "
            f"missing symbols; {c['survivorship']}.",
            f"  {c['scanned_unread']} scanned filings not yet read (not in any stats)."]
    return "\n".join(out)


# --- the run ----------------------------------------------------------------------------------------------------


@dataclass
class Summary:
    week: str | None = None
    run_id: int | None = None
    narrative: bool = False
    proposals: int = 0
    fallback_reason: str | None = None
    text: str = field(default="", repr=False)


def run(
    conn: sqlite3.Connection,
    *,
    week: str | None = None,
    backend: str = runner.CLAUDE_CODE,
    template_only: bool = False,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    agent_runner: Callable[..., runner.RunResult] = runner.run_agent,
) -> Summary:
    moment = now()
    nightly = report.nightly_day(conn)
    week = week or due(conn) or target_week(date.fromisoformat(nightly) if nightly else moment.date())
    f = facts(conn, week, moment.date())
    written = report.write(
        conn, agent=AGENT, facts=f, text=render(f), instructions=INSTRUCTIONS,
        task="Write this week's strategy review as instructed.", inputs={"week": week, "snapshot": snapshot(f)},
        stamp=moment.strftime("%Y-%m-%dT%H:%M:%SZ"), backend=backend, effort="high", max_turns=25, timeout=900,
        template_only=template_only, dry_run=dry_run, agent_runner=agent_runner)
    if not dry_run:
        report.set_marker(conn, SOURCE, max(week, report.marker(conn, SOURCE)[0] or ""))
    return Summary(week, written.run_id, written.narrative, len(written.proposals), written.fallback_reason,
                   written.text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the review; write nothing")
    parser.add_argument("--template", action="store_true", help="no model: the fact sections only")
    parser.add_argument("--api", action="store_true", help="use the Anthropic API instead of Claude Code")
    args = parser.parse_args(argv)
    logs.setup()
    s = run(connect(), backend=runner.API if args.api else runner.CLAUDE_CODE, template_only=args.template,
            dry_run=args.dry_run)
    print(s.text)
    if s.run_id:
        print(f"\nLogged as agent run {s.run_id}" + (f" with {s.proposals} proposal(s)." if s.proposals else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
