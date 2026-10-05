import json
from types import SimpleNamespace as NS

import pytest

from agents import apply, runner, tools
from agents.proposals import NOTE, WATCHLIST_ADD, WATCHLIST_REMOVE


@pytest.fixture
def db_path(tmp_path, conn):
    conn.executemany(
        "INSERT INTO members (member_id, name, chamber) VALUES (?, ?, 'house')",
        [("A000001", "Alice Able"), ("B000002", "Bob Baker"), ("C000003", "Cara Cole")],
    )
    conn.execute("INSERT INTO watchlist (member_id, added_at, reason) VALUES ('B000002', '2026-09-01T00:00:00Z', 'x')")
    conn.commit()
    return tmp_path / "test.db"


@pytest.fixture
def ro(db_path):
    connection = tools.readonly_connect(db_path)
    yield connection
    connection.close()


# --- read-only tools ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "INSERT INTO members (member_id, name, chamber) VALUES ('X', 'X', 'house')",
    "UPDATE members SET name = 'X'",
    "WITH x AS (SELECT 1) DELETE FROM members",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA writable_schema = 1",
    "CREATE TABLE evil (a)",
    "DROP TABLE watchlist",
    "SELECT 1; DELETE FROM members",
])
def test_query_rejects_anything_but_a_read(ro, conn, sql):
    out = tools.call(ro, "query", {"sql": sql})
    assert out.is_error
    assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 3


def test_query_returns_rows_and_flags_truncation(ro):
    out = tools.call(ro, "query", {"sql": "SELECT member_id, name FROM members ORDER BY member_id"})
    assert not out.is_error and out.rows == 3
    assert json.loads(out.content)["rows"][0] == ["A000001", "Alice Able"]

    many = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 500) SELECT i FROM n"
    out = tools.call(ro, "query", {"sql": many})
    assert out.rows == tools.MAX_ROWS and out.truncated and json.loads(out.content)["truncated"]


def test_sql_errors_come_back_to_the_agent(ro):
    out = tools.call(ro, "query", {"sql": "SELECT nope FROM members"})
    assert out.is_error and "no such column" in out.content


def test_describe_table_uses_the_documented_schema(ro):
    out = tools.call(ro, "describe_table", {"name": "trade_outcomes"})
    assert "CREATE TABLE IF NOT EXISTS trade_outcomes" in out.content and "Sample rows" in out.content
    assert tools.call(ro, "describe_table", {"name": "nope"}).is_error


def test_list_tables_includes_descriptions(ro):
    tables = {t["table"]: t["about"] for t in json.loads(tools.call(ro, "list_tables", {}).content)}
    assert "member_scores" in tables and "leaderboard" in tables["member_scores"]


