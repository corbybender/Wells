"""Tests for the BigBrain memory bridge: availability, write/compile/recall."""

from __future__ import annotations

from pathlib import Path

import pytest

from wells import memory_bridge, tools


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src.txt").write_text("x\n", encoding="utf-8")
    return tmp_path


def _need_bigbrain():
    if not memory_bridge.BIGBRAIN_AVAILABLE:
        pytest.skip("bigbrain-memory not installed")


# ---------------------------------------------------------------------------
# Write -> compile -> recall lifecycle
# ---------------------------------------------------------------------------


def test_log_lesson_and_compile(workspace: Path):
    _need_bigbrain()
    line = memory_bridge.log_lesson(
        workspace,
        goal="fix pagination",
        summary="Offset pagination broke past page 100; switched to cursor.",
        key_files=["src/api.py"],
        gotchas=["Always index the cursor column before shipping."],
    )
    assert line  # appended to log.md
    result = memory_bridge.compile(workspace)
    assert not result.get("error")
    # Compiled memory exists and mentions the lesson.
    bb_dir = workspace / ".bigbrain"
    assert bb_dir.is_dir()
    mem = "\n".join(p.read_text(encoding="utf-8") for p in (bb_dir / "memory").glob("*.md"))
    assert "cursor" in mem.lower()
    assert "index the cursor column" in mem


def test_recall_finds_compiled_lesson(workspace: Path):
    _need_bigbrain()
    memory_bridge.log_lesson(
        workspace, goal="g", summary="We chose sqlite-vec over FAISS for embeddings."
    )
    memory_bridge.compile(workspace)
    out = memory_bridge.recall(workspace, "embeddings")
    assert "sqlite-vec" in out or "FAISS" in out.upper()


def test_log_lesson_disabled(monkeypatch, workspace: Path):
    monkeypatch.setenv("WELLS_BIGBRAIN", "0")
    assert memory_bridge.log_lesson(workspace, goal="g", summary="s") is None
    assert memory_bridge.enabled() is False


def test_memory_search_tool_registered_when_available():
    if not memory_bridge.enabled():
        pytest.skip("bigbrain disabled/unavailable")
    names = {t.name for t in tools.registry(include_mutating=False)}
    assert "memory_search" in names  # read-only toolset: planner can recall


def test_memory_search_tool_dispatch(workspace: Path):
    _need_bigbrain()
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto")
    r = tools.dispatch("memory_search", {"query": "anything"}, ctx)
    assert r.ok
    assert "memory" in r.output.lower() or "no project memory" in r.output.lower()


# ---------------------------------------------------------------------------
# Finisher wiring
# ---------------------------------------------------------------------------


def test_finisher_writes_bigbrain(workspace: Path, monkeypatch):
    _need_bigbrain()
    from wells import finisher

    monkeypatch.chdir(workspace)
    state = {
        "workspace_root": str(workspace),
        "goal": "fix pagination",
        "implementation_steps": "Changed src/api.py to cursor pagination.",
        "review_result": "Beware: cursor column must be indexed.",
        "review_complete": True,
    }
    finisher.finisher(state)
    bb = workspace / ".bigbrain"
    assert bb.is_dir()
    compiled = "\n".join(p.read_text(encoding="utf-8") for p in (bb / "memory").glob("*.md"))
    assert "cursor" in compiled.lower()
