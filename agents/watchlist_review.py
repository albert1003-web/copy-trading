"""Rule-based watchlist review: proposes watchlist changes from the leaderboard, with no model (free).

    python -m agents.watchlist_review            # the nightly pipeline runs this after the leaderboard
    python -m agents.watchlist_review --dry-run  # print what it would propose

It writes an agent run with proposals only when there is something new to propose, and you approve each one in the
app like any agent's.
- add: a member in the leaderboard's top TOP_N (ranked = at least 20 filings), shrunk 20-day excess vs the
  S&P 500 of at least MIN_SCORE, and a hit rate of at least MIN_HIT_RATE, who isn't on the watchlist;
- remove: a watched member who is ranked and whose shrunk score is negative. An unranked member (too few filings)
  is never proposed for removal: there isn't enough evidence either way.
A proposal isn't repeated while one for the same member and change is pending or approved but not yet applied,
or for REJECT_COOLDOWN_DAYS after you reject it.
"""

import argparse
import logging
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agents import proposals as props
from agents import runner
from common import log as logs
from db import connect

log = logging.getLogger(__name__)

AGENT = "watchlist_review"
TOP_N = 5
MIN_SCORE = 0.005  # +0.5% shrunk 20-day excess over the S&P 500
MIN_HIT_RATE = 0.5
REJECT_COOLDOWN_DAYS = 90

SOURCE_SQL = (
    "SELECT rank, n_filings, shrunk_score, mean_abn_ret, hit_rate, consistency FROM member_scores "
    "WHERE member_id = '{member_id}' AND as_of = '{as_of}'"
)


@dataclass
class Summary:
    run_id: int | None = None
    as_of: str | None = None
    proposals: list[props.Proposal] = field(default_factory=list)
    skipped: int = 0  # already pending, or rejected recently


def candidates(conn: sqlite3.Connection) -> tuple[str | None, list[props.Proposal]]:
    """The latest leaderboard snapshot's date and the proposals the rules give, before de-duplication."""
    as_of = conn.execute("SELECT MAX(as_of) FROM member_scores").fetchone()[0]
    if as_of is None:
        return None, []
    rows = conn.execute(
        """
        SELECT s.member_id, m.name, s.rank, s.n_filings, s.shrunk_score, s.mean_abn_ret, s.hit_rate, s.consistency,
               COALESCE(w.active, 0) AS watched,
               (SELECT COUNT(*) FROM member_scores r WHERE r.as_of = s.as_of AND r.rank IS NOT NULL) AS n_ranked
        FROM member_scores s
        JOIN members m ON m.member_id = s.member_id
        LEFT JOIN watchlist w ON w.member_id = s.member_id
        WHERE s.as_of = ? AND s.rank IS NOT NULL
        ORDER BY s.rank
        """,
        (as_of,),
    ).fetchall()
    out = []
    for r in rows:
        member_id, name, rank, n, score, mean, hit, consistency, watched, n_ranked = r
        if not watched and rank <= TOP_N and score >= MIN_SCORE and (hit or 0) >= MIN_HIT_RATE:
            out.append(props.Proposal(
                kind=props.WATCHLIST_ADD, member_id=member_id, title=f"Add {name} to the watchlist",
                rationale=f"Meets the add rule: top {TOP_N} on the leaderboard, shrunk 20-day excess of at least "
                f"{MIN_SCORE:+.1%} and a hit rate of at least {MIN_HIT_RATE:.0%}. Free price data lacks delisted "
                "tickers, so treat the numbers as indicative.",
                evidence=_evidence(member_id, as_of, rank, n_ranked, n, score, mean, hit, consistency),
            ))
        elif watched and score < 0:
            out.append(props.Proposal(
                kind=props.WATCHLIST_REMOVE, member_id=member_id, title=f"Remove {name} from the watchlist",
                rationale=f"Ranked, and the shrunk 20-day excess is negative ({score:+.2%}): copying their buys "
                "from disclosure has trailed the S&P 500 after shrinkage.",
                evidence=_evidence(member_id, as_of, rank, n_ranked, n, score, mean, hit, consistency),
            ))
    return as_of, out


def _evidence(member_id, as_of, rank, n_ranked, n, score, mean, hit, consistency) -> list[dict]:
    source = SOURCE_SQL.format(member_id=member_id, as_of=as_of)
    claims = [
        f"Ranked #{rank} of {n_ranked} on the {as_of} leaderboard, over {n} filings (20 trading days from D0)",
        f"Shrunk 20-day excess vs the S&P 500 {score:+.2%} (raw mean {mean:+.2%}); beat the S&P 500 in "
        f"{hit:.0%} of filings" + (f"; positive in {consistency:.0%} of years" if consistency is not None else ""),
    ]
    return [{"claim": c, "source": source} for c in claims]


def _already_proposed(conn: sqlite3.Connection, p: props.Proposal, cutoff: str) -> bool:
    return conn.execute(
        """
        SELECT 1 FROM agent_proposals
        WHERE kind = ? AND member_id = ?
          AND ((applied_at IS NULL AND (approved IS NULL OR approved = 1)) OR (approved = 0 AND decided_at >= ?))
        """,
        (p.kind, p.member_id, cutoff),
    ).fetchone() is not None


def run(conn: sqlite3.Connection, *, dry_run: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Summary:
    as_of, found = candidates(conn)
    cutoff = (now() - timedelta(days=REJECT_COOLDOWN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = [p for p in found if not _already_proposed(conn, p, cutoff)]
    summary = Summary(as_of=as_of, proposals=fresh, skipped=len(found) - len(fresh))
    if not fresh or dry_run:
        return summary

    adds = sum(p.kind == props.WATCHLIST_ADD for p in fresh)
    text = (f"Watchlist review of the {as_of} leaderboard: {adds} to add, {len(fresh) - adds} to remove. "
            f"Rules: add a top-{TOP_N} member with a shrunk 20-day excess of at least {MIN_SCORE:+.1%} and a hit rate "
            f"of at least {MIN_HIT_RATE:.0%}; remove a ranked watched member whose score is negative.")
    inputs = {"leaderboard_as_of": as_of, "top_n": TOP_N, "min_score": MIN_SCORE, "min_hit_rate": MIN_HIT_RATE,
              "reject_cooldown_days": REJECT_COOLDOWN_DAYS}
    stamp = lambda: now().strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    summary.run_id = runner.start_run(conn, AGENT, inputs, None, stamp)
    runner.finish_run(conn, summary.run_id, text, fresh, now=stamp)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the proposals; write nothing")
    args = parser.parse_args(argv)
    logs.setup()
    s = run(connect(), dry_run=args.dry_run)
    if s.as_of is None:
        print("No leaderboard yet (python -m analytics.leaderboard).")
        return 0
    for p in s.proposals:
        print(f"{p.kind}: {p.title}\n  " + "\n  ".join(e["claim"] for e in p.evidence))
    print(f"{len(s.proposals)} new proposal(s), {s.skipped} already proposed or recently rejected"
          + (f"; logged as agent run {s.run_id}" if s.run_id else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
