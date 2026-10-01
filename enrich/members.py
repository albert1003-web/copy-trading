"""Members from the congress-legislators dataset, and matching each filing's filer to one of them.

  House:  members who held the filing's state_district (e.g. OH04) since 2019, whose last name is in the
          filer name. Fallback: same last name and state.
  Senate: senators since 2019 whose last name is in the filer name; ties broken by first name, nickname
          or middle name (Rick vs Tim Scott).
  enrich/member_aliases.csv (filer_name,bioguide) overrides both, for names the rules can't match.
"""

import csv
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("enrich.members")

ALIASES = Path(__file__).with_name("member_aliases.csv")
SINCE = "2019-01-01"  # only terms ending after this matter for current filings
PARTIES = {"Democrat": "D", "Republican": "R", "Independent": "I"}
NOISE = {"hon", "honorable", "the", "mr", "mrs", "ms", "dr", "jr", "sr", "ii", "iii", "iv", "v"}


@dataclass
class Legislator:
    member_id: str
    name: str
    chamber: str  # of the latest term
    party: str | None
    state: str
    active: bool
    last: list[str]  # normalized last-name tokens
    given: set[str]  # normalized first / middle / nickname tokens
    house_seats: set[str] = field(default_factory=set)  # e.g. {"OH04"} since SINCE
    states: set[str] = field(default_factory=set)
    senator: bool = False  # served in the Senate since SINCE


def tokens(name: str | None) -> list[str]:
    """'Hon. A. Mitchell McConnell, Jr.' -> ['a', 'mitchell', 'mcconnell']"""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z][a-z'\-]*", text)
    return [w.replace("'", "") for w in words if w not in NOISE]


def load(current: list[dict], historical: list[dict]) -> list[Legislator]:
    people = []
    for record, active in [(r, True) for r in current] + [(r, False) for r in historical]:
        recent = [t for t in record["terms"] if t["end"] >= SINCE]
        if not recent:
            continue
        name, last_term = record["name"], record["terms"][-1]
        people.append(Legislator(
            member_id=record["id"]["bioguide"],
            name=name.get("official_full") or f"{name['first']} {name['last']}",
            chamber="senate" if last_term["type"] == "sen" else "house",
            party=PARTIES.get(last_term.get("party"), (last_term.get("party") or "")[:1] or None),
            state=last_term["state"],
            active=active,
            last=tokens(name["last"]),
            given=set(tokens(" ".join(name.get(k, "") for k in ("first", "middle", "nickname")))),
            house_seats={f"{t['state']}{int(t['district']):02d}" for t in recent
                         if t["type"] == "rep" and t.get("district") is not None},
            states={t["state"] for t in recent},
            senator=any(t["type"] == "sen" for t in recent),
        ))
    return people


def upsert(conn: sqlite3.Connection, people: list[Legislator]) -> None:
    """Inserts or refreshes members; never touches committees (M2.2) or deletes anyone (watchlist FKs)."""
    conn.executemany(
        """
        INSERT INTO members (member_id, name, chamber, party, state, active) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (member_id) DO UPDATE SET
          name = excluded.name, chamber = excluded.chamber, party = excluded.party,
          state = excluded.state, active = excluded.active
        """,
        [(p.member_id, p.name, p.chamber, p.party, p.state, int(p.active)) for p in people],
    )


def aliases(path: Path = ALIASES) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {row["filer_name"].strip(): row["bioguide"].strip() for row in csv.DictReader(f) if row.get("bioguide")}


def match(filer_name: str, chamber: str, state_district: str | None, people: list[Legislator],
          overrides: dict[str, str] | None = None) -> str | None:
    """The bioguide id for a filer, or None if no single member fits."""
    if overrides and filer_name.strip() in overrides:
        return overrides[filer_name.strip()]
    words = tokens(filer_name)
    named = [p for p in people if p.last and _contains(words, p.last)]
    if chamber == "house":
        seat = (state_district or "").upper()
        found = [p for p in named if seat in p.house_seats] or [p for p in named if seat[:2] in p.states]
    else:
        found = [p for p in named if p.senator]
    if len(found) > 1:
        found = [p for p in found if p.given & set(words)] or found
    return found[0].member_id if len(found) == 1 else None


def _contains(words: list[str], last: list[str]) -> bool:
    """The last name's tokens appear consecutively in the filer name ('van orden', 'wasserman schultz')."""
    n = len(last)
    return any(words[i:i + n] == last for i in range(len(words) - n + 1))


def assign(conn: sqlite3.Connection, people: list[Legislator], overrides: dict[str, str]) -> list[str]:
    """Sets filings.member_id and trades.member_id. Returns the filer names that matched nobody."""
    unmatched = set()
    for row in conn.execute(
        "SELECT DISTINCT filer_name, chamber, state_district FROM filings WHERE filer_name IS NOT NULL"
    ).fetchall():
        member_id = match(row["filer_name"], row["chamber"], row["state_district"], people, overrides)
        if member_id is None:
            unmatched.add(row["filer_name"])
        conn.execute(
            """UPDATE filings SET member_id = ?
               WHERE filer_name = ? AND chamber = ? AND COALESCE(state_district, '') = COALESCE(?, '')""",
            (member_id, row["filer_name"], row["chamber"], row["state_district"]),
        )
    conn.execute("UPDATE trades SET member_id = (SELECT member_id FROM filings f WHERE f.doc_id = trades.doc_id)")
    for name in sorted(unmatched):
        log.warning("No member matches filer %r; add it to %s", name, ALIASES.name)
    return sorted(unmatched)
