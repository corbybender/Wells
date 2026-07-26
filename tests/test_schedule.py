"""Tests for wells schedule: validation, interval translation, wrapper-script
generation, and registry CRUD (OS scheduler calls mocked; a small live group
at the bottom exercises the real Windows Task Scheduler / cron)."""

from __future__ import annotations

import platform
from pathlib import Path
from unittest.mock import patch

import pytest

from wells import schedule


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch):
    """Point every ~/.wells/schedule* path at a per-test tmp_path -- these
    tests must never touch the developer's real registry/scripts/logs."""
    monkeypatch.setattr(schedule, "_REGISTRY", tmp_path / "schedules.json")
    monkeypatch.setattr(schedule, "_SCRIPT_DIR", tmp_path / "schedule-scripts")
    monkeypatch.setattr(schedule, "_LOG_DIR", tmp_path / "schedule-logs")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expect_err",
    [("", True), ("Bad Name", True), ("-leading", True), ("ok-name", False)],
)
def test_validate_name(name, expect_err):
    err = schedule.validate_name(name)
    assert (err is not None) == expect_err


@pytest.mark.parametrize(
    "interval,expect_err",
    [
        ("every15m", False),
        ("every2h", False),
        ("daily@09:30", False),
        ("daily@9:5", True),  # minutes must be 2 digits (09:05, not 9:5) -- avoids ambiguity
        ("bogus", True),
        ("", True),
    ],
)
def test_validate_interval_common(interval, expect_err):
    err = schedule.validate_interval(interval)
    assert (err is not None) == expect_err


def test_validate_interval_raw_cron_platform_dependent(monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Windows")
    assert schedule.validate_interval("0 9 * * *") is not None  # rejected

    monkeypatch.setattr(schedule.platform, "system", lambda: "Linux")
    assert schedule.validate_interval("0 9 * * *") is None  # accepted


# ---------------------------------------------------------------------------
# Interval -> cron translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "interval,expected",
    [
        ("every15m", "*/15 * * * *"),
        ("every2h", "0 */2 * * *"),
        ("daily@09:30", "30 9 * * *"),
        ("0 9 * * *", "0 9 * * *"),  # raw cron passes through
    ],
)
def test_interval_to_cron(interval, expected):
    assert schedule._interval_to_cron(interval) == expected


def test_interval_to_cron_rejects_garbage():
    assert schedule._interval_to_cron("nonsense") is None


# ---------------------------------------------------------------------------
# Wrapper script generation
# ---------------------------------------------------------------------------


