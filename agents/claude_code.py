"""Runs an agent through the local Claude Code CLI (`claude -p`), on your Claude subscription: no API key and no
per-token bill (it counts toward the plan's usage limits instead).

The run is locked down to the same capabilities as the API backend:
- tools: only the read-only database tools (agents/mcp_server.py, over MCP) and, if asked, web search;
  `--tools` removes every built-in tool that reads files or runs commands, and `--permission-mode dontAsk` denies
  anything not in `--allowedTools`;
- `--setting-sources ""` ignores your Claude Code settings (hooks, permissions), and the run's working directory is
  an empty temp folder, so no CLAUDE.md is loaded;
- ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are removed from its environment, so it always uses the subscription;
- the final answer is the same structured output (agents/proposals.OUTPUT_SCHEMA), via `--json-schema`.

Tool calls are read from the stream-json output as they happen and logged like the API backend's.
"""

import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from agents import proposals as props
from agents import tools
from agents.runner import MODEL, AgentError, Trace
from common import config

CLAUDE = "claude"
# launchd runs the pipeline without your shell's PATH, so look in the usual install places too.
CLAUDE_PLACES = ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")
TIMEOUT_SECONDS = 900
SERVER = "tracker"
DB_TOOL_NAMES = [f"mcp__{SERVER}__{t['name']}" for t in tools.DB_TOOLS]
WEB_SEARCH = "WebSearch"
MAX_URLS = 10  # web-search result URLs kept per call in tools_called
STRUCTURED_OUTPUT = "StructuredOutput"  # how Claude Code delivers --json-schema output; not logged as a tool call
SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def find_claude() -> str | None:
    """The `claude` executable: CLAUDE_BIN (.env), else PATH, else the usual install places."""
    if configured := os.environ.get("CLAUDE_BIN", "").strip():
        return configured
    if found := shutil.which(CLAUDE):
        return found
    for place in CLAUDE_PLACES:
        path = Path(place).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def available() -> bool:
    return find_claude() is not None


def mcp_config(db_path: Path) -> dict:
    return {"mcpServers": {SERVER: {
        "command": sys.executable,
        "args": ["-m", "agents.mcp_server"],
        "cwd": str(config.REPO_ROOT),
        "env": {"TRACKER_DB_PATH": str(db_path), "PYTHONPATH": str(config.REPO_ROOT)},
    }}}


def command(system: str, *, web_search: bool, effort: str, max_turns: int, mcp_config_path: Path) -> list[str]:
    allowed = DB_TOOL_NAMES + ([WEB_SEARCH] if web_search else [])
    return [
        find_claude() or CLAUDE, "-p",  # the prompt goes on stdin
        "--output-format", "stream-json", "--verbose",
        "--json-schema", json.dumps(props.OUTPUT_SCHEMA),
        "--system-prompt", system,
        "--tools", WEB_SEARCH if web_search else "",
        "--mcp-config", str(mcp_config_path), "--strict-mcp-config",
        "--allowedTools", " ".join(allowed),
        "--permission-mode", "dontAsk",
        "--setting-sources", "",
        "--no-session-persistence",
        "--model", MODEL,
        "--effort", effort,
        "--max-turns", str(max_turns),
    ]


def complete(
    *,
    system: str,
    prompt: str,
    web_search: bool,
    effort: str,
    max_turns: int,
    trace: Trace,
    timeout: float = TIMEOUT_SECONDS,
    on_call: Callable[[], None] = lambda: None,
    db_path: Path | None = None,
    run: Callable[..., subprocess.Popen] = subprocess.Popen,
) -> dict:
    """Runs one agent with Claude Code and returns its structured output. Raises AgentError on any failure."""
    if run is subprocess.Popen and not available():
        raise AgentError("the `claude` command (Claude Code) wasn't found; set CLAUDE_BIN in .env")
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    # Claude Code reads its login from the macOS Keychain, by account name (USER) and through system tools (PATH).
    # launchd sets both, but don't depend on it.
    env.setdefault("USER", getpass.getuser())
    path = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    env["PATH"] = os.pathsep.join(path + [p for p in SYSTEM_PATH if p not in path])
    with tempfile.TemporaryDirectory(prefix="tracker-agent-") as workdir:
        config_path = Path(workdir) / "mcp.json"
        config_path.write_text(json.dumps(mcp_config(db_path or config.db_path())))
        cmd = command(system, web_search=web_search, effort=effort, max_turns=max_turns, mcp_config_path=config_path)
        with tempfile.TemporaryFile(mode="w+") as stderr:
            proc = run(cmd, cwd=workdir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                       text=True)
            timed_out = threading.Event()

            def stop():
                timed_out.set()
                proc.kill()

            timer = threading.Timer(timeout, stop)
            timer.start()
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
                result = _read_stream(proc.stdout, trace, on_call)
                code = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            finally:
                timer.cancel()
            stderr.seek(0)
            err = stderr.read().strip()

    if timed_out.is_set():
        raise AgentError(f"Claude Code timed out after {timeout:.0f} s")
    if result is None:
        raise AgentError(f"Claude Code exited with {code} and no result: {err[-500:] or '(no error output)'}")
    _add_usage(trace, result)
    if result.get("subtype") == "error_max_turns":
        raise AgentError(f"no answer after {max_turns} turns")
    if result.get("is_error") or result.get("subtype") != "success":
        raise AgentError(f"Claude Code failed ({result.get('subtype')}): {str(result.get('result'))[:500]}")
    output = result.get("structured_output")
    if not isinstance(output, dict):
        raise AgentError(f"no structured answer: {str(result.get('result'))[:200]!r}")
    return output


