"""The structured output every agent returns: a summary plus proposals a human approves one by one.

Only watchlist proposals change anything (via agents/apply.py, after approval). A `note` carries any other
suggestion, e.g. an exit-rule change: exit_rules is rebuilt nightly by analytics, so it isn't applied directly.
"""

import sqlite3
from dataclasses import dataclass, field

WATCHLIST_ADD = "watchlist_add"
WATCHLIST_REMOVE = "watchlist_remove"
NOTE = "note"
KINDS = [WATCHLIST_ADD, WATCHLIST_REMOVE, NOTE]

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "The answer or report, in plain text for the user"},
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": KINDS},
                    "member_id": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "bioguide id (members.member_id) for watchlist kinds, else null",
                    },
                    "title": {"type": "string", "description": "One line, e.g. 'Add Jane Doe to the watchlist'"},
                    "rationale": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim": {"type": "string"},
                                "source": {"type": "string", "description": "The SQL that shows it, or a URL"},
                            },
                            "required": ["claim", "source"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["kind", "member_id", "title", "rationale", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "proposals"],
    "additionalProperties": False,
}

INSTRUCTIONS = """\
Finish with the JSON output: a `summary` for the user, and `proposals` (often none).
- `watchlist_add` / `watchlist_remove`: set `member_id` to the member's bioguide id from `members`. Propose one only
  when the evidence supports it, with every claim's `source` being the SQL you ran (or a URL).
- `note`: any other suggestion (exit rules, data problems, something to check by hand); `member_id` may be null.
A human reviews each proposal; nothing changes until they approve it."""


@dataclass
class Proposal:
    kind: str
    member_id: str | None
    title: str
    rationale: str
    evidence: list[dict] = field(default_factory=list)


def validate(conn: sqlite3.Connection, raw: list[dict]) -> tuple[list[Proposal], list[str]]:
    """Checks watchlist proposals against the database. One that can't be applied (unknown member, already watched,
    not on the list, no evidence) becomes a note, so the human still sees it but approving it changes nothing."""
    proposals, warnings = [], []
    for item in raw:
        p = Proposal(
            kind=item.get("kind") if item.get("kind") in KINDS else NOTE,
            member_id=item.get("member_id") or None,
            title=str(item.get("title") or "(untitled)"),
            rationale=str(item.get("rationale") or ""),
            evidence=[e for e in item.get("evidence") or [] if isinstance(e, dict)],
        )
        if p.kind != NOTE:
            problem = _watchlist_problem(conn, p)
            if problem:
                warnings.append(f"{p.title!r} downgraded to a note: {problem}")
                p.rationale = f"[Not applicable: {problem}] {p.rationale}"
                p.kind = NOTE
        proposals.append(p)
    return proposals, warnings


def _watchlist_problem(conn: sqlite3.Connection, p: Proposal) -> str | None:
    if not p.evidence:
        return "no evidence given"
    if not p.member_id or not conn.execute("SELECT 1 FROM members WHERE member_id = ?", (p.member_id,)).fetchone():
        return f"unknown member_id {p.member_id!r}"
    row = conn.execute("SELECT active FROM watchlist WHERE member_id = ?", (p.member_id,)).fetchone()
    watched = bool(row and row[0])
    if p.kind == WATCHLIST_ADD and watched:
        return "already on the watchlist"
    if p.kind == WATCHLIST_REMOVE and not watched:
        return "not on the watchlist"
    return None