def test_schema_blocks_cover_every_table(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names <= set(tools.schema_blocks())


# --- runner -----------------------------------------------------------------------------------------------------


def text(t):
    return NS(type="text", text=t)


def tool_use(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


def message(content, stop="end_turn", **usage):
    return NS(content=content, stop_reason=stop, stop_details=None, model=runner.MODEL,
              usage=NS(input_tokens=100, output_tokens=10, cache_read_input_tokens=0,
                       cache_creation_input_tokens=0, server_tool_use=NS(**usage) if usage else None))


def answer(summary, proposals=()):
    return message([text(json.dumps({"summary": summary, "proposals": list(proposals)}))])


def proposal(kind, member_id, title="t", evidence=({"claim": "c", "source": "SELECT 1"},)):
    return {"kind": kind, "member_id": member_id, "title": title, "rationale": "r", "evidence": list(evidence)}


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def run(conn, ro, responses, **kwargs):
    client = FakeClient(responses)
    result = runner.run_agent(conn, agent="test", prompt="q?", client=client, db=ro, **kwargs)
    row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (result.run_id,)).fetchone()
    return result, row, client


def test_run_logs_tool_calls_and_writes_proposals(conn, ro):
    responses = [
        message([tool_use("t1", "query", {"sql": "SELECT COUNT(*) FROM members"})], stop="tool_use"),
        message([NS(type="server_tool_use", name="web_search", input={"query": "Alice Able trades"})],
                stop="pause_turn", web_search_requests=1),
        message([tool_use("t2", "describe_table", {"name": "watchlist"}),
                 tool_use("t3", "query", {"sql": "DELETE FROM members"})], stop="tool_use"),
        answer("Alice looks strong.", [
            proposal(WATCHLIST_ADD, "A000001", "Add Alice"),
            proposal(WATCHLIST_ADD, "Z999999", "Add a ghost"),
            proposal(WATCHLIST_REMOVE, "B000002", "Drop Bob", evidence=()),
            proposal(NOTE, None, "Try a 10-day hold"),
        ]),
    ]
    result, row, client = run(conn, ro, responses, web_search_uses=3)

    assert result.status == "ok" and row["status"] == "ok" and row["output"] == "Alice looks strong."
    assert row["model"] == runner.MODEL and row["finished_at"]
    calls = json.loads(row["tools_called"])
    assert [c["name"] for c in calls] == ["query", "web_search", "describe_table", "query"]
    assert calls[0]["rows"] == 1 and calls[3]["error"]
    usage = json.loads(row["usage"])
    assert usage["turns"] == 4 and usage["web_search_requests"] == 1

    # All tool results for one turn go back in one user message, errors flagged.
    results = client.requests[3]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t2", "t3"] and results[1]["is_error"]
    # The schema-constrained output, web search and the refusal fallback are requested.
    req = client.requests[0]
    assert req["output_config"]["format"]["type"] == "json_schema" and req["fallbacks"] == "default"
    assert any(t.get("type") == "web_search_20260209" for t in req["tools"])
    assert "Today is" in req["messages"][0]["content"]

    stored = conn.execute(
        "SELECT kind, member_id, title, approved FROM agent_proposals WHERE run_id = ? ORDER BY position",
        (result.run_id,),
    ).fetchall()
    assert [tuple(r) for r in stored] == [
        (WATCHLIST_ADD, "A000001", "Add Alice", None),
        (NOTE, "Z999999", "Add a ghost", None),     # unknown member
        (NOTE, "B000002", "Drop Bob", None),        # no evidence
        (NOTE, None, "Try a 10-day hold", None),
    ]
    assert len(result.warnings) == 2


def test_add_for_a_watched_member_becomes_a_note(conn, ro):
    result, _, _ = run(conn, ro, [answer("s", [proposal(WATCHLIST_ADD, "B000002")])])
    assert result.proposals[0].kind == NOTE and "already on the watchlist" in result.warnings[0]


def test_no_web_search_unless_asked(conn, ro):
    _, _, client = run(conn, ro, [answer("s")])
    assert all("type" not in t for t in client.requests[0]["tools"])


@pytest.mark.parametrize("responses, error", [
    ([message([text("not json")])], "not valid JSON"),
    ([NS(**{**vars(message([])), "stop_reason": "refusal", "stop_details": NS(category="cyber")})], "declined"),
    ([message([text("{")], stop="max_tokens")], "stopped early: max_tokens"),
    ([message([tool_use(f"t{i}", "list_tables", {})], stop="tool_use") for i in range(3)], "after 3 turns"),
])
def test_failed_runs_are_recorded_without_proposals(conn, ro, responses, error):
    result, row, _ = run(conn, ro, responses, max_turns=3)
    assert result.status == "failed" and row["status"] == "failed" and error in row["error"]
    assert conn.execute("SELECT COUNT(*) FROM agent_proposals").fetchone()[0] == 0


def test_api_errors_are_recorded(conn, ro):
    class Boom:
        beta = NS(messages=NS(create=lambda **_: (_ for _ in ()).throw(RuntimeError("overloaded"))))

    result = runner.run_agent(conn, agent="test", prompt="q", client=Boom(), db=ro)
    row = conn.execute("SELECT status, error FROM agent_runs WHERE run_id = ?", (result.run_id,)).fetchone()
    assert tuple(row) == ("failed", "RuntimeError: overloaded")


# --- apply ------------------------------------------------------------------------------------------------------


def seed_proposals(conn, rows):
    conn.execute("INSERT INTO agent_runs (run_id, agent, started_at, status) VALUES (7, 'test', '2026-10-01', 'ok')")
    for i, (kind, member_id, approved) in enumerate(rows):
        conn.execute(
            "INSERT INTO agent_proposals (run_id, position, kind, member_id, title, rationale, approved) "
            "VALUES (7, ?, ?, ?, ?, 'r', ?)",
            (i, kind, member_id, f"p{i}", approved),
        )
    conn.commit()


def watched(conn):
    return {r[0] for r in conn.execute("SELECT member_id FROM watchlist WHERE active = 1")}


def test_apply_carries_out_only_approved_proposals(conn, db_path):
    seed_proposals(conn, [
        (WATCHLIST_ADD, "A000001", 1),
        (WATCHLIST_REMOVE, "B000002", 1),
        (WATCHLIST_ADD, "C000003", None),  # pending
        (WATCHLIST_ADD, "C000003", 0),     # rejected
        (NOTE, None, 1),
    ])
    s = apply.run(conn, now=lambda: "2026-10-05T12:00:00Z")
    assert (s.applied, s.acknowledged, s.failed) == (2, 1, [])
    assert watched(conn) == {"A000001"}
    reason = conn.execute("SELECT reason FROM watchlist WHERE member_id = 'A000001'").fetchone()[0]
    assert reason == "agent run 7: p0"
    results = [tuple(r) for r in conn.execute("SELECT apply_result, applied_at FROM agent_proposals ORDER BY position")]
    assert results == [("applied", "2026-10-05T12:00:00Z"), ("applied", "2026-10-05T12:00:00Z"),
                       (None, None), (None, None), ("acknowledged", "2026-10-05T12:00:00Z")]

    again = apply.run(conn)
    assert (again.applied, again.acknowledged, again.failed) == (0, 0, [])


def test_apply_reactivates_and_records_failures_once(conn, db_path):
    conn.execute("UPDATE watchlist SET active = 0 WHERE member_id = 'B000002'")
    seed_proposals(conn, [
        (WATCHLIST_ADD, "B000002", 1),      # re-adds a soft-deleted row
        (WATCHLIST_ADD, "NOPE", 1),         # FK error
        (WATCHLIST_REMOVE, "C000003", 1),   # not watched
    ])
    s = apply.run(conn)
    assert s.applied == 1 and len(s.failed) == 2
    assert watched(conn) == {"B000002"}
    results = [r[0] for r in conn.execute("SELECT apply_result FROM agent_proposals ORDER BY position")]
    assert results[0] == "applied" and results[1].startswith("failed: FOREIGN KEY")
    assert results[2] == "failed: not on the watchlist any more"
    assert apply.run(conn).failed == []


# --- Claude Code backend (a fake `claude` process) ----------------------------------------------------------------


class FakeClaude:
    """Stands in for subprocess.Popen: replays stream-json events and records the command."""

    def __init__(self, events, code=0):
        self.events, self.code, self.calls = events, code, []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        written = []
        self.prompt = written
        return NS(stdin=NS(write=written.append, close=lambda: None),
                  stdout=iter(json.dumps(e) + "\n" for e in self.events),
                  wait=lambda: self.code, kill=lambda: None)


INIT = {"type": "system", "subtype": "init", "apiKeySource": "none",
        "mcp_servers": [{"name": "tracker", "status": "connected"}]}


def cc_tool_use(id_, name, input_):
    return {"type": "assistant", "message": {"id": f"m{id_}", "content": [
        {"type": "tool_use", "id": id_, "name": name, "input": input_}]}}


def cc_result(id_, content, is_error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": id_, "content": content, "is_error": is_error}]}}


def cc_done(output, subtype="success", **extra):
    return {"type": "result", "subtype": subtype, "is_error": subtype != "success", "num_turns": 4,
            "structured_output": output, "result": json.dumps(output), "total_cost_usd": 0.04,
            "usage": {"input_tokens": 5, "output_tokens": 50, "server_tool_use": {"web_search_requests": 1}},
            "modelUsage": {runner.MODEL: {}}, **extra}


def run_cc(conn, ro, monkeypatch, fake, **kwargs):
    from agents import claude_code

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    real = claude_code.complete
    monkeypatch.setattr(claude_code, "complete", lambda **kw: real(**kw, run=fake))
    result = runner.run_agent(conn, agent="test", prompt="q?", backend=runner.CLAUDE_CODE, db=ro, **kwargs)
    row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (result.run_id,)).fetchone()
    return result, row


def test_claude_code_backend_logs_calls_and_writes_proposals(conn, ro, monkeypatch):
    rows = json.dumps({"result": json.dumps({"columns": ["n"], "rows": [[3]], "truncated": False})})
    fake = FakeClaude([
        INIT,
        cc_tool_use("t1", "mcp__tracker__query", {"sql": "SELECT COUNT(*) FROM members"}),
        cc_result("t1", rows),
        cc_tool_use("t2", "WebSearch", {"query": "Alice Able"}),
        cc_result("t2", [{"type": "text", "text": "results"}]),
        cc_tool_use("t3", "mcp__tracker__query", {"sql": "DELETE FROM members"}),
        cc_result("t3", "not allowed: the database is read-only", is_error=True),
        cc_tool_use("t4", "StructuredOutput", {}),
        cc_done({"summary": "Alice looks strong.", "proposals": [proposal(WATCHLIST_ADD, "A000001", "Add Alice")]}),
    ])
    result, row = run_cc(conn, ro, monkeypatch, fake, web_search_uses=5)

    assert result.status == "ok" and row["output"] == "Alice looks strong."
    calls = json.loads(row["tools_called"])
    assert [c["name"] for c in calls] == ["query", "web_search", "query"]
    assert calls[0]["rows"] == 1 and "read-only" in calls[2]["error"]
    usage = json.loads(row["usage"])
    assert usage["turns"] == 4 and usage["web_search_requests"] == 1 and usage["auth"] == "none"
    assert json.loads(row["inputs"])["backend"] == runner.CLAUDE_CODE
    assert [p.kind for p in result.proposals] == [WATCHLIST_ADD]

    call = fake.calls[0]
    cmd = call["cmd"]
    assert "ANTHROPIC_API_KEY" not in call["env"]  # always the subscription, never the key
    assert cmd[cmd.index("--tools") + 1] == "WebSearch"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert set(cmd[cmd.index("--allowedTools") + 1].split()) == {
        "mcp__tracker__list_tables", "mcp__tracker__describe_table", "mcp__tracker__query", "WebSearch"}
    assert "Today is" in "".join(fake.prompt)


def test_claude_code_without_web_has_no_built_in_tools(conn, ro, monkeypatch):
    fake = FakeClaude([INIT, cc_done({"summary": "s", "proposals": []})])
    run_cc(conn, ro, monkeypatch, fake)
    cmd = fake.calls[0]["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "" and "WebSearch" not in cmd[cmd.index("--allowedTools") + 1]


@pytest.mark.parametrize("events, code, error", [
    ([INIT, cc_done(None, subtype="error_max_turns")], 1, "no answer after"),
    ([INIT, cc_done(None, subtype="error_during_execution")], 1, "Claude Code failed"),
    ([INIT, cc_done("not a dict")], 0, "no structured answer"),
    ([INIT], 1, "exited with 1"),
    ([{**INIT, "mcp_servers": [{"name": "tracker", "status": "failed"}]}], 1, "database tools didn't start"),
])
def test_claude_code_failures_are_recorded(conn, ro, monkeypatch, events, code, error):
    result, row = run_cc(conn, ro, monkeypatch, FakeClaude(events, code))
    assert result.status == "failed" and error in row["error"]
    assert conn.execute("SELECT COUNT(*) FROM agent_proposals").fetchone()[0] == 0


def test_mcp_server_exposes_the_read_only_tools(db_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from agents import mcp_server

    monkeypatch.setattr(mcp_server, "_conn", tools.readonly_connect(db_path))
    assert json.loads(mcp_server.query("SELECT COUNT(*) FROM members"))["rows"] == [[3]]
    assert "watchlist" in mcp_server.list_tables()
    with pytest.raises(ToolError, match="read-only"):
        mcp_server.query("DELETE FROM members")


# --- rule-based watchlist review ----------------------------------------------------------------------------------


def seed_scores(conn, rows, as_of="2026-10-05"):
    for member_id, rank, score, hit in rows:
        conn.execute(
            "INSERT INTO member_scores (member_id, as_of, horizon, n_filings, rank, shrunk_score, mean_abn_ret, "
            "hit_rate, consistency) VALUES (?, ?, 20, 25, ?, ?, ?, ?, 0.5)",
            (member_id, as_of, rank, score, score, hit),
        )
    conn.commit()


def test_watchlist_review_proposes_by_the_rules(conn, db_path):
    from agents import watchlist_review as review

    conn.execute("INSERT INTO members (member_id, name, chamber) VALUES ('D000004', 'Dan Doe', 'house')")
    seed_scores(conn, [
        ("A000001", 1, 0.02, 0.6),     # add
        ("B000002", 2, -0.01, 0.4),    # watched + negative: remove
        ("C000003", 3, 0.004, 0.6),    # score below the bar
        ("D000004", None, 0.05, 0.9),  # unranked: never proposed
    ])
    s = review.run(conn)
    assert [(p.kind, p.member_id) for p in s.proposals] == [(WATCHLIST_ADD, "A000001"), (WATCHLIST_REMOVE, "B000002")]
    run_row = conn.execute(
        "SELECT agent, status, model, output FROM agent_runs WHERE run_id = ?", (s.run_id,)
    ).fetchone()
    assert tuple(run_row)[:3] == ("watchlist_review", "ok", None) and "1 to add, 1 to remove" in run_row[3]
    evidence = conn.execute("SELECT evidence FROM agent_proposals WHERE member_id = 'A000001'").fetchone()[0]
    evidence = json.loads(evidence)
    assert "Ranked #1 of 3" in evidence[0]["claim"] and "member_scores" in evidence[0]["source"]

    # Pending proposals aren't repeated, and nothing new means no run at all.
    again = review.run(conn)
    assert again.run_id is None and again.skipped == 2


def test_watchlist_review_respects_rejections_for_a_while(conn, db_path):
    from datetime import UTC, datetime

    from agents import watchlist_review as review

    seed_scores(conn, [("A000001", 1, 0.02, 0.6)])
    first = review.run(conn, now=lambda: datetime(2026, 10, 5, tzinfo=UTC))
    conn.execute("UPDATE agent_proposals SET approved = 0, decided_at = '2026-10-06T10:00:00.000Z' WHERE run_id = ?",
                 (first.run_id,))
    assert review.run(conn, now=lambda: datetime(2026, 12, 1, tzinfo=UTC)).proposals == []
    assert len(review.run(conn, now=lambda: datetime(2027, 1, 10, tzinfo=UTC)).proposals) == 1


def test_watchlist_review_without_a_leaderboard_does_nothing(conn):
    from agents import watchlist_review as review

    s = review.run(conn)
    assert s.as_of is None and s.run_id is None