def _read_stream(lines, trace: Trace, on_call: Callable[[], None]) -> dict | None:
    """Logs tool calls from Claude Code's stream-json events; returns the final `result` event."""
    by_id: dict[str, dict] = {}
    messages: list[str] = []
    result = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            trace.usage["auth"] = event.get("apiKeySource")
            servers = {s.get("name"): s.get("status") for s in event.get("mcp_servers") or []}
            if servers.get(SERVER) != "connected":
                raise AgentError(f"the database tools didn't start (MCP status: {servers.get(SERVER)})")
        elif kind == "assistant":
            message = event.get("message") or {}
            if message.get("id") not in messages:
                messages.append(message.get("id"))
            for block in _blocks(message):
                if block.get("type") == "tool_use" and block.get("name") != STRUCTURED_OUTPUT:
                    call = {"turn": len(messages), "name": _short(block.get("name")), "input": block.get("input")}
                    by_id[block.get("id")] = call
                    trace.calls.append(call)
        elif kind == "user":
            for block in _blocks(event.get("message") or {}):
                call = by_id.get(block.get("tool_use_id")) if block.get("type") == "tool_result" else None
                if call is not None:
                    _describe_result(call, block, event.get("tool_use_result"))
                    on_call()
        elif kind == "result":
            result = event
    return result


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _short(name: str | None) -> str:
    if name == WEB_SEARCH:
        return "web_search"
    prefix = f"mcp__{SERVER}__"
    return name[len(prefix):] if name and name.startswith(prefix) else str(name)


def _describe_result(call: dict, block: dict, extra=None) -> None:
    """Adds rows/truncated (database tools), the result URLs (web search) or the error text."""
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    content = str(content or "")
    if block.get("is_error"):
        call["error"] = content[:500]
        return
    if call["name"] == "web_search":  # the event's tool_use_result: {"query", "results": [{"content": [{url}]}]}
        found = [link.get("url") for r in (extra or {}).get("results") or [] if isinstance(r, dict)
                 for link in r.get("content") or [] if isinstance(link, dict) and link.get("url")]
        call["rows"], call["urls"] = len(found), found[:MAX_URLS]
        return
    try:  # MCP results arrive as {"result": "<the tool's JSON>"}
        outer = json.loads(content)
        inner = json.loads(outer["result"]) if isinstance(outer, dict) and "result" in outer else outer
        if isinstance(inner, dict) and "rows" in inner:
            call["rows"], call["truncated"] = len(inner["rows"]), bool(inner.get("truncated"))
        elif isinstance(inner, list):
            call["rows"] = len(inner)
    except (json.JSONDecodeError, TypeError, KeyError):
        pass


def _add_usage(trace: Trace, result: dict) -> None:
    u, total = result.get("usage") or {}, trace.usage
    total["turns"] = result.get("num_turns") or 0
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        total[key] = u.get(key) or 0
    # Claude Code's WebSearch doesn't show in server_tool_use; count the logged calls instead.
    searches = sum(1 for c in trace.calls if c.get("name") == "web_search")
    total["web_search_requests"] = max((u.get("server_tool_use") or {}).get("web_search_requests") or 0, searches)
    total["models"] = list(result.get("modelUsage") or {})
    # Claude Code's list-price estimate. On a subscription nothing is billed per token.
    total["est_cost_usd"] = result.get("total_cost_usd")
