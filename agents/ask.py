"""Ad-hoc research: ask a question about the data; the answer (and any proposals) is logged for review in the app.

    python -m agents.ask "Which ranked member not on my watchlist has the strongest 20-day record?"
    python -m agents.ask --no-web "How did Pelosi's buys do from D0 at 20 days?"
    python -m agents.ask --dry-run "..."      # print only; nothing is written to the database
    python -m agents.ask --api "..."          # the Anthropic API (ANTHROPIC_API_KEY) instead of Claude Code

By default it runs through the local Claude Code CLI on your Claude subscription (no API key, no per-token bill).
"""

import argparse
import logging
import sys

from agents import runner
from common import log as logs
from db import connect

log = logging.getLogger(__name__)

AGENT = "researcher"
WEB_SEARCHES = 5

INSTRUCTIONS = """\
Answer the owner's question from the database first; use web search only for context the data can't give
(news, earnings dates, committee changes), and cite the URL. If the answer suggests following or dropping a
member, propose it as a watchlist change backed by leaderboard numbers; otherwise return no proposals."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("question")
    parser.add_argument("--no-web", action="store_true", help="database tools only")
    parser.add_argument("--dry-run", action="store_true", help="print the result; write nothing")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--api", action="store_true",
                        help="use the Anthropic API (needs ANTHROPIC_API_KEY) instead of Claude Code")
    args = parser.parse_args(argv)

    logs.setup()
    # A dry run logs to a throwaway in-memory database; the tools still read the real one.
    conn = connect(":memory:") if args.dry_run else connect()
    result = runner.run_agent(
        conn, agent=AGENT, prompt=args.question, instructions=INSTRUCTIONS,
        web_search_uses=0 if args.no_web else WEB_SEARCHES, effort=args.effort,
        backend=runner.API if args.api else runner.CLAUDE_CODE,
    )
    if result.status != "ok":
        print(f"Run failed: {result.error}", file=sys.stderr)
        return 1

    print(result.summary)
    for i, p in enumerate(result.proposals, 1):
        print(f"\nProposal {i} [{p.kind}{' ' + p.member_id if p.member_id else ''}]: {p.title}\n  {p.rationale}")
        for e in p.evidence:
            print(f"  - {e.get('claim')}  ({e.get('source')})")
    for w in result.warnings:
        print(f"\nNote: {w}")
    if not args.dry_run:
        print(f"\nLogged as agent run {result.run_id}" + ("; approve proposals in the app's Agents page."
                                                         if result.proposals else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
