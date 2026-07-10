"""Background agents: start / check / collect concurrent sub-agent work.

:mod:`wells.subagents` already fans out 2–4 read-only research
subagents in parallel — but it is **blocking**: ``parallel_research`` waits for
all of them to finish before returning. The agent can't do anything else while
they run, and it can't decide *when* to check on them.

This module adds the **async background-agent** pattern from the Microsoft
Agent Framework "scaling the claw" article: the main agent gets three tools —

  * ``bg_start``  — launch a sub-agent on a background thread, get a handle id.
  * ``bg_status`` — check which background agents are running / done / failed.
  * ``bg_collect`` — collect a finished agent's report (once).

The fan-out becomes the *agent's* decision: it starts N tasks, keeps working
(reading files, making edits), and collects results when convenient. This is
the natural generalization of the roadmap item "async task tracking for MCP
``run_agent_task``".

Design:
  * Each background agent is a :class:`run_subagent` call running on a daemon
    thread in a process-wide :class:`BackgroundRegistry`. Threads are right
    here (LLM calls are I/O-bound), matching :mod:`subagents`.
  * The registry is keyed by short, stable ids so the model can reference them.
  * A result is collected at most once (after that it's gone) to keep memory
    bounded across a long run.
  * Recursion is blocked: a sub-agent cannot start its own background agents
    (``ctx.subagent`` is set by :func:`run_subagent`).
  * Cooperative cancellation: the registry honors :data:`control.CONTROL.cancel`
    so Escape stops pending background work too.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from wells.subagents import SubagentReport, SubagentSpec, run_subagent
from wells.tools import ToolContext, ToolDef, ToolResult


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class _Slot:
    """One background agent's tracking state."""

    id: str
    name: str
    task: str
    started_at: float
    # status: running | done | error | cancelled
    status: str = "running"
    report: SubagentReport | None = None
    collected: bool = False


class BackgroundRegistry:
    """Process-wide registry of background agent slots.

    Thread-safe; one instance is shared across an executor run. The lifecycle
    is: ``start`` → poll ``status`` → ``collect`` (once).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._counter = 0
        self._on_finish: Callable[[str], None] | None = None

    def reset(self) -> None:
        """Clear all slots (call at the start of a new run)."""
        with self._lock:
            self._slots.clear()
            self._counter = 0

    def start(
        self,
        spec: SubagentSpec,
        ctx: ToolContext,
        *,
        profile: str | None = None,
    ) -> str:
        """Launch ``spec`` on a daemon thread; return its id."""
        with self._lock:
            self._counter += 1
            bgid = f"bg-{self._counter}"
            slot = _Slot(
                id=bgid, name=spec.name, task=spec.task, started_at=time.time()
            )
            self._slots[bgid] = slot

        def _run() -> None:
            from wells.control import CONTROL

            try:
                if CONTROL.cancelled():
                    with self._lock:
                        slot.status = "cancelled"
                    return
                report = run_subagent(spec, ctx, profile=profile, quiet=True)
                with self._lock:
                    if slot.status == "cancelled":
                        return
                    slot.report = report
                    slot.status = "ok" if report.ok else "error"
            except Exception as e:
                with self._lock:
                    if slot.status == "cancelled":
                        return
                    slot.status = "error"
                    slot.report = SubagentReport(
                        name=spec.name,
                        ok=False,
                        summary="",
                        steps_taken=0,
                        error=f"{type(e).__name__}: {e}",
                    )
            finally:
                if self._on_finish is not None:
                    try:
                        self._on_finish(bgid)
                    except Exception:
                        pass

        t = threading.Thread(
            target=_run, name=f"wells-bg-{bgid}", daemon=True
        )
        t.start()
        return bgid

    def status(self) -> list[dict]:
        """Snapshot of all slots: [{id, name, status, elapsed_s, collected}]."""
        now = time.time()
        with self._lock:
            out = []
            for s in self._slots.values():
                out.append(
                    {
                        "id": s.id,
                        "name": s.name,
                        "status": s.status,
                        "elapsed_s": round(now - s.started_at, 1),
                        "collected": s.collected,
                    }
                )
            return out

    def collect(self, bgid: str) -> SubagentReport | None:
        """Return a finished agent's report (once). None if not done / unknown."""
        with self._lock:
            slot = self._slots.get(bgid)
            if slot is None:
                return None
            if slot.status == "running":
                return None
            if slot.collected:
                return None
            slot.collected = True
            return slot.report

    def cancel_all(self) -> int:
        """Mark all running slots cancelled. Returns how many were running."""
        with self._lock:
            n = 0
            for s in self._slots.values():
                if s.status == "running":
                    s.status = "cancelled"
                    n += 1
            return n

    def pending(self) -> int:
        with self._lock:
            return sum(1 for s in self._slots.values() if s.status == "running")

    def has(self, bgid: str) -> bool:
        with self._lock:
            return bgid in self._slots


