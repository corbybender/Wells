"""Tests for run-trace recording and replay (the harness regression harness)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from wells import config, executor, tools, traces
from wells.executor import ExecutorResult
from wells.tokens import LEDGER


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "maths.py").write_text("def add(a, b):\n    return a - b\n")
    return tmp_path


@pytest.fixture
def ctx(workspace: Path) -> tools.ToolContext:
    return tools.ToolContext(workspace=str(workspace), safety="auto")


def _scripted(responses):
    it = iter(responses)

    def _fake(llm, messages):
        try:
            return next(it)
        except StopIteration:
            return AIMessage(content="(done)")

    return _fake


# ---------------------------------------------------------------------------
# Serialization + record
# ---------------------------------------------------------------------------


def test_serialize_message_types():
    assert traces._serialize_message(SystemMessage(content="s"))["type"] == "system"
    assert traces._serialize_message(HumanMessage(content="h"))["type"] == "human"
    ai = traces._serialize_message(AIMessage(
        content="a", tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "1"}]
    ))
    assert ai["type"] == "ai"
    assert ai["tool_calls"][0]["name"] == "read_file"
    tm = traces._serialize_message(ToolMessage(content="out", tool_call_id="1", name="read_file"))
    assert tm["type"] == "tool" and tm["name"] == "read_file"


def test_record_run_writes_trace_and_rotates(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WELLS_TRACE", "1")
    monkeypatch.setenv("WELLS_TRACE_KEEP", "3")
    for i in range(5):
        r = ExecutorResult(summary=f"run {i}", steps_taken=i, stopped_reason="done",
                           messages=[AIMessage(content=f"reply {i}")])
        p = traces.record_run(task=f"task {i}", workspace=str(tmp_path),
                              step_label="t", result=r)
        assert p is not None and p.is_file()
    left = traces.list_traces(str(tmp_path))
    assert len(left) == 3  # oldest rotated away
    data = json.loads(left[-1].read_text(encoding="utf-8"))
    assert data["task"] == "task 4"
    assert data["stopped_reason"] == "done"
    assert data["messages"][0]["type"] == "ai"


def test_record_run_disabled_by_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WELLS_TRACE", "0")
    r = ExecutorResult(summary="x", stopped_reason="done")
    assert traces.record_run(task="t", workspace=str(tmp_path),
                             step_label="t", result=r) is None
    assert traces.list_traces(str(tmp_path)) == []


def test_record_run_never_raises_on_bad_workspace():
    r = ExecutorResult(summary="x", stopped_reason="done")
    assert traces.record_run(task="t", workspace="Z:/definitely/not/a/dir",
                             step_label="t", result=r) is None


# ---------------------------------------------------------------------------
# End-to-end: a real (scripted) run records a trace that replays to a match
# ---------------------------------------------------------------------------


def _run_recorded(ctx, monkeypatch):
    monkeypatch.setenv("WELLS_TRACE", "1")
    LEDGER.reset()
    script = [
        AIMessage(content='<tool_call>{"name": "read_file", "args": {"path": "maths.py"}}</tool_call>'),
        AIMessage(content='<tool_call>{"name": "edit_file", "args": {"path": "maths.py", "old_string": "return a - b", "new_string": "return a + b"}}</tool_call>'),
        AIMessage(content="Fixed the add bug."),
    ]
    with (
        patch.object(config, "_invoke_with_retry", side_effect=_scripted(script)),
        patch.object(config, "STRUCTURED_OUTPUTS", False),
        patch.object(executor, "_try_bind_tools", return_value=None),
    ):
        result = executor.run_executor(task="fix maths.py", ctx=ctx,
                                       max_steps=5, step_label="t")
    return result


def test_executor_run_records_a_trace(ctx: tools.ToolContext, workspace: Path, monkeypatch):
    result = _run_recorded(ctx, monkeypatch)
    assert result.stopped_reason == "done"
    found = traces.list_traces(str(workspace))
    assert len(found) == 1
    data = json.loads(found[0].read_text(encoding="utf-8"))
    assert data["task"] == "fix maths.py"
    assert [c["name"] for c in data["tool_calls"]] == ["read_file", "edit_file"]


def test_replay_matches_recorded_behavior(ctx: tools.ToolContext, workspace: Path, monkeypatch):
    _run_recorded(ctx, monkeypatch)
    trace_path = traces.list_traces(str(workspace))[0]
    report = traces.replay(trace_path)
    assert report["calls"] == report["recorded_calls"] == ["read_file", "edit_file"]
    assert report["stopped_reason"] == report["recorded_stopped_reason"] == "done"
    assert report["match"] is True


def test_replay_flags_divergence(ctx: tools.ToolContext, workspace: Path, monkeypatch):
    """A tampered recording (different stop reason than the harness now
    produces) must be reported as a divergence, not silently passed."""
    _run_recorded(ctx, monkeypatch)
    trace_path = traces.list_traces(str(workspace))[0]
    data = json.loads(trace_path.read_text(encoding="utf-8"))
    data["stopped_reason"] = "stuck_loop"  # pretend live behavior differed
    trace_path.write_text(json.dumps(data), encoding="utf-8")
    report = traces.replay(trace_path)
    assert report["match"] is False


def test_replay_does_not_touch_the_real_workspace(
    ctx: tools.ToolContext, workspace: Path, monkeypatch
):
    """Replay stubs dispatch — the recorded edit_file must NOT re-apply."""
    _run_recorded(ctx, monkeypatch)
    (workspace / "maths.py").write_text("SENTINEL = 1\n")
    trace_path = traces.list_traces(str(workspace))[0]
    traces.replay(trace_path)
    assert (workspace / "maths.py").read_text() == "SENTINEL = 1\n"
