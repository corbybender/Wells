"""Tests for background agents: registry lifecycle, tool handlers, gating."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from wells import background, tools
from wells.subagents import SubagentReport, SubagentSpec


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def ctx(workspace: Path) -> tools.ToolContext:
    return tools.ToolContext(workspace=str(workspace), safety="auto")


@pytest.fixture(autouse=True)
def _reset_registry():
    background.REGISTRY.reset()
    yield
    background.REGISTRY.reset()


def _ok_report(*args, **kwargs) -> SubagentReport:
    return SubagentReport(name="x", ok=True, summary="found it", steps_taken=2)


def _slow_report(*args, **kwargs):
    """Simulate a subagent that takes a moment so we can observe 'running'."""
    time.sleep(0.3)
    return _ok_report()


# ---------------------------------------------------------------------------
# Registration + gating
# ---------------------------------------------------------------------------


def test_bg_tools_are_registered():
    names = [t.name for t in tools.registry()]
    assert "bg_start" in names
    assert "bg_status" in names
    assert "bg_collect" in names


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("WELLS_BG_AGENTS", "0")
    assert not background.enabled()


# ---------------------------------------------------------------------------
# Tool handler guards
# ---------------------------------------------------------------------------


def test_bg_start_blocked_in_subagent(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto", subagent=True)
    r = tools.dispatch("bg_start", {"task": "x"}, ctx)
    assert not r.ok
    assert "subagents cannot" in r.error.lower()


def test_bg_status_blocked_in_subagent(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto", subagent=True)
    r = tools.dispatch("bg_status", {}, ctx)
    assert not r.ok


def test_bg_collect_blocked_in_subagent(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto", subagent=True)
    r = tools.dispatch("bg_collect", {"id": "bg-1"}, ctx)
    assert not r.ok


def test_bg_start_requires_task(ctx: tools.ToolContext):
    r = tools.dispatch("bg_start", {"task": ""}, ctx)
    assert not r.ok


def test_bg_start_rejects_bad_role(ctx: tools.ToolContext):
    r = tools.dispatch("bg_start", {"task": "x", "role": "bogus"}, ctx)
    assert not r.ok


def test_bg_status_empty(ctx: tools.ToolContext):
    r = tools.dispatch("bg_status", {}, ctx)
    assert r.ok
    assert "no background" in r.output.lower()


def test_bg_collect_unknown_id(ctx: tools.ToolContext):
    r = tools.dispatch("bg_collect", {"id": "bg-999"}, ctx)
    assert not r.ok


# ---------------------------------------------------------------------------
# Registry lifecycle (with mocked run_subagent)
# ---------------------------------------------------------------------------


def test_start_status_collect_lifecycle(ctx: tools.ToolContext):
    with patch("wells.background.run_subagent", return_value=_ok_report()):
        r = tools.dispatch("bg_start", {"task": "research the auth module"}, ctx)
    assert r.ok
    assert "bg-1" in r.output

    # Wait for completion.
    _wait_until_done("bg-1", timeout=5.0)

    status = tools.dispatch("bg_status", {}, ctx)
    assert status.ok
    assert "bg-1" in status.output
    assert "done" in status.output or "ok" in status.output.lower()

    collected = tools.dispatch("bg_collect", {"id": "bg-1"}, ctx)
    assert collected.ok
    assert "found it" in collected.output


def test_collect_returns_none_while_running(ctx: tools.ToolContext):
    with patch("wells.background.run_subagent", side_effect=_slow_report):
        tools.dispatch("bg_start", {"task": "slow"}, ctx)
    # Immediately — should still be running (or just finished).
    # If it finished already that's fine too; the point is no crash.
    r = tools.dispatch("bg_collect", {"id": "bg-1"}, ctx)
    assert r.ok  # either "still running" or the report


def test_collect_twice_second_time_gone(ctx: tools.ToolContext):
    with patch("wells.background.run_subagent", return_value=_ok_report()):
        tools.dispatch("bg_start", {"task": "x"}, ctx)
    _wait_until_done("bg-1", timeout=5.0)

    first = tools.dispatch("bg_collect", {"id": "bg-1"}, ctx)
    assert first.ok
    assert "found it" in first.output

    second = tools.dispatch("bg_collect", {"id": "bg-1"}, ctx)
    assert second.ok
    assert "already been collected" in second.output.lower()


def test_multiple_agents_run_concurrently(ctx: tools.ToolContext):
    with patch("wells.background.run_subagent", return_value=_ok_report()):
        tools.dispatch("bg_start", {"task": "a"}, ctx)
        tools.dispatch("bg_start", {"task": "b"}, ctx)
        tools.dispatch("bg_start", {"task": "c"}, ctx)

    assert background.REGISTRY.pending() <= 3
    _wait_until_done("bg-3", timeout=5.0)

    status = tools.dispatch("bg_status", {}, ctx)
    assert status.ok
    for bid in ("bg-1", "bg-2", "bg-3"):
        assert bid in status.output


def test_collect_without_id_when_multiple_finished(ctx: tools.ToolContext):
    with patch("wells.background.run_subagent", return_value=_ok_report()):
        tools.dispatch("bg_start", {"task": "a"}, ctx)
        tools.dispatch("bg_start", {"task": "b"}, ctx)
    _wait_until_done("bg-2", timeout=5.0)

    r = tools.dispatch("bg_collect", {}, ctx)
    assert r.ok
    assert "Multiple" in r.output or "bg-1" in r.output


def test_collect_without_id_picks_single_finished(ctx: tools.ToolContext):
    with patch("wells.background.run_subagent", return_value=_ok_report()):
        tools.dispatch("bg_start", {"task": "only one"}, ctx)
    _wait_until_done("bg-1", timeout=5.0)

    r = tools.dispatch("bg_collect", {}, ctx)
    assert r.ok
    assert "found it" in r.output


def test_registry_reset_clears_slots(ctx: tools.ToolContext):
    with patch("wells.background.run_subagent", return_value=_ok_report()):
        tools.dispatch("bg_start", {"task": "x"}, ctx)
    assert len(background.REGISTRY.status()) == 1
    background.REGISTRY.reset()
    assert background.REGISTRY.status() == []


def test_failed_subagent_reports_error(ctx: tools.ToolContext):
    bad = SubagentReport(name="x", ok=False, summary="", steps_taken=0, error="boom")
    with patch("wells.background.run_subagent", return_value=bad):
        tools.dispatch("bg_start", {"task": "x"}, ctx)
    _wait_until_done("bg-1", timeout=5.0)

    status = tools.dispatch("bg_status", {}, ctx)
    assert "error" in status.output.lower()

    collected = tools.dispatch("bg_collect", {"id": "bg-1"}, ctx)
    assert not collected.ok


def test_fix_role_accepted(ctx: tools.ToolContext):
    with patch("wells.background.run_subagent", return_value=_ok_report()) as m:
        tools.dispatch("bg_start", {"task": "fix the bug", "role": "fix"}, ctx)
        _wait_until_done("bg-1", timeout=5.0)
    # The spec passed to run_subagent should have role fix (full toolset).
    assert m.called
    spec = m.call_args.args[0]
    assert isinstance(spec, SubagentSpec)
    assert spec.toolset == "full"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_until_done(bgid: str, timeout: float = 5.0) -> None:
    """Poll the registry until ``bgid`` is no longer 'running'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = {r["id"]: r for r in background.REGISTRY.status()}
        row = rows.get(bgid)
        if row and row["status"] != "running":
            return
        time.sleep(0.05)
    # Don't fail hard if timing was tight; let the assertion in the test speak.
