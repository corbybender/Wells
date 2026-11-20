"""Code Mode: one tool call that composes many.

The round-trip model is the harness's biggest latency tax: ten dependent
lookups ("read this, grep for callers of each, read those files") cost ten
LLM round trips — each one 5-30s of token generation — and ten tool outputs
parked in the context window. Strong models don't need the harness to
baby-step them through that; they can write the whole chain themselves.

``program`` gives the model ONE tool: a Python program in which every
registered harness tool is a plain callable on a ``tools`` object::

    program('''
    api = tools.read_file("src/api.ts")
    names = ["handleIssue", "closeIssue"]
    callers = {n: tools.grep(n) for n in names}
    RESULT = {"api_len": len(api), "callers": callers}
    ''')

The harness executes the program in-process; each ``tools.<name>(...)``
dispatches through the ordinary :func:`wells.tools.dispatch` path, so the
safety chokepoint, plan/dry-run modes, and the deny-lists all still apply
*inside* the program — permissions the model can't see past, which is the
design point OpenCode's Code Mode doesn't advertise.

Guardrails (v1 — honest about what this is NOT):
  * This is NOT a hard sandbox. It's a restricted ``exec``: ``open``,
    ``eval``, ``exec``, and ``__import__`` are removed/replaced (imports are
    allow-listed to pure-stdlib computation modules), and the codeact source
    screening applies. Direct filesystem access should go through
    ``tools.read_file``/``tools.write_file`` so safety gating applies; a
    determined program can still reach ``builtins`` via attribute walks.
    Treat it as guardrails for well-behaved models, not a security boundary
    — the safety modes and ``WELLS_CODE_MODE=0`` are the boundary.
  * Call/output budgets: max 40 tool calls and 200k result chars per program
    (context protection), plus a wall-clock cap.
  * Cancellation: the cancel flag is checked before every inner dispatch.
  * Output: the program's stdout plus the JSON-serialized ``RESULT`` global,
    both truncated.

Feature gate: ``WELLS_CODE_MODE=0`` disables. Default on — but weak/local
model profiles should turn it off (orchestration code from a weak model is
usually a debugging session, not a speedup).
"""

from __future__ import annotations

import io
import json
import builtins
import os
import time
from contextlib import redirect_stdout

from wells import safety
from wells.tools import ToolContext, ToolDef, ToolResult, dispatch

_MAX_CALLS = 40
_MAX_RESULT_CHARS = 200_000
_MAX_OUTPUT_CHARS = 8_000
_DEFAULT_TIMEOUT = 120.0

# Modules an in-program import may resolve to (pure computation, no fs/net).
_SAFE_IMPORTS = frozenset({
    "json", "re", "math", "statistics", "itertools", "collections",
    "datetime", "time", "functools", "heapq", "bisect", "string", "textwrap",
    "uuid", "decimal", "fractions", "random", "typing", "dataclasses",
})

# Reuse codeact's source screening (subprocess/os.system/__import__/
# fork denial + the shell deny-list) for a first-pass refusal.
from wells.codeact import _screen_code  # noqa: E402