def test_wrapper_script_windows_is_valid_here_string(monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Windows")
    monkeypatch.setattr(schedule, "_wells_command", lambda: ["wells.exe"])
    path = schedule._write_wrapper_script("t1", "do the thing\nwith a newline", "C:\\proj")
    assert path.suffix == ".ps1"
    text = path.read_text(encoding="utf-8")
    assert text.startswith('& "wells.exe"')
    assert "@'" in text
    assert text.rstrip().endswith("'@")
    assert "do the thing" in text


def test_wrapper_script_posix_is_valid_heredoc(monkeypatch):
    monkeypatch.setattr(schedule.platform, "system", lambda: "Linux")
    monkeypatch.setattr(schedule, "_wells_command", lambda: ["wells"])
    path = schedule._write_wrapper_script("t1", "do the thing", "/home/me/proj")
    assert path.suffix == ".sh"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "<<'WELLS_GOAL_EOF'" in text
    assert "do the thing" in text


def test_wrapper_script_goal_with_quotes_needs_no_escaping(monkeypatch):
    """The whole point of the heredoc/here-string approach: a goal
    containing quotes and special shell chars needs zero escaping."""
    monkeypatch.setattr(schedule.platform, "system", lambda: "Windows")
    monkeypatch.setattr(schedule, "_wells_command", lambda: ["wells.exe"])
    tricky_goal = 'fix the "bug" in $HOME/`whoami`.py; rm -rf / #evil'
    path = schedule._write_wrapper_script("t2", tricky_goal, "C:\\proj")
    text = path.read_text(encoding="utf-8")
    assert tricky_goal in text  # verbatim, unescaped, inside the here-string body


# ---------------------------------------------------------------------------
# add_schedule / remove_schedule (OS registration mocked)
# ---------------------------------------------------------------------------


@pytest.fixture
def mocked_os_register():
    with (
        patch.object(schedule, "_register_windows", return_value=(True, "mock-registered")),
        patch.object(schedule, "_register_cron", return_value=(True, "mock-registered")),
        patch.object(schedule, "_unregister_windows", return_value=(True, "mock-removed")),
        patch.object(schedule, "_unregister_cron", return_value=(True, "mock-removed")),
    ):
        yield


def test_add_schedule(tmp_path: Path, mocked_os_register):
    ok, msg = schedule.add_schedule("nightly", "do the thing", "every1h", str(tmp_path))
    assert ok, msg
    entry = schedule.by_name("nightly")
    assert entry is not None
    assert entry["interval"] == "every1h"
    assert entry["goal"] == "do the thing"


def test_add_schedule_rejects_bad_name(tmp_path: Path, mocked_os_register):
    ok, msg = schedule.add_schedule("Bad Name", "x", "every1h", str(tmp_path))
    assert not ok


def test_add_schedule_rejects_bad_interval(tmp_path: Path, mocked_os_register):
    ok, msg = schedule.add_schedule("x", "goal", "bogus", str(tmp_path))
    assert not ok


def test_add_schedule_rejects_empty_goal(tmp_path: Path, mocked_os_register):
    ok, msg = schedule.add_schedule("x", "  ", "every1h", str(tmp_path))
    assert not ok


def test_add_schedule_refuses_duplicate(tmp_path: Path, mocked_os_register):
    schedule.add_schedule("dup", "g1", "every1h", str(tmp_path))
    ok, msg = schedule.add_schedule("dup", "g2", "every2h", str(tmp_path))
    assert not ok
    assert "already exists" in msg


def test_add_schedule_rolls_back_wrapper_on_os_register_failure(tmp_path: Path):
    with patch.object(schedule, "_register_windows", return_value=(False, "boom")), \
         patch.object(schedule, "_register_cron", return_value=(False, "boom")):
        ok, msg = schedule.add_schedule("x", "goal", "every1h", str(tmp_path))
    assert not ok
    assert schedule.by_name("x") is None
    # The wrapper script must not be left behind on a failed registration.
    assert not any(schedule._SCRIPT_DIR.glob("x.*"))


def test_remove_schedule(tmp_path: Path, mocked_os_register):
    schedule.add_schedule("nightly", "g", "every1h", str(tmp_path))
    ok, msg = schedule.remove_schedule("nightly")
    assert ok, msg
    assert schedule.by_name("nightly") is None


def test_remove_unknown_schedule_fails(mocked_os_register):
    ok, msg = schedule.remove_schedule("nope")
    assert not ok


def test_list_schedules_empty():
    assert schedule.list_schedules() == []


def test_list_schedules_after_add(tmp_path: Path, mocked_os_register):
    schedule.add_schedule("a", "goal a", "every1h", str(tmp_path))
    schedule.add_schedule("b", "goal b", "daily@09:00", str(tmp_path))
    names = {e["name"] for e in schedule.list_schedules()}
    assert names == {"a", "b"}


# ---------------------------------------------------------------------------
# Cron marker parsing (crontab read/write mocked -- no real crontab touched)
# ---------------------------------------------------------------------------


def test_register_cron_writes_marked_line(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(schedule, "_read_crontab", lambda: "0 0 * * * /existing/job\n")
    written = {}

    def _fake_write(text):
        written["text"] = text
        return True, ""

    monkeypatch.setattr(schedule, "_write_crontab", _fake_write)
    script = tmp_path / "x.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    ok, msg = schedule._register_cron("myname", script, "every15m")
    assert ok, msg
    assert "/existing/job" in written["text"]  # untouched
    assert "# WELLS-SCHEDULE:myname" in written["text"]
    assert "*/15 * * * *" in written["text"]


def test_unregister_cron_removes_only_matching_marker(monkeypatch):
    existing = (
        "0 0 * * * /existing/job\n"
        "*/5 * * * * sh /a.sh >> /a.log 2>&1 # WELLS-SCHEDULE:keep-me\n"
        "*/5 * * * * sh /b.sh >> /b.log 2>&1 # WELLS-SCHEDULE:remove-me\n"
    )
    monkeypatch.setattr(schedule, "_read_crontab", lambda: existing)
    written = {}

    def _fake_write(text):
        written["text"] = text
        return True, ""

    monkeypatch.setattr(schedule, "_write_crontab", _fake_write)
    ok, msg = schedule._unregister_cron("remove-me")
    assert ok, msg
    assert "keep-me" in written["text"]
    assert "remove-me" not in written["text"]
    assert "/existing/job" in written["text"]


def test_unregister_cron_missing_entry_is_ok(monkeypatch):
    monkeypatch.setattr(schedule, "_read_crontab", lambda: "0 0 * * * /existing/job\n")
    ok, msg = schedule._unregister_cron("never-existed")
    assert ok
    assert "no cron entry found" in msg.lower()


# ---------------------------------------------------------------------------
# Live: real Windows Task Scheduler (skipped everywhere else)
# ---------------------------------------------------------------------------

requires_windows_scheduler = pytest.mark.skipif(
    platform.system() != "Windows", reason="Windows Task Scheduler only"
)


@requires_windows_scheduler
def test_live_add_and_remove_schedule_windows(tmp_path: Path):
    import subprocess

    name = "wells-pytest-live-verify"
    ok, msg = schedule.add_schedule(name, "pytest live verification goal", "every1h", str(tmp_path))
    assert ok, msg
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/TN", f"Wells\\{name}"],
            capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 0, r.stderr
    finally:
        ok2, msg2 = schedule.remove_schedule(name)
        assert ok2, msg2
        r2 = subprocess.run(
            ["schtasks", "/query", "/TN", f"Wells\\{name}"],
            capture_output=True, text=True, timeout=15,
        )
        assert r2.returncode != 0  # confirmed gone
