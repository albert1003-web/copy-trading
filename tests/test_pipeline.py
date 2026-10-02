import json
import plistlib
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from pipeline import report, schedule
from pipeline import run as pipeline
from pipeline.run import StageResult

# 2026-09-30 is a Wednesday; 2026-10-03 a Saturday.
WEDNESDAY = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def ok_stage(conn):
    return StageResult({"new": 1})


def failing_stage(conn):
    return StageResult(errors=["senate search failed"])


def crashing_stage(conn):
    raise RuntimeError("boom")


def warning_stage(conn):
    return StageResult(warnings=["parse: 1 new filing(s) need review"])


@pytest.fixture
def clock():
    return Clock(WEDNESDAY)


def go(conn, clock, stages, *, force=True):
    return pipeline.run(conn, stages=stages, force=force, now=clock)


# --- runner ---------------------------------------------------------------------------------------


def test_every_stage_runs_in_order_even_after_a_failure(conn, clock):
    calls = []

    def tracked(name, stage):
        def wrapper(c):
            calls.append(name)
            return stage(c)
        return name, wrapper

    result = go(conn, clock, [tracked("a", ok_stage), tracked("b", crashing_stage),
                                      tracked("c", failing_stage), tracked("d", warning_stage)])

    assert calls == ["a", "b", "c", "d"]
    assert result.status == "failed"
    assert result.stages["a"] == {"ok": True, "errors": [], "summary": {"new": 1}}
    assert result.stages["b"]["errors"] == ["b: RuntimeError: boom"]
    row = conn.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", (result.run_id,)).fetchone()
    assert (row["status"], row["started_at"], row["finished_at"]) == ("failed", "2026-09-30T15:00:00Z",
                                                                        "2026-09-30T15:00:00Z")
    assert json.loads(row["stages"])["c"]["ok"] is False
    assert json.loads(row["warnings"]) == ["parse: 1 new filing(s) need review"]


def test_warnings_dont_fail_a_run(conn, clock):
    assert go(conn, clock, [("w", warning_stage)]).status == "ok"


@pytest.mark.parametrize("start, wait, expected", [
    (WEDNESDAY, timedelta(minutes=28), False),
    (WEDNESDAY, timedelta(minutes=29), True),
    (SATURDAY, timedelta(minutes=30), False),
    (SATURDAY, timedelta(minutes=118), False),
    (SATURDAY, timedelta(minutes=119), True),
])
def test_due_check(conn, start, wait, expected):
    clock = Clock(start)
    assert go(conn, clock, [("a", ok_stage)], force=False) is not None  # the first run is always due
    clock.now += wait
    assert (go(conn, clock, [("a", ok_stage)], force=False) is not None) is expected


def test_weekend_is_judged_in_eastern_time():
    late_friday_eastern = datetime(2026, 10, 3, 3, 30, tzinfo=UTC)  # Friday 23:30 EDT, already Saturday in UTC
    assert pipeline.interval(late_friday_eastern) == pipeline.WEEKDAY_INTERVAL
    assert pipeline.interval(datetime(2026, 11, 7, 18, 0, tzinfo=UTC)) == pipeline.WEEKEND_INTERVAL  # after DST ends


def test_force_runs_even_when_not_due(conn, clock):
    go(conn, clock, [("a", ok_stage)])
    assert go(conn, clock, [("a", ok_stage)], force=True) is not None


def test_only_one_run_at_a_time(tmp_path):
    lock = tmp_path / "pipeline.lock"
    with pipeline.single_run(lock) as first:
        with pipeline.single_run(lock) as second:
            assert first is True and second is False
    with pipeline.single_run(lock) as again:
        assert again is True


# --- schedule -------------------------------------------------------------------------------------


