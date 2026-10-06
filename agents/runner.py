"""One agent run: a Claude tool-use loop over the read-only DB tools (plus web search), logged to agent_runs.

The run row is written before the first API call and updated every turn, so a crash still leaves an audit trail.
The final answer is structured output (agents/proposals.OUTPUT_SCHEMA); its proposals go to agent_proposals for
a human to approve. The agent itself only ever reads: the writable connection is used here, never by a tool.
"""

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from agents import proposals as props
from agents import tools

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
MAX_TURNS = 20
# On a safety-classifier refusal, the API re-runs the request on a fallback model it picks.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
ET = ZoneInfo("America/New_York")

BASE_SYSTEM = """\
You are an analyst for a personal Congressional Trade Tracker. It watches stock-trade disclosures (PTRs) by
members of Congress and measures how copying them would have done *after they became public*. The owner trades
by hand in a Roth IRA (no shorting, margin or options) and decides everything; you inform and propose.

You have read-only SQL access to the tracker's SQLite database (list_tables, describe_table, query), and may have
web search. Describe a table before you first query it: the column comments define the units.

Definitions (use them exactly):
- D0: the first trading-day open after a filing became available to us (filings.available_at). Every tradeable
  metric starts at D0. Returns from the trade date (trade_outcomes.tx_ret / tx_abn_ret) are context only: never
  use them to judge whether a member or trade is worth copying.
- Return(h): adjusted close at D0 + h trading days / D0 open - 1, h in {1, 5, 10, 20, 60}.
  Abnormal return: Return(h) minus SPY's return over the same window. Win: abnormal return > 0.
- The unit of evidence is the filing, not the trade: a filing's trades share D0 and market moves.
- The leaderboard (member_scores, member_horizon_stats) uses empirical-Bayes shrunk scores and ranks only members
  with at least 20 filings. Don't treat a handful of lucky trades as skill.
- Free price data lacks delisted tickers (survivorship bias); say so when it matters.

Be concrete and brief. Prefer numbers from the database over general claims, and say when a sample is small."""


@dataclass
class RunResult:
    run_id: int
    status: str  # ok | failed
    summary: str = ""
    proposals: list[props.Proposal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class AgentError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Trace:
    calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {
        "turns": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "web_search_requests": 0, "models": [],
    })

    def add_usage(self, response) -> None:
        u, total = response.usage, self.usage
        total["turns"] += 1
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            total[key] += getattr(u, key, None) or 0
        server = getattr(u, "server_tool_use", None)
        total["web_search_requests"] += (getattr(server, "web_search_requests", None) or 0) if server else 0
        if response.model not in total["models"]:
            total["models"].append(response.model)


API = "api"  # the Anthropic API (ANTHROPIC_API_KEY; billed per token)
CLAUDE_CODE = "claude_code"  # the local `claude` CLI on your Claude subscription (agents/claude_code.py)
BACKENDS = (API, CLAUDE_CODE)


def run_agent(
    conn: sqlite3.Connection,
    *,
    agent: str,
    prompt: str,
    instructions: str = "",
    web_search_uses: int = 0,
    backend: str = API,
    client=None,
    db: sqlite3.Connection | None = None,
    max_turns: int = MAX_TURNS,
    effort: str = "high",
    timeout: float | None = None,
    extra_inputs: dict | None = None,
    now: Callable[[], str] = utc_now,
) -> RunResult:
    """Runs one agent to completion and records it. `conn` is the writable log database; `db` the read-only
    connection used to validate proposals and, for the API backend, by the tools (default: the tracker database;
    the Claude Code backend's tools open it themselves). Never raises for API or agent failures: they are recorded
    as a failed run."""
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")
    db = db or tools.readonly_connect()
    system_text = BASE_SYSTEM + "\n\n" + props.INSTRUCTIONS
    # The date goes in the user turn, not the system prompt, so the cached prefix stays the same every day.
    today = datetime.now(ET).date().isoformat()
    user_text = f"Today is {today} (US Eastern).\n\n{prompt}"

    inputs = {
        "backend": backend, "prompt": prompt, "instructions": instructions, "effort": effort,
        "web_search_uses": web_search_uses, "max_turns": max_turns,
        "system_sha": hashlib.sha256((system_text + instructions).encode()).hexdigest()[:12],
        **(extra_inputs or {}),
    }
    run_id = start_run(conn, agent, inputs, MODEL, now)
    trace = Trace()
    try:
        if backend == API:
            output = _api(client, conn, db, run_id, trace, system_text, instructions, user_text, web_search_uses,
                          max_turns, effort, timeout)
        else:
            from agents import claude_code

            output = claude_code.complete(
                system=system_text + ("\n\n" + instructions if instructions else ""), prompt=user_text,
                web_search=web_search_uses > 0, effort=effort, max_turns=max_turns, trace=trace,
                timeout=timeout or claude_code.TIMEOUT_SECONDS,
                on_call=lambda: save_calls(conn, run_id, trace),
            )
        proposals, warnings = props.validate(db, output.get("proposals") or [])
    except Exception as e:  # API errors, refusals, bad output: all end the run as failed
        log.exception("Agent run %d (%s) failed", run_id, agent)
        error = str(e) if isinstance(e, AgentError) else f"{type(e).__name__}: {e}"
        fail_run(conn, run_id, error, trace, now)
        return RunResult(run_id, "failed", error=error)

    summary = str(output.get("summary") or "")
    finish_run(conn, run_id, summary, proposals, trace, now)
    for w in warnings:
        log.warning("Agent run %d: %s", run_id, w)
    return RunResult(run_id, "ok", summary, proposals, warnings)


