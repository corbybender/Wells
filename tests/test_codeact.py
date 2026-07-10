"""Tests for the CodeAct run_code tool: execution, confinement, guardrails, gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from wells import codeact, tools


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "data.txt").write_text("hello\nworld\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def ctx(workspace: Path) -> tools.ToolContext:
    return tools.ToolContext(workspace=str(workspace), safety="auto")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def test_run_code_prints(ctx: tools.ToolContext):
    r = tools.dispatch("run_code", {"code": "print(2 + 2)"}, ctx)
    assert r.ok
    assert "4" in r.output
    assert "[exit 0]" in r.output


def test_run_code_returns_nonzero_on_error(ctx: tools.ToolContext):
    r = tools.dispatch("run_code", {"code": "raise ValueError('boom')"}, ctx)
    assert not r.ok
    assert "ValueError" in r.output or "ValueError" in r.error


def test_run_code_can_read_workspace_files(ctx: tools.ToolContext, workspace: Path):
    # cwd is the workspace, so relative open() works.
    r = tools.dispatch(
        "run_code",
        {"code": "print(open('data.txt').read().strip())"},
        ctx,
    )
    assert r.ok
    assert "hello" in r.output


def test_run_code_capture_multiple_prints(ctx: tools.ToolContext):
    r = tools.dispatch(
        "run_code",
        {"code": "for i in range(3): print(i)"},
        ctx,
    )
    assert r.ok
    assert "0" in r.output and "1" in r.output and "2" in r.output


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "import os\nos.system('echo hi')",
        "import subprocess\nsubprocess.run(['ls'])",
        "__import__('os').system('rm -rf /')",
    ],
)
def test_run_code_blocks_dangerous_patterns(ctx: tools.ToolContext, snippet: str):
    r = tools.dispatch("run_code", {"code": snippet}, ctx)
    assert not r.ok
    assert "forbidden" in r.error.lower() or "blocked" in r.error.lower()


def test_run_code_blocks_blocked_command_string(ctx: tools.ToolContext):
    # A literal rm -rf / in the code text is caught by the deny-list too.
    r = tools.dispatch("run_code", {"code": "x = 'rm -rf /'"}, ctx)
    assert not r.ok


def test_run_code_requires_code(ctx: tools.ToolContext):
    r = tools.dispatch("run_code", {"code": ""}, ctx)
    assert not r.ok


def test_run_code_truncates_large_output(ctx: tools.ToolContext):
    r = tools.dispatch(
        "run_code", {"code": "print('x' * 50000)"}, ctx
    )
    assert r.ok
    assert len(r.output) < 50000
    assert "truncated" in r.output


# ---------------------------------------------------------------------------
# Safety gating
# ---------------------------------------------------------------------------


def test_run_code_respects_plan_mode(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto", plan_mode=True)
    r = tools.dispatch("run_code", {"code": "print(1)"}, ctx)
    assert r.simulated
    assert "plan" in r.output.lower()


def test_run_code_respects_dryrun(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="dryrun")
    r = tools.dispatch("run_code", {"code": "print(1)"}, ctx)
    assert r.simulated
    assert "dry-run" in r.output.lower()


def test_run_code_auto_mode_executes(ctx: tools.ToolContext):
    r = tools.dispatch("run_code", {"code": "print('ok')"}, ctx)
    assert r.ok
    assert not r.simulated


# ---------------------------------------------------------------------------
# Registration + gating
# ---------------------------------------------------------------------------


def test_run_code_is_registered():
    names = [t.name for t in tools.ALL_TOOLS]
    assert "run_code" in names


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("WELLS_CODEACT", "0")
    assert not codeact.enabled()


def test_direct_handler_call(ctx: tools.ToolContext):
    r = codeact.run_code(ctx, "print(6 * 7)")
    assert r.ok
    assert "42" in r.output
