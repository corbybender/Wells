"""Run traces: record executor runs, replay them as harness regression tests.

Every live failure so far had to be reconstructed by hand (SSH sessions,
copy-pasted TUI output, guesswork about what the model actually said).
Instead: each executor run is recorded — task, every model reply, every tool
call with its result, the stop reason — as one JSON file under
``.wells/traces/``. ``wells replay <trace>`` then re-runs the *harness* over
the recorded model outputs with tool dispatch stubbed to the recorded
results, and reports whether the harness still makes the same decisions
(same tool-call sequence, same stop reason).

That turns any failure in the wild into a one-command, permanent regression
fixture: change the parser/nudges/loop detectors, replay the trace corpus,
see immediately which recorded behaviors changed. The model itself is never
called during replay — this tests the harness, not the model.

Recording is best-effort and never breaks a run. ``WELLS_TRACE=0`` disables;
the newest ``WELLS_TRACE_KEEP`` traces are kept per workspace (default 20).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

TRACE_VERSION = 1
TRACE_SUBDIR = Path(".wells") / "traces"

# Monotonic per-process counter: same-second runs (stepwise mode, parallel
# subagents) must never collide on filename. id()-based suffixes don't work —
# CPython reuses freed object ids within a loop.
_SEQ = 0


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def enabled() -> bool:
    return os.environ.get("WELLS_TRACE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _keep() -> int:
    try:
        return max(1, int(os.environ.get("WELLS_TRACE_KEEP", "20")))
    except ValueError:
        return 20


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_message(m) -> dict:
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    content = getattr(m, "content", "") or ""
    if isinstance(content, list):
        content = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    if isinstance(m, ToolMessage):
        mtype = "tool"
    elif isinstance(m, AIMessage):
        mtype = "ai"
    elif isinstance(m, SystemMessage):
        mtype = "system"
    elif isinstance(m, HumanMessage):
        mtype = "human"
    else:
        mtype = "other"
    out: dict = {"type": mtype, "content": str(content)}
    tcs = getattr(m, "tool_calls", None) or []
    if tcs:
        out["tool_calls"] = [
            {"name": tc.get("name"), "args": tc.get("args") or {}, "id": tc.get("id")}
            for tc in tcs
        ]
    if isinstance(m, ToolMessage):
        out["name"] = m.name
        out["tool_call_id"] = m.tool_call_id
    return out


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


def record_run(*, task: str, workspace: str | None, step_label: str, result) -> Path | None:
    """Write one run's trace under <workspace>/.wells/traces. Never raises."""
    if not enabled():
        return None
    try:
        root = Path(workspace or ".")
        if not root.is_dir():
            return None
        d = root / TRACE_SUBDIR
        d.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^\w-]+", "-", step_label or "run").strip("-")[:40] or "run"
        path = d / f"{ts}-{slug}-{os.getpid()}-{_next_seq():04d}.json"
        data = {
            "version": TRACE_VERSION,
            "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "task": task,
            "step_label": step_label,
            "stopped_reason": result.stopped_reason,
            "steps_taken": result.steps_taken,
            "summary": result.summary,
            "tool_calls": result.tool_calls,
            "messages": [_serialize_message(m) for m in result.messages],
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        _rotate(d)
        return path
    except Exception:
        return None


def _rotate(d: Path) -> None:
    try:
        files = sorted(p for p in d.glob("*.json") if p.is_file())
        for p in files[: max(0, len(files) - _keep())]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def list_traces(workspace: str | None = None) -> list[Path]:
    d = Path(workspace or ".") / TRACE_SUBDIR
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json") if p.is_file())


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay(trace_path: str | Path) -> dict:
    """Re-run the harness over a recorded trace; report behavioral divergence.

    The recorded AI replies are fed back as the scripted model; tool dispatch
    is stubbed to return each call's recorded result (matched by tool name,
    in order). No model and no real tools run. The report compares the
    replayed tool-call sequence and stop reason against the recording —
    ``match`` is True when the harness made the same decisions it made live.
    """
    import tempfile
    from unittest.mock import patch

    from langchain_core.messages import AIMessage

    from wells import config, executor, tools
    from wells.control import CONTROL
    from wells.tokens import LEDGER
    from wells.tools import ToolContext, ToolResult

    trace = json.loads(Path(trace_path).read_text(encoding="utf-8"))

    ai_script: list[AIMessage] = []
    for m in trace.get("messages", []):
        if m.get("type") != "ai":
            continue
        tcs = [
            {"name": tc.get("name"), "args": tc.get("args") or {},
             "id": tc.get("id") or f"replay_{i}"}
            for i, tc in enumerate(m.get("tool_calls") or [])
        ]
        ai_script.append(AIMessage(content=m.get("content", ""), tool_calls=tcs))
    script_it = iter(ai_script)

    def _scripted_invoke(llm, messages):
        try:
            return next(script_it)
        except StopIteration:
            return AIMessage(content="(replay script exhausted)")

    recorded = list(trace.get("tool_calls") or [])
    pending = list(recorded)

    def _recorded_dispatch(name, args, ctx):
        for i, rc in enumerate(pending):
            if rc.get("name") == name:
                rc = pending.pop(i)
                ok = bool(rc.get("ok", True))
                out = rc.get("output_preview", "") or ""
                return ToolResult(ok, out if ok else "", "" if ok else (out or "recorded failure"))
        return ToolResult(True, "(no recorded result for this call)", "")

    LEDGER.reset()
    CONTROL.reset()
    with (
        tempfile.TemporaryDirectory() as td,
        patch.object(config, "_invoke_with_retry", side_effect=_scripted_invoke),
        patch.object(config, "STRUCTURED_OUTPUTS", False),
        patch.object(config, "STREAM_GUARD", False),
        patch.object(config, "ESCALATION_PROFILE", ""),
        # Report native tool support so recorded native tool_calls replay;
        # text-format calls still parse via the fallback path.
        patch.object(executor, "_try_bind_tools", side_effect=lambda llm, ts: llm),
        patch.object(tools, "dispatch", side_effect=_recorded_dispatch),
        patch.dict(os.environ, {"WELLS_TRACE": "0"}),
    ):
        result = executor.run_executor(
            task=trace.get("task", ""),
            ctx=ToolContext(workspace=td, safety="auto"),
            max_steps=0,
            step_label="replay",
        )

    replayed_names = [c.get("name") for c in result.tool_calls]
    recorded_names = [c.get("name") for c in recorded]
    return {
        "trace": str(trace_path),
        "recorded_stopped_reason": trace.get("stopped_reason"),
        "stopped_reason": result.stopped_reason,
        "recorded_steps": trace.get("steps_taken"),
        "steps_taken": result.steps_taken,
        "recorded_calls": recorded_names,
        "calls": replayed_names,
        "match": (
            result.stopped_reason == trace.get("stopped_reason")
            and replayed_names == recorded_names
        ),
    }
