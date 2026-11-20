"""Tests for Code Mode (the `program` tool): composition, budgets, guardrails."""

from __future__ import annotations

from pathlib import Path

import pytest

from wells import codemode, tools


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def ctx(workspace: Path) -> tools.ToolContext:
    return tools.ToolContext(workspace=str(workspace), safety="auto")


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_program_composes_tool_calls(ctx: tools.ToolContext):
    r = tools.dispatch(
        "program",
        {"code": 'names = tools.list_dir(path=".")\nRESULT = {"entries": len(names.splitlines())}'},
        ctx,
    )
    assert r.ok, r.error
    assert "2 tool calls" not in r.output  # one call
    assert "RESULT" in r.output
    assert "entries" in r.output


def test_program_stdout_and_result(ctx: tools.ToolContext):
    r = tools.dispatch("program", {"code": 'print("hello")\nRESULT = [1, 2, 3]'}, ctx)
    assert r.ok
    assert "hello" in r.output
    assert "[1, 2, 3]" in r.output


def test_program_chains_dependent_lookups(ctx: tools.ToolContext):
    code = (
        "listing = tools.list_dir(path='.')\n"
        "first = listing.splitlines()[0]\n"
        "content = tools.read_file(path=first)\n"
        "RESULT = content.strip()\n"
    )
    r = tools.dispatch("program", {"code": code}, ctx)
    assert r.ok, r.error
    assert "alpha" in r.output or "beta" in r.output


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def test_program_denies_subprocess_pattern(ctx: tools.ToolContext):
    r = tools.dispatch("program", {"code": "import subprocess"}, ctx)
    assert not r.ok


def test_program_blocks_unsafe_import(ctx: tools.ToolContext):
    r = tools.dispatch("program", {"code": "import os\nRESULT = os.getcwd()"}, ctx)
    assert not r.ok
    assert "not allowed" in r.output or "not allowed" in r.error


def test_program_allows_safe_import(ctx: tools.ToolContext):
    r = tools.dispatch("program", {"code": "import json\nRESULT = {'a': 1}"}, ctx)
    assert r.ok, r.error
    assert '"a": 1' in r.output


def test_program_blocks_open(ctx: tools.ToolContext):
    r = tools.dispatch("program", {"code": "open('a.txt').read()"}, ctx)
    assert not r.ok


def test_program_reports_inner_tool_failure(ctx: tools.ToolContext):
    r = tools.dispatch(
        "program", {"code": "tools.read_file(path='nope/missing.txt')"}, ctx
    )
    assert not r.ok
    assert "read_file" in r.output or "read_file" in r.error


def test_program_budget_exhausted(ctx: tools.ToolContext, monkeypatch):
    monkeypatch.setattr(codemode, "_MAX_CALLS", 2)
    code = "for i in range(5):\n    tools.list_dir(path='.')\n"
    r = tools.dispatch("program", {"code": code}, ctx)
    assert not r.ok
    assert "budget" in r.error


def test_program_plan_mode_simulated(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto", plan_mode=True)
    r = tools.dispatch("program", {"code": "print('x')"}, ctx)
    assert r.ok and r.simulated
    assert "[plan]" in r.output


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_program_tool_registered():
    names = {t.name for t in tools.registry()}
    if codemode.enabled():
        assert "program" in names
        assert "program" not in {t.name for t in tools.registry(include_mutating=False)}
