"""Run the pipeline on a schedule with launchd: python -m pipeline.schedule install | uninstall | status

Installs a LaunchAgent that starts `python -m pipeline.run` every 30 minutes (and at login). The run itself
decides whether it's due (30 min on weekdays, 2 h on weekends), so the agent can fire often. launchd runs a
missed interval once after the Mac wakes; nothing runs while it's asleep or off.
"""

import argparse
import os
import plistlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from common import config
from db import connect

LABEL = "com.tracker.pipeline"
INTERVAL_SECONDS = 1800
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def launchctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def plist(python: str = sys.executable, repo: Path = config.REPO_ROOT, log_dir: Path | None = None) -> dict:
    log_dir = log_dir or config.log_dir()
    env = {k: v for k, v in os.environ.items() if k.startswith("TRACKER_")}  # keep any path overrides
    data = {
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "pipeline.run"],
        "WorkingDirectory": str(repo),
        "StartInterval": INTERVAL_SECONDS,
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / "launchd.log"),
        "StandardErrorPath": str(log_dir / "launchd.log"),
        "ProcessType": "Background",
    }
    if env:
        data["EnvironmentVariables"] = env
    return data


def domain() -> str:
    return f"gui/{os.getuid()}"


def install(path: Path = PLIST, run: Runner = launchctl) -> str:
    config.log_dir().mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist()))
    run(["bootout", f"{domain()}/{LABEL}"])  # not loaded yet is fine
    result = run(["bootstrap", domain(), str(path)])
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.returncode}")
    return f"Installed {path}; the pipeline runs every 30 minutes (2 hours on weekends) while the Mac is awake."


def uninstall(path: Path = PLIST, run: Runner = launchctl) -> str:
    run(["bootout", f"{domain()}/{LABEL}"])
    if path.exists():
        path.unlink()
    return "Uninstalled; the pipeline no longer runs on a schedule."


def status(path: Path = PLIST, run: Runner = launchctl) -> str:
    result = run(["print", f"{domain()}/{LABEL}"])
    if result.returncode == 0:
        details: dict[str, str] = {}  # the job's own fields come first; nested sections repeat "state ="
        for line in result.stdout.splitlines():
            key, _, value = line.strip().partition(" = ")
            if key in ("state", "runs", "last exit code") and key not in details:
                details[key] = value
        lines = [f"Scheduled ({path}): " + "; ".join(f"{k} {v}" for k, v in details.items())]
    else:
        lines = ["Not scheduled. Install with: python -m pipeline.schedule install"]
    last = connect().execute(
        "SELECT started_at, finished_at, status FROM pipeline_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    lines.append(f"Last run: {last['started_at']} ({last['status']})" if last else "Last run: never")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["install", "uninstall", "status"])
    args = parser.parse_args(argv)
    try:
        print({"install": install, "uninstall": uninstall, "status": status}[args.command]())
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
