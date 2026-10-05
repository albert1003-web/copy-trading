"""Carries out agent proposals a human approved in the app. A plain pipeline stage, not an agent.

    python -m agents.apply        # the pipeline runs this every pass, before alerts

Only watchlist proposals change anything; an approved note is just acknowledged. Each proposal is handled once:
applied_at is set even when it fails (with the reason in apply_result), so a bad one isn't retried forever.
"""

import logging
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agents.proposals import NOTE, WATCHLIST_ADD, WATCHLIST_REMOVE
from common import log as logs
from db import connect

log = logging.getLogger(__name__)


@dataclass
class ApplySummary:
    applied: int = 0
    acknowledged: int = 0
    failed: list[str] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(conn: sqlite3.Connection, now: Callable[[], str] = utc_now) -> ApplySummary:
    summary = ApplySummary()
    pending = conn.execute(
        "SELECT proposal_id, run_id, kind, member_id, title FROM agent_proposals "
        "WHERE approved = 1 AND applied_at IS NULL ORDER BY proposal_id"
    ).fetchall()
    for proposal_id, run_id, kind, member_id, title in pending:
        stamp = now()
        try:
            result = _apply(conn, kind, member_id, f"agent run {run_id}: {title}", stamp)
        except sqlite3.Error as e:
            conn.rollback()
            result = f"failed: {e}"
        if result.startswith("failed"):
            summary.failed.append(f"proposal {proposal_id} ({title}): {result}")
            log.warning("Proposal %d not applied: %s", proposal_id, result)
        elif result == "acknowledged":
            summary.acknowledged += 1
        else:
            summary.applied += 1
            log.info("Applied proposal %d: %s %s", proposal_id, kind, member_id)
        conn.execute(
            "UPDATE agent_proposals SET applied_at = ?, apply_result = ? WHERE proposal_id = ?",
            (stamp, result, proposal_id),
        )
        conn.commit()
    return summary


def _apply(conn: sqlite3.Connection, kind: str, member_id: str | None, reason: str, stamp: str) -> str:
    if kind == NOTE:
        return "acknowledged"
    if kind == WATCHLIST_ADD:
        # The same upsert as the app's watchlist form (MemberRepository.watch).
        conn.execute(
            "INSERT INTO watchlist (member_id, added_at, reason, active) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(member_id) DO UPDATE SET active = 1, added_at = excluded.added_at, reason = excluded.reason",
            (member_id, stamp, reason),
        )
        return "applied"
    if kind == WATCHLIST_REMOVE:
        changed = conn.execute(
            "UPDATE watchlist SET active = 0 WHERE member_id = ? AND active = 1", (member_id,)
        ).rowcount
        return "applied" if changed else "failed: not on the watchlist any more"
    return f"failed: unknown kind {kind!r}"


def main() -> int:
    logs.setup()
    s = run(connect())
    log.info("Agent proposals: %d applied, %d acknowledged, %d failed", s.applied, s.acknowledged, len(s.failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
