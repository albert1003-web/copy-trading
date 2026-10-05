"""The read-only database tools (agents/tools.py) as an MCP server, for agents run through Claude Code.

    python -m agents.mcp_server        # stdio; started by agents/claude_code.py, not by hand

Same tools, same read-only connection (mode=ro, query_only, read-only authorizer) as the API runner.
"""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agents import tools

DESCRIPTIONS = {t["name"]: t["description"] for t in tools.DB_TOOLS}

server = MCPServer("tracker", instructions="Read-only access to the Congressional Trade Tracker database.")
_conn = None


def _db():
    global _conn
    if _conn is None:
        _conn = tools.readonly_connect()
    return _conn


def _result(out: tools.ToolOutput) -> str:
    if out.is_error:
        raise ToolError(out.content)
    return out.content


@server.tool(description=DESCRIPTIONS["list_tables"])
def list_tables() -> str:
    return _result(tools.call(_db(), "list_tables", {}))


@server.tool(description=DESCRIPTIONS["describe_table"])
def describe_table(name: str) -> str:
    return _result(tools.call(_db(), "describe_table", {"name": name}))


@server.tool(description=DESCRIPTIONS["query"])
def query(sql: str) -> str:
    return _result(tools.call(_db(), "query", {"sql": sql}))


if __name__ == "__main__":
    server.run("stdio")
