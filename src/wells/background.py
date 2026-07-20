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

Three roles:

  * ``research`` — read-only investigator. Cannot mutate the workspace.
  * ``fix`` — scoped editor. Writes the parent workspace directly; suitable
    when only one fix is in flight or edits target disjoint files.
  * ``worktree`` — scoped editor with **isolation**: the sub-agent runs in its
    own :mod:`wells.worktree` git worktree, and on ``bg_collect`` its commit
    is cherry-picked into the parent. On conflict the cherry-pick is aborted
    and the diff is returned to the parent agent (no surprise merges). Use
    this when multiple write-fan-outs target overlapping areas, or whenever
    isolation is cheaper than reasoning about interleaving. Requires git.

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
  * For ``role="worktree"``: the worktree handle is registered on the slot
    *before* the sub-agent thread starts, so ``reset`` can always reap a
    half-spawned or cancelled worktree even if the parent agent never collects.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from wells.subagents import SubagentReport, SubagentSpec, run_subagent
from wells.tools import ToolContext, ToolDef, ToolResult

from wells.worktree import WorktreeHandle

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
    # role="worktree" only: the live worktree + parent workspace to merge into.
    # Both are None for research/fix roles. Registered here BEFORE the sub-agent
    # thread starts so reset() can always reap a half-spawned or cancelled
    # worktree — even if the parent agent never collects.
    worktree: WorktreeHandle | None = None
    parent_workspace: str = ""


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

    def reset(self) -> int:
        """Cancel any still-running background agents, then clear all slots.

        Called at the start of every run_executor() call. Before this
        cancelled running slots at all — clearing the dict alone left their
        daemon threads running unsupervised: still calling tools (including
        writes, for a role="fix" agent) against the SAME workspace a brand
        new task had just started editing, with no way for the new run's
        bg_status/bg_collect to see or know about them, since their slots
        were gone.

        Marking a slot cancelled does not force-kill its thread — Python
        can't do that, and the same limitation already applies to LLM-call
        cancellation elsewhere in this module (see executor._invoke_cancelable).
        What it DOES guarantee: the thread's own _run() checks
        ``slot.status == "cancelled"`` before ever committing a report, so
        a stale result can never resurface via bg_status/bg_collect after
        this call returns. The thread still runs to its own next
        CONTROL.cancelled() checkpoint or natural completion — see
        run_executor's cooperative-cancellation checks — this closes the
        "invisible, unrecoverable" half of the problem, not the "still
        physically running" half, which cooperative threading cannot close.

        For role="worktree" slots, also reaps the worktree + branch so a
        cancelled run doesn't leak disk. Best-effort: a worktree whose
        parent_workspace is gone (e.g. tmp_path) is silently skipped.

        Returns how many were cancelled, so the caller can warn when a
        background agent was actually abandoned mid-flight.
        """
        with self._lock:
            n = self._cancel_all_locked()
            self._reap_worktrees_locked()
            self._slots.clear()
            self._counter = 0
            return n

    def start(
        self,
        spec: SubagentSpec,
        ctx: ToolContext,
        *,
        profile: str | None = None,
        worktree: WorktreeHandle | None = None,
        parent_workspace: str = "",
    ) -> str:
        """Launch ``spec`` on a daemon thread; return its id.

        If ``worktree`` is given, it is registered on the slot *before* the
        thread starts so :meth:`reset` can always reap it. ``ctx.workspace``
        must already point at the worktree path in that case (the caller
        arranges that — typically via ``replace(ctx, workspace=handle.worktree_path)``).
        ``parent_workspace`` is recorded separately so collect() knows where
        to cherry-pick back into.
        """
        with self._lock:
            self._counter += 1
            bgid = f"bg-{self._counter}"
            slot = _Slot(
                id=bgid,
                name=spec.name,
                task=spec.task,
                started_at=time.time(),
                worktree=worktree,
                parent_workspace=parent_workspace,
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

        t = threading.Thread(target=_run, name=f"wells-bg-{bgid}", daemon=True)
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
        """Return a finished agent's report (once). None if not done / unknown.

        For role="worktree" slots, also: commit any pending worktree edits,
        cherry-pick the resulting commit into ``parent_workspace``, then reap
        the worktree + branch. On cherry-pick conflict, aborts, attaches the
        diff to the report's summary, and reaps anyway — the parent agent
        re-applies by hand.
        """
        with self._lock:
            slot = self._slots.get(bgid)
            if slot is None:
                return None
            if slot.status == "running":
                return None
            if slot.collected:
                return None
            slot.collected = True
            report = slot.report
            worktree = slot.worktree
            parent = slot.parent_workspace

        # Cherry-pick + reap happens outside the lock (subprocess I/O).
        if worktree is not None and parent:
            report = _finalize_worktree_slot(parent, worktree, report)

        return report

    def _cancel_all_locked(self) -> int:
        """Mark all running slots cancelled. Caller must already hold self._lock."""
        n = 0
        for s in self._slots.values():
            if s.status == "running":
                s.status = "cancelled"
                n += 1
        return n

    def _reap_worktrees_locked(self) -> None:
        """Best-effort reap of every slot's worktree. Caller holds self._lock.

        Used by :meth:`reset` so a cancelled run doesn't leak worktrees on
        disk. We snapshot what to reap under the lock, then release it before
        doing subprocess I/O (git won't deadlock, but holding a lock across
        blocking I/O is still bad form).
        """
        to_reap: list[tuple[str, WorktreeHandle]] = [
            (s.parent_workspace, s.worktree)
            for s in self._slots.values()
            if s.worktree is not None and s.parent_workspace
        ]
        if not to_reap:
            return
        # Release the lock around the git calls by deferring them. We're
        # already inside reset()'s `with self._lock`, so we can't release
        # here; instead we keep the calls short (each is one git subprocess)
        # and accept the brief hold. git operations are bounded by timeout.
        from wells.worktree import remove_worktree

        for parent, handle in to_reap:
            try:
                remove_worktree(parent, handle)
            except Exception:
                pass

    def cancel_all(self) -> int:
        """Mark all running slots cancelled. Returns how many were running."""
        with self._lock:
            return self._cancel_all_locked()

    def pending(self) -> int:
        with self._lock:
            return sum(1 for s in self._slots.values() if s.status == "running")

    def has(self, bgid: str) -> bool:
        with self._lock:
            return bgid in self._slots


# Process-wide registry. The executor resets it at run start.
REGISTRY = BackgroundRegistry()


# ---------------------------------------------------------------------------
# Worktree slot finalization (cherry-pick + reap)
# ---------------------------------------------------------------------------


def _finalize_worktree_slot(
    parent_workspace: str,
    handle: WorktreeHandle,
    report: SubagentReport | None,
) -> SubagentReport:
    """Commit the worktree's pending edits, cherry-pick into the parent, reap.

    On conflict: aborts the cherry-pick and attaches the diff to the report's
    summary so the parent agent can re-apply manually. The worktree is reaped
    either way (the diff is in the parent's context now). Never raises —
    git errors land in the report's summary.
    """
    from wells.worktree import (
        cherry_pick_into_parent,
        commit_pending,
        remove_worktree,
    )

    footer: list[str] = []
    tip = ""
    try:
        c = commit_pending(handle, message=f"wells bg worktree: {handle.slot_id}")
        if not c.ok:
            footer.append(f"[worktree commit failed: {c.message}]")
        else:
            tip = c.tip_sha
            if not tip:
                footer.append("[worktree: no edits to merge]")
    except Exception as e:
        footer.append(f"[worktree commit error: {type(e).__name__}: {e}]")

    merged = False
    conflict_diff = ""
    if tip:
        try:
            m = cherry_pick_into_parent(parent_workspace, handle, tip_sha=tip)
            if m.ok and not m.skipped:
                merged = True
                footer.append(f"[merged worktree branch {handle.branch} into parent]")
            elif m.skipped:
                pass  # already noted "no edits" above
            elif m.conflict:
                conflict_diff = m.diff
                footer.append(
                    f"[CONFLICT: cherry-pick of {handle.branch} aborted; "
                    f"parent diverged. Re-apply the change manually. "
                    f"Diff against parent HEAD:]"
                )
            else:
                footer.append(f"[worktree merge failed: {m.message}]")
        except Exception as e:
            footer.append(f"[worktree merge error: {type(e).__name__}: {e}]")

    # Always reap the worktree — the commits (if any) are either merged or
    # captured in the diff text. Leaving it would leak disk.
    try:
        remove_worktree(parent_workspace, handle)
    except Exception as e:
        footer.append(f"[worktree cleanup error: {type(e).__name__}: {e}]")

    # Augment the report with the merge outcome. If the subagent itself
    # errored (report is None), synthesize one so the parent still sees the
    # worktree outcome.
    if report is None:
        report = SubagentReport(
            name=handle.slot_id,
            ok=False,
            summary="",
            steps_taken=0,
            error="subagent did not produce a report",
        )
    parts = [report.summary.rstrip(), *footer]
    if conflict_diff:
        parts.append("```diff\n" + conflict_diff.rstrip() + "\n```")
    report.summary = "\n".join(p for p in parts if p)
    if not merged and conflict_diff:
        # Make sure the parent notices the merge didn't land.
        report.ok = False
    return report


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
    if role not in ("research", "fix", "worktree"):
        return ToolResult(
            False,
            "",
            f"role must be 'research', 'fix', or 'worktree' (got {role!r})",
        )

    from dataclasses import replace as _replace

    from wells import subagents
    from wells import worktree as _wt

    name = f"{role}-{int(time.time() * 1000) % 100000}"

    # role="worktree": spawn the subagent in its own git worktree, isolated
    # from the parent workspace. On bg_collect the worktree's commit is
    # cherry-picked back into the parent (or, on conflict, the diff is
    # returned for manual re-apply). Requires git; degrades gracefully.
    worktree_handle: _wt.WorktreeHandle | None = None
    parent_workspace = ""
    run_ctx = ctx
    if role == "worktree":
        if not worktrees_enabled():
            return ToolResult(
                False,
                "",
                "worktree branch agents are disabled (WELLS_BG_WORKTREES=0)",
            )
        # Reserve a worktree-local id (decoupled from the registry's bg-N id;
        # the registry id is what the model references; the worktree id only
        # shows up in the on-disk path and branch name).
        slot_id = f"wt-{int(time.time() * 1_000_000) % 1_000_000}"
        try:
            worktree_handle = _wt.create_worktree(
                ctx.workspace,
                slot_id=slot_id,
                task=task,
            )
        except RuntimeError as e:
            return ToolResult(
                False,
                "",
                f"could not create worktree for role=worktree: {e}. "
                f"Use role=fix for an inline edit instead.",
            )
        parent_workspace = ctx.workspace
        run_ctx = _replace(ctx, workspace=worktree_handle.worktree_path)
        spec = subagents.fix_subagent(name, task, max_steps=max_steps)
        try:
            bgid = REGISTRY.start(
                spec,
                run_ctx,
                worktree=worktree_handle,
                parent_workspace=parent_workspace,
            )
        except Exception:
            # Roll back the worktree we just created so we don't leak.
            try:
                _wt.remove_worktree(parent_workspace, worktree_handle)
            except Exception:
                pass
            raise
        return ToolResult(
            True,
            f"Started background worktree-isolated agent {bgid} ({name}) in "
            f"branch {worktree_handle.branch}. It runs concurrently and writes "
            f"to its own checkout — keep working, then bg_collect to merge "
            f"(or receive the diff on conflict).",
        )

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
        pending = [
            r
            for r in REGISTRY.status()
            if r["status"] != "running" and not r["collected"]
        ]
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
                True,
                f"{bgid} is still running ({slot['elapsed_s']}s). Try again later.",
            )
        return ToolResult(
            True, f"{bgid} has already been collected (or was cancelled)."
        )
    return ToolResult(
        report.ok, report.as_context_block(), "" if report.ok else report.error
    )


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
        "role=fix may edit the parent workspace directly (use when edits target "
        "disjoint files or only one agent writes at a time); role=worktree runs "
        "the agent in its own isolated git worktree and merges its commit back "
        "into the parent on collect (use when multiple write-fan-outs target "
        "overlapping areas — requires git). You can have several running at once."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Focused task for the sub-agent"},
            "role": {
                "type": "string",
                "enum": ["research", "fix", "worktree"],
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
        "properties": {
            "id": {"type": "string", "description": "Agent id from bg_start"}
        },
    },
    handler=_bg_collect,
    mutating=False,
)


def enabled() -> bool:
    """Whether the background-agent tools are registered (``WELLS_BG_AGENTS`` != 0)."""
    import os

    return os.environ.get("WELLS_BG_AGENTS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def worktrees_enabled() -> bool:
    """Whether ``bg_start role=worktree`` may spawn isolated worktrees.

    Default on (mirrors :func:`enabled`). Set ``WELLS_BG_WORKTREES=0`` to
    refuse the role without disabling the rest of the background-agent tools.
    """
    import os

    return os.environ.get("WELLS_BG_WORKTREES", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
