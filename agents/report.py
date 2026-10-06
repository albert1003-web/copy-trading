"""The pattern shared by the scheduled reports (daily digest, weekly strategy review, monthly journal review).

Code gathers the facts with SQL and renders them; Claude writes a narrative on top (and, for the weekly review,
proposals). The run's output is narrative + rendered facts, so every number is exact even if the model errs. If
the narrative fails, the report is written from the rendered facts alone (a second, model-less run; the failed
run stays logged), so a scheduled report is never lost. Each report keeps its place in source_state.
"""

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from agents import runner
from agents.proposals import Proposal
from db import connect

log = logging.getLogger(__name__)

NIGHTLY_SOURCE = "pipeline.nightly"  # pipeline/run.py: the last weekday whose nightly stages succeeded


@dataclass
class Written:
    run_id: int | None = None
    text: str = ""
    narrative: bool = False
    fallback_reason: str | None = None
    proposals: list[Proposal] = field(default_factory=list)


def write(
    conn: sqlite3.Connection,
    *,
    agent: str,
    facts: dict,
    text: str,
    instructions: str,
    task: str,
    inputs: dict,
    stamp: str,
    backend: str = runner.CLAUDE_CODE,
    effort: str = "medium",
    max_turns: int = 8,
    timeout: float | None = None,
    template_only: bool = False,
    dry_run: bool = False,
    agent_runner: Callable[..., runner.RunResult] = runner.run_agent,
) -> Written:
    """Writes one report run (or prints nothing and writes nothing with dry_run, apart from the model call)."""
    out = Written(text=text)
    if not template_only:
        prompt = f"Facts (JSON):\n{json.dumps(facts, default=str)}\n\n{task}"
        log_conn = connect(":memory:") if dry_run else conn
        result = agent_runner(log_conn, agent=agent, prompt=prompt, instructions=instructions, web_search_uses=0,
                              effort=effort, max_turns=max_turns, timeout=timeout, backend=backend,
                              extra_inputs=inputs)
        if result.status == "ok" and result.summary.strip():
            out.narrative, out.proposals = True, result.proposals
            out.text = result.summary.strip() + "\n\n" + text
            if not dry_run:
                out.run_id = result.run_id
                conn.execute("UPDATE agent_runs SET output = ? WHERE run_id = ?", (out.text, result.run_id))
                conn.commit()
        else:
            out.fallback_reason = result.error or "the narrative came back empty"
            log.warning("%s narrative failed (%s); writing it from the template", agent, out.fallback_reason)

    if not out.narrative:
        out.text = text if template_only else f"(Written from the template: {out.fallback_reason})\n\n{text}"
        if not dry_run:
            fallback = {**inputs, "template_only": template_only, "fallback_reason": out.fallback_reason}
            out.run_id = runner.start_run(conn, agent, fallback, None, lambda: stamp)
            runner.finish_run(conn, out.run_id, out.text, [], now=lambda: stamp)
    return out


def marker(conn: sqlite3.Connection, source: str) -> tuple[str | None, str | None]:
    """(checked_at, changed_at) of a report's source_state row."""
    row = conn.execute("SELECT checked_at, changed_at FROM source_state WHERE source = ?", (source,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def set_marker(conn: sqlite3.Connection, source: str, checked_at: str, changed_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO source_state (source, checked_at, changed_at) VALUES (?, ?, ?) "
        "ON CONFLICT (source) DO UPDATE SET checked_at = excluded.checked_at, changed_at = excluded.changed_at",
        (source, checked_at, changed_at),
    )
    conn.commit()


def nightly_day(conn: sqlite3.Connection) -> str | None:
    """The last weekday (ISO date) whose nightly stages succeeded."""
    return marker(conn, NIGHTLY_SOURCE)[0] or None


def rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    cursor = conn.execute(sql, params)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def pct(x: float | None, places: int = 1) -> str:
    return "n/a" if x is None else f"{x:+.{places}%}"