# Process-wide registry. The executor resets it at run start.
REGISTRY = BackgroundRegistry()


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _bg_start(
    ctx: ToolContext,
    task: str,
    role: str = "research",
    max_steps: int | None = None,
) -> ToolResult:
    """Start a background sub-agent; returns its handle id immediately."""
    if ctx.subagent:
        return ToolResult(False, "", "subagents cannot start background agents")
    if not task or not task.strip():
        return ToolResult(False, "", "task is required")
    role = (role or "research").strip().lower()
    if role not in ("research", "fix"):
        return ToolResult(
            False, "", f"role must be 'research' or 'fix' (got {role!r})"
        )

    from wells import subagents

    name = f"{role}-{int(time.time() * 1000) % 100000}"
    if role == "fix":
        spec = subagents.fix_subagent(name, task, max_steps=max_steps)
    else:
        spec = subagents.research_subagent(name, task, max_steps=max_steps)
    bgid = REGISTRY.start(spec, ctx)
    desc = "read-only research" if role == "research" else "scoped edit"
    return ToolResult(
        True,
        f"Started background {desc} agent {bgid} ({name}). "
        f"It runs concurrently — keep working, then bg_status / bg_collect.",
    )


def _bg_status(ctx: ToolContext) -> ToolResult:
    """Report the status of all background agents (running/done/error)."""
    if ctx.subagent:
        return ToolResult(False, "", "subagents cannot query background agents")
    rows = REGISTRY.status()
    if not rows:
        return ToolResult(True, "(no background agents started)")
    lines = []
    for r in rows:
        lines.append(
            f"- {r['id']} [{r['name']}] {r['status']} "
            f"({r['elapsed_s']}s{' , collected' if r['collected'] else ''})"
        )
    return ToolResult(True, "\n".join(lines))


def _bg_collect(ctx: ToolContext, id: str = "") -> ToolResult:
    """Collect a finished background agent's report (once)."""
    if ctx.subagent:
        return ToolResult(False, "", "subagents cannot collect background agents")
    bgid = (id or "").strip()
    if not bgid:
        pending = [r for r in REGISTRY.status() if r["status"] != "running" and not r["collected"]]
        if not pending:
            return ToolResult(
                True,
                "No finished, uncollected background agents. "
                "Pass an explicit id, or bg_status to see all.",
            )
        if len(pending) > 1:
            avail = ", ".join(r["id"] for r in pending)
            return ToolResult(
                True,
                f"Multiple agents finished: {avail}. Call bg_collect with one id.",
            )
        bgid = pending[0]["id"]

    if not REGISTRY.has(bgid):
        return ToolResult(False, "", f"Unknown background agent id: {bgid}")
    report = REGISTRY.collect(bgid)
    if report is None:
        slot = next((r for r in REGISTRY.status() if r["id"] == bgid), None)
        if slot and slot["status"] == "running":
            return ToolResult(
                True, f"{bgid} is still running ({slot['elapsed_s']}s). Try again later."
            )
        return ToolResult(True, f"{bgid} has already been collected (or was cancelled).")
    return ToolResult(report.ok, report.as_context_block(), "" if report.ok else report.error)


# ---------------------------------------------------------------------------
# Tool descriptors
# ---------------------------------------------------------------------------


BG_START_TOOL = ToolDef(
    name="bg_start",
    description=(
        "Start a background sub-agent that runs CONCURRENTLY and return its handle "
        "id immediately (does not block). Use for fan-out: start N agents (e.g. "
        "research 3 tickers, or investigate 3 modules), keep working, then "
        "bg_status/bg_collect when convenient. role=research is read-only; "
        "role=fix may edit. You can have several running at once."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Focused task for the sub-agent"},
            "role": {
                "type": "string",
                "enum": ["research", "fix"],
                "default": "research",
            },
            "max_steps": {"type": "integer", "default": 0},
        },
        "required": ["task"],
    },
    handler=_bg_start,
    mutating=False,
)

BG_STATUS_TOOL = ToolDef(
    name="bg_status",
    description=(
        "List all background agents and their status (running/done/error) with "
        "elapsed seconds. Poll this to see which bg_start tasks are finished and "
        "ready to collect."
    ),
    input_schema={"type": "object", "properties": {}},
    handler=_bg_status,
    mutating=False,
)

BG_COLLECT_TOOL = ToolDef(
    name="bg_collect",
    description=(
        "Collect a finished background agent's report (exactly once). Pass the id "
        "from bg_start, or omit to collect the single finished, uncollected agent. "
        "Returns 'still running' if it isn't done yet — keep working and retry."
    ),
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string", "description": "Agent id from bg_start"}},
    },
    handler=_bg_collect,
    mutating=False,
)


def enabled() -> bool:
    """Whether the background-agent tools are registered (``WELLS_BG_AGENTS`` != 0)."""
    import os

    return os.environ.get("WELLS_BG_AGENTS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )
