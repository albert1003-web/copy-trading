"""Reference files for enrichment, cached in <raw_dir>/reference/ and refreshed at most weekly.

  legislators-*.json   github.com/unitedstates/congress-legislators (members, terms, districts)
  committee*.yaml      the same project: committee names and current assignments (enrich/committees.py)
  *listed.txt          Nasdaq Trader symbol directory (Nasdaq, NYSE, NYSE American/Arca; ETF flag)

A failed refresh falls back to the cached copy, so a source outage never blocks enrichment.
"""

import json
import logging
import time
from pathlib import Path

import httpx

from common import http as polite

log = logging.getLogger("enrich.reference")

LEGISLATORS = "https://unitedstates.github.io/congress-legislators/"
SYMBOLS = "https://www.nasdaqtrader.com/dynamic/SymDir/"
SOURCES = {
    "legislators-current.json": LEGISLATORS + "legislators-current.json",
    "legislators-historical.json": LEGISLATORS + "legislators-historical.json",
    "committees-current.yaml": LEGISLATORS + "committees-current.yaml",
    "committees-historical.yaml": LEGISLATORS + "committees-historical.yaml",
    "committee-membership-current.yaml": LEGISLATORS + "committee-membership-current.yaml",
    "nasdaqlisted.txt": SYMBOLS + "nasdaqlisted.txt",
    "otherlisted.txt": SYMBOLS + "otherlisted.txt",
}
MAX_AGE_SECONDS = 7 * 24 * 3600


def path(raw_root: Path, name: str) -> Path:
    return raw_root / "reference" / name


def refresh(http: httpx.Client, raw_root: Path, *, now: float | None = None) -> None:
    """Downloads each reference file that is missing or older than a week."""
    now = time.time() if now is None else now
    for name, url in SOURCES.items():
        target = path(raw_root, name)
        if target.exists() and now - target.stat().st_mtime < MAX_AGE_SECONDS:
            continue
        try:
            response = polite.request(http, "GET", url)
            if response.status_code != 200 or not _valid(name, response.content):
                raise ValueError(f"HTTP {response.status_code}, {len(response.content)} bytes")
        except (httpx.HTTPError, ValueError) as e:
            if not target.exists():
                raise RuntimeError(f"can't download {name} and there is no cached copy: {e}") from e
            log.warning("Refreshing %s failed (%s); using the cached copy", name, e)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(response.content)
        tmp.replace(target)
        log.info("Refreshed %s", name)


def _valid(name: str, content: bytes) -> bool:
    if name.endswith(".yaml"):
        import yaml

        try:
            return isinstance(yaml.safe_load(content), (list, dict))
        except yaml.YAMLError:
            return False
    if name.endswith(".json"):
        try:
            return isinstance(json.loads(content), list)
        except ValueError:
            return False
    return content.startswith((b"Symbol|", b"ACT Symbol|"))


def legislators(raw_root: Path) -> tuple[list[dict], list[dict]]:
    """(current, historical) legislator records."""
    load = lambda name: json.loads(path(raw_root, name).read_text())  # noqa: E731
    return load("legislators-current.json"), load("legislators-historical.json")


def symbols(raw_root: Path) -> dict[str, bool]:
    """Listed symbol -> is ETF. Symbols use the files' own form (BRK.B, CADE$A); test issues are skipped."""
    listed: dict[str, bool] = {}
    for name, column in (("nasdaqlisted.txt", "Symbol"), ("otherlisted.txt", "ACT Symbol")):
        lines = path(raw_root, name).read_text().splitlines()
        header = lines[0].split("|")
        for line in lines[1:]:
            fields = dict(zip(header, line.split("|"), strict=False))
            if not fields.get(column) or line.startswith("File Creation Time") or fields.get("Test Issue") == "Y":
                continue
            listed[fields[column]] = fields.get("ETF") == "Y"
    return listed