def test_plist(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_DB_PATH", "/tmp/x.db")
    data = schedule.plist(python="/venv/bin/python", repo=tmp_path, log_dir=tmp_path / "logs")
    assert data["Label"] == "com.tracker.pipeline"
    assert data["ProgramArguments"] == ["/venv/bin/python", "-m", "pipeline.run"]
    assert data["WorkingDirectory"] == str(tmp_path)
    assert data["StartInterval"] == 1800 and data["RunAtLoad"] is True
    assert data["StandardOutPath"] == str(tmp_path / "logs" / "launchd.log")
    assert data["EnvironmentVariables"] == {"TRACKER_DB_PATH": "/tmp/x.db"}


def test_install_and_uninstall_call_launchctl(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_LOG_DIR", str(tmp_path / "logs"))
    calls = []

    def fake(args):
        calls.append(args[0])
        return subprocess.CompletedProcess(args, 0, "", "")

    path = tmp_path / "agent.plist"
    schedule.install(path, run=fake)
    assert plistlib.loads(path.read_bytes())["Label"] == schedule.LABEL
    assert calls == ["bootout", "bootstrap"]
    schedule.uninstall(path, run=fake)
    assert not path.exists() and calls[-1] == "bootout"


def test_install_reports_a_launchctl_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKER_LOG_DIR", str(tmp_path / "logs"))
    fail = lambda args: subprocess.CompletedProcess(args, 5, "", "Input/output error")  # noqa: E731
    with pytest.raises(RuntimeError, match="Input/output error"):
        schedule.install(tmp_path / "agent.plist", run=fail)


# --- report ---------------------------------------------------------------------------------------


def test_report(conn):
    now = datetime.now(UTC).replace(microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    start = now - timedelta(hours=6)
    # One deliberate gap (start+30m -> now-70m, about 4.3 h); the other runs are 30 minutes apart.
    times = [start, start + timedelta(minutes=30), now - timedelta(minutes=70), now - timedelta(minutes=40),
             now - timedelta(minutes=10)]
    conn.executemany("INSERT INTO pipeline_runs (started_at, status) VALUES (?, ?)",
                     [(t.strftime(fmt), "ok" if i != 1 else "failed") for i, t in enumerate(times)])
    seen_h, seen_s = start + timedelta(hours=1), start + timedelta(hours=2)
    conn.executescript(f"""
        INSERT INTO filings (doc_id, chamber, filing_date, first_seen_at) VALUES
          ('BACKFILL', 'house', '2026-01-02', '{(start - timedelta(days=1)).strftime(fmt)}'),
          ('H1', 'house', '{seen_h.date()}', '{seen_h.strftime(fmt)}'),
          ('S1', 'senate', '{seen_s.date()}', '{seen_s.strftime(fmt)}');
    """)
    conn.commit()

    text = report.report(conn, days=14)

    assert "5 runs" in text and "4 ok, 1 failed" in text
    assert "1 gap(s)" in text and "4.3 h" in text
    assert "house  1 filings: same day 100%" in text  # the backfilled filing is excluded
    assert "senate 1 filings: same day 100%" in text
    assert "Alerts: none sent" in text
    assert "house  1 filings" in text  # first seen in the same second as a run still counts


def test_report_before_any_runs(conn):
    assert "no runs yet" in report.report(conn)


# --- nightly stages -------------------------------------------------------------------------------


@pytest.mark.parametrize("moment, day", [
    (datetime(2026, 9, 30, 21, 59, tzinfo=UTC), "2026-09-29"),  # Wed 17:59 EDT: Tuesday's
    (datetime(2026, 9, 30, 22, 0, tzinfo=UTC), "2026-09-30"),  # Wed 18:00 EDT
    (SATURDAY, "2026-10-02"),  # weekends cover Friday
    (datetime(2026, 10, 5, 14, 0, tzinfo=UTC), "2026-10-02"),  # Monday morning: still Friday
    (datetime(2026, 12, 2, 23, 0, tzinfo=UTC), "2026-12-02"),  # Wed 18:00 EST
])
def test_nightly_day(moment, day):
    assert pipeline.nightly_day(moment).isoformat() == day


def test_nightly_stages_run_once_per_evening_and_retry_after_failure(conn):
    calls = []

    def nightly(c):
        calls.append("n")
        return StageResult(errors=["prices: down"] if len(calls) == 1 else [])

    clock = Clock(datetime(2026, 9, 30, 22, 5, tzinfo=UTC))  # Wed 18:05 EDT
    run = lambda: pipeline.run(conn, stages=[("a", ok_stage)], nightly_stages=[("n", nightly)], now=clock)  # noqa: E731
    assert "n" in run().stages and calls == ["n"]  # failed: not marked done
    clock.advance(minutes=30)
    assert run().stages["n"]["ok"] and calls == ["n", "n"]
    clock.advance(minutes=30)
    assert "n" not in run().stages  # done for Wednesday
    clock.advance(hours=24)
    assert "n" in run().stages  # Thursday evening


def test_nightly_flag_forces_or_skips(conn, clock):
    nightly = [("n", ok_stage)]
    assert "n" not in pipeline.run(conn, stages=[], nightly_stages=nightly, nightly=False, now=clock).stages
    clock.advance(hours=1)
    assert "n" in pipeline.run(conn, stages=[], nightly_stages=nightly, nightly=True, now=clock).stages
    clock.advance(hours=1)
    assert "n" in pipeline.run(conn, stages=[], nightly_stages=nightly, nightly=True, now=clock).stages
