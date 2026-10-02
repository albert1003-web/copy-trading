"""Committee assignments per Congress and the trades.committee_relevant flag (Milestone 2.2).

Sources (congress-legislators, cached in <raw_dir>/reference/):
  committee-membership-current.yaml   today's assignments (refreshed weekly by enrich/reference.py); used for
                                      the Congress in session today
  committee-membership-<N>.yaml       the same file at a pinned commit, one per past Congress
                                      (enrich/committee_snapshots.csv); downloaded once, never changes
  committees-{current,historical}.yaml  committee names

When a new Congress starts, pin a snapshot of the outgoing one in committee_snapshots.csv (a commit from late in
its term). Subcommittees roll up to their parent committee (HSAS28 -> HSAS).

committee_relevant = 1 when, in the Congress covering the trade date, the member sat on a committee whose
sector (and industry, if given) in enrich/committee_sectors.csv matches the trade's.
"""

import csv
import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
import yaml

from common import http as polite
from enrich import reference

log = logging.getLogger("enrich.committees")

SNAPSHOTS = Path(__file__).with_name("committee_snapshots.csv")
SECTORS = Path(__file__).with_name("committee_sectors.csv")
SNAPSHOT_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/{commit}/committee-membership-current.yaml"
CURRENT = "committee-membership-current.yaml"


@dataclass(frozen=True)
class Membership:
    member_id: str
    congress: int
    committee_id: str
    committee_name: str | None
    role: str | None


def congress_of(day: date) -> int:
    """The Congress in session on a day; each begins January 3 of an odd year."""
    congress = (day.year - 1789) // 2 + 1
    if day.year % 2 == 1 and day.month == 1 and day.day < 3:
        congress -= 1
    return congress


def snapshots(path: Path = SNAPSHOTS) -> dict[int, str]:
    with path.open(newline="") as f:
        return {int(row["congress"]): row["commit"].strip() for row in csv.DictReader(f)}


def snapshot_path(raw_root: Path, congress: int) -> Path:
    return reference.path(raw_root, f"committee-membership-{congress}.yaml")


def refresh_snapshots(http: httpx.Client, raw_root: Path, pins: dict[int, str]) -> None:
    """Downloads each pinned past-Congress snapshot that isn't cached yet. A failure is retried next run."""
    for congress, commit in sorted(pins.items()):
        target = snapshot_path(raw_root, congress)
        if target.exists():
            continue
        try:
            response = polite.request(http, "GET", SNAPSHOT_URL.format(commit=commit))
            if response.status_code != 200 or not isinstance(yaml.safe_load(response.content), dict):
                raise ValueError(f"HTTP {response.status_code}, {len(response.content)} bytes")
        except (httpx.HTTPError, ValueError, yaml.YAMLError) as e:
            log.warning("Committee snapshot for Congress %d failed (%s); will retry", congress, e)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".yaml.part")
        tmp.write_bytes(response.content)
        tmp.replace(target)
        log.info("Cached committee snapshot for Congress %d", congress)


def names(raw_root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in ("committees-historical.yaml", "committees-current.yaml"):  # current names win
        path = reference.path(raw_root, name)
        if path.exists():
            found.update({c["thomas_id"]: c["name"] for c in yaml.safe_load(path.read_text()) or []})
    return found


def parse_membership(data: dict, congress: int, committee_names: dict[str, str]) -> list[Membership]:
    """Memberships from one membership file, rolled up to parent committees (a parent-level title wins)."""
    found: dict[tuple[str, str], Membership] = {}
    for key, people in (data or {}).items():
        parent = str(key)[:4]
        for person in people or []:
            bioguide = person.get("bioguide")
            if not bioguide:
                continue
            role = person.get("title") if key == parent else None
            existing = found.get((bioguide, parent))
            if existing and (existing.role or not role):
                continue
            found[(bioguide, parent)] = Membership(bioguide, congress, parent, committee_names.get(parent), role)
    return list(found.values())


def load(raw_root: Path, today: date, pins: dict[int, str]) -> list[Membership]:
    """Memberships for every pinned past Congress that's cached, plus the current one."""
    committee_names = names(raw_root)
    current = congress_of(today)
    files = {c: snapshot_path(raw_root, c) for c in pins if c != current}
    files[current] = reference.path(raw_root, CURRENT)
    memberships = []
    for congress, path in sorted(files.items()):
        if not path.exists():
            log.warning("No committee assignments for Congress %d (%s not cached)", congress, path.name)
            continue
        memberships += parse_membership(yaml.safe_load(path.read_text()), congress, committee_names)
    return memberships


def store(conn: sqlite3.Connection, memberships: list[Membership], today: date) -> None:
    """Replaces committee_memberships and sets members.committees to the current Congress's committee names."""
    conn.execute("DELETE FROM committee_memberships")
    conn.executemany(
        "INSERT INTO committee_memberships (member_id, congress, committee_id, committee_name, role) "
        "VALUES (?, ?, ?, ?, ?)",
        [(m.member_id, m.congress, m.committee_id, m.committee_name, m.role) for m in memberships],
    )
    current = congress_of(today)
    by_member: dict[str, list[str]] = defaultdict(list)
    for m in memberships:
        if m.congress == current:
            by_member[m.member_id].append(m.committee_name or m.committee_id)
    conn.execute("UPDATE members SET committees = NULL")
    conn.executemany("UPDATE members SET committees = ? WHERE member_id = ?",
                     [(json.dumps(sorted(v)), k) for k, v in by_member.items()])


def sector_map(path: Path = SECTORS) -> dict[str, list[tuple[str, str | None]]]:
    found: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            found[row["committee_id"].strip()].append((row["sector"].strip(), row["industry"].strip() or None))
    return dict(found)


def assign(conn: sqlite3.Connection, relevant: dict[str, list[tuple[str, str | None]]]) -> int:
    """Recomputes trades.committee_relevant from committee_memberships. Returns how many trades are relevant."""
    seats: dict[tuple[str, int], set[str]] = defaultdict(set)
    for r in conn.execute("SELECT member_id, congress, committee_id FROM committee_memberships"):
        seats[(r[0], r[1])].add(r[2])
    updates = []
    for r in conn.execute("SELECT trade_id, member_id, tx_date, disclosure_date, sector, industry FROM trades"):
        day = r["tx_date"] or r["disclosure_date"]
        flag = 0
        if r["member_id"] and r["sector"] and day:
            committees = seats.get((r["member_id"], congress_of(date.fromisoformat(day))), set())
            flag = int(any(sector == r["sector"] and (industry is None or industry == r["industry"])
                           for c in committees for sector, industry in relevant.get(c, [])))
        updates.append((flag, r["trade_id"]))
    conn.executemany("UPDATE trades SET committee_relevant = ? WHERE trade_id = ?", updates)
    return sum(flag for flag, _ in updates)