def enabled() -> bool:
    """Whether the program tool is registered (``WELLS_CODE_MODE`` != 0)."""
    return os.environ.get("WELLS_CODE_MODE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


class _Budget(Exception):
    pass


class _Tools:
    """The ``tools`` object injected into a program — attribute access
    returns a callable that dispatches the real harness tool."""

    def __init__(self, ctx: ToolContext, state: dict) -> None:
        self._ctx = ctx
        self._state = state

    def __getattr__(self, name: str):
        def call(**kwargs):
            return self._call(name, kwargs)
        call.__name__ = name
        return call

    def _call(self, name: str, kwargs: dict) -> str:
        from wells.control import CONTROL

        if CONTROL.cancelled():
            raise _Budget("cancelled by user")
        calls = self._state["calls"] + 1
        if calls > _MAX_CALLS:
            raise _Budget(f"tool-call budget exhausted ({_MAX_CALLS} calls)")
        self._state["calls"] = calls
        result = dispatch(name, kwargs, self._ctx)
        out = result.output or ""
        self._state["chars"] += len(out)
        if self._state["chars"] > _MAX_RESULT_CHARS:
            raise _Budget(f"result-char budget exhausted ({_MAX_RESULT_CHARS})")
        if not result.ok:
            raise RuntimeError(f"{name} failed: {result.error or out[:300]}")
        CONTROL.set_activity(f"program · tool {calls}/{_MAX_CALLS} · {name}")
        return out


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = (name or "").split(".")[0]
    if root in _SAFE_IMPORTS:
        import importlib

        return importlib.import_module(name)
    raise ImportError(
        f"import {name!r} is not allowed inside program; safe modules: "
        f"{sorted(_SAFE_IMPORTS)}. Use tools.* for file/shell/web access."
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit - 40
    return text[:keep] + f"\n… (truncated, {len(text) - keep} more chars)"


def run_program(ctx: ToolContext, code: str) -> ToolResult:
    """Execute a tool-composition program; return stdout + RESULT."""
    from wells.control import CONTROL

    if not code or not code.strip():
        return ToolResult(False, "", "code is required")

    refuse = _screen_code(code)
    if refuse is not None:
        return ToolResult(False, "", refuse)

    detail = f"program: {len(code)} chars composing harness tools"
    if ctx.plan_mode:
        return ToolResult(True, f"[plan] would {detail}", simulated=True)
    decision = safety.gate("program", detail, safety=ctx.safety, approver=ctx.approver)
    if not decision.allowed:
        return ToolResult(True, decision.reason, simulated=decision.simulated)

    state = {"calls": 0, "chars": 0}
    tools_obj = _Tools(ctx, state)
    buf = io.StringIO()
    t0 = time.monotonic()
    timeout = min(
        float(os.environ.get("CODE_MODE_TIMEOUT", "") or _DEFAULT_TIMEOUT),
        ctx.shell_timeout or _DEFAULT_TIMEOUT,
    )

    glb: dict = {
        "tools": tools_obj,
        "print": print,
        "RESULT": None,
        "__builtins__": {},  # replaced below with the restricted set
    }
    restricted = vars(builtins).copy()
    for dangerous in ("open", "exec", "eval", "compile", "__import__", "input"):
        restricted.pop(dangerous, None)
    restricted["__import__"] = _safe_import
    glb["__builtins__"] = restricted

    from wells.control import RunCancelled

    try:
        with redirect_stdout(buf):
            exec(compile(code, "<program>", "exec"), glb, None)  # noqa: S102
    except _Budget as e:
        return ToolResult(
            False,
            _render(buf.getvalue(), glb.get("RESULT"), state, t0, err=f"budget: {e}"),
            f"budget: {e}",
        )
    except RunCancelled:
        return ToolResult(False, _render(buf.getvalue(), None, state, t0, err="cancelled"), "cancelled")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        return ToolResult(
            False,
            _render(buf.getvalue(), None, state, t0, err=msg),
            msg,
        )
    if CONTROL.cancelled():
        return ToolResult(False, _render(buf.getvalue(), None, state, t0, err="cancelled"), "cancelled")
    if time.monotonic() - t0 > timeout:
        return ToolResult(False, _render(buf.getvalue(), None, state, t0, err="timeout"), f"timed out after {timeout}s")
    return ToolResult(True, _render(buf.getvalue(), glb.get("RESULT"), state, t0), "")


def _render(stdout: str, result, state: dict, t0: float, *, err: str | None = None) -> str:
    parts = [f"[program · {state['calls']} tool calls · {time.monotonic() - t0:.1f}s]"]
    if stdout.strip():
        parts.append(_truncate(stdout.rstrip(), _MAX_OUTPUT_CHARS // 2))
    if result is not None:
        try:
            parts.append("RESULT = " + _truncate(json.dumps(result, default=str), _MAX_OUTPUT_CHARS // 2))
        except Exception:
            parts.append("RESULT = " + _truncate(repr(result), _MAX_OUTPUT_CHARS // 2))
    if err:
        parts.append(_truncate(f"(program ended early: {err})", 500))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool descriptor
# ---------------------------------------------------------------------------

PROGRAM_TOOL = ToolDef(
    name="program",
    description=(
        "Run ONE Python program that composes many harness tool calls in a "
        "single step. Every registered tool is a function on the `tools` "
        "object: tools.read_file(path='…'), tools.grep(pattern='…'), "
        "tools.list_dir(path='…'), tools.run_command(command='…'), etc. — "
        "keyword arguments only, each returns the tool's output text. Chain "
        "them, loop, filter, aggregate; assign your final answer to RESULT "
        "(JSON-serializable) and/or print it. Safety gating applies to every "
        "inner call. Imports limited to pure-computation stdlib (json, re, "
        "math, itertools, collections, …). Use this instead of sequences of "
        "dependent tool calls — it is dramatically faster and cheaper."
    ),
    input_schema={
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Python program"}},
        "required": ["code"],
    },
    handler=lambda ctx, code: run_program(ctx, code),
    mutating=True,
)