# --- agent_runs / agent_proposals rows (shared with agents/watchlist_review.py) --------------------------------


def start_run(conn: sqlite3.Connection, agent: str, inputs: dict, model: str | None,
              now: Callable[[], str] = utc_now) -> int:
    run_id = conn.execute(
        "INSERT INTO agent_runs (agent, started_at, inputs, model, status) VALUES (?, ?, ?, ?, 'running')",
        (agent, now(), json.dumps(inputs), model),
    ).lastrowid
    conn.commit()
    return run_id


def save_calls(conn: sqlite3.Connection, run_id: int, trace: Trace) -> None:
    conn.execute("UPDATE agent_runs SET tools_called = ? WHERE run_id = ?", (json.dumps(trace.calls), run_id))
    conn.commit()


def finish_run(conn: sqlite3.Connection, run_id: int, summary: str, proposals: list[props.Proposal],
               trace: Trace | None = None, now: Callable[[], str] = utc_now) -> None:
    for position, p in enumerate(proposals):
        conn.execute(
            "INSERT INTO agent_proposals (run_id, position, kind, member_id, title, rationale, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, position, p.kind, p.member_id, p.title, p.rationale, json.dumps(p.evidence)),
        )
    trace = trace or Trace()
    conn.execute(
        "UPDATE agent_runs SET status = 'ok', output = ?, finished_at = ?, tools_called = ?, usage = ? "
        "WHERE run_id = ?",
        (summary, now(), json.dumps(trace.calls), json.dumps(trace.usage), run_id),
    )
    conn.commit()


def fail_run(conn: sqlite3.Connection, run_id: int, error: str, trace: Trace,
             now: Callable[[], str] = utc_now) -> None:
    conn.execute(
        "UPDATE agent_runs SET status = 'failed', error = ?, finished_at = ?, tools_called = ?, usage = ? "
        "WHERE run_id = ?",
        (error, now(), json.dumps(trace.calls), json.dumps(trace.usage), run_id),
    )
    conn.commit()


# --- the API backend ----------------------------------------------------------------------------------------------


def _api(client, conn, db, run_id, trace, system_text, instructions, user_text, web_search_uses, max_turns,
         effort, timeout=None) -> dict:
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    if timeout:  # per request; the SDK's retries can stretch the total
        client = client.with_options(timeout=timeout, max_retries=0)
    tool_list = tools.DB_TOOLS + ([tools.web_search(web_search_uses)] if web_search_uses else [])
    system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
    if instructions:
        system.append({"type": "text", "text": instructions})
    messages = [{"role": "user", "content": user_text}]
    return _loop(client, conn, db, run_id, trace, system, tool_list, messages, max_turns, effort)


def _loop(client, conn, db, run_id, trace, system, tool_list, messages, max_turns, effort) -> dict:
    for turn in range(1, max_turns + 1):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=tool_list,
            messages=messages,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": props.OUTPUT_SCHEMA}},
            cache_control={"type": "ephemeral"},  # also cache the growing conversation between turns
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        trace.add_usage(response)
        messages.append({"role": "assistant", "content": response.content})

        searches = {}
        for block in response.content:
            if block.type == "server_tool_use":
                searches[block.id] = {"turn": turn, "name": block.name, "input": block.input}
                trace.calls.append(searches[block.id])
            elif block.type == "web_search_tool_result" and block.tool_use_id in searches:
                results = block.content if isinstance(block.content, list) else []  # an error is an object
                urls = [r.url for r in results if getattr(r, "url", None)]
                searches[block.tool_use_id].update(rows=len(urls), urls=urls[:10])

        stop = response.stop_reason
        if stop == "end_turn":
            text = "".join(b.text for b in response.content if b.type == "text")
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise AgentError(f"final answer is not valid JSON ({e}): {text[:200]!r}") from e
        if stop == "pause_turn":  # a long server-side web search; re-send to let it continue
            continue
        if stop == "tool_use":
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                out = tools.call(db, block.name, block.input or {})
                trace.calls.append({
                    "turn": turn, "name": block.name, "input": block.input, "rows": out.rows,
                    "truncated": out.truncated, "error": out.content if out.is_error else None,
                })
                results.append({
                    "type": "tool_result", "tool_use_id": block.id, "content": out.content, "is_error": out.is_error,
                })
            messages.append({"role": "user", "content": results})
            save_calls(conn, run_id, trace)
            continue
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            raise AgentError(f"the model declined (category: {getattr(details, 'category', None)})")
        raise AgentError(f"stopped early: {stop}")
    raise AgentError(f"no answer after {max_turns} turns")
