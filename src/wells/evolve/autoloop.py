"""SEACS autonomous loop: propose -> gate -> promote/reject -> repeat,
unattended, bounded by a cycle count and a wall-clock deadline.

This is the "set it and forget it" mode explicitly requested — every other
part of the evolve subsystem (propose/gate/promote/reject) is a single
supervised action. This module is the one place that chains them together
without a human approving each step, so it carries the sharpest edges in
the whole subsystem:

  * It writes to the harness's own AGENT.md and commits+pushes that change
    to origin *unattended*, repeatedly, over days — done only because the
    user explicitly asked for exactly that (auto-promote, auto-push), not
    the module's own default judgment. A future caller wanting a more
    conservative mode should pass ``push=False`` and/or leave promotion to
    a human by not calling this module at all (propose/gate/promote are
    still available individually).
  * It is bounded on two independent axes (max_cycles AND a wall-clock
    deadline) so "a few days" can never silently become "forever" — see
    the ``run_autonomous_loop`` docstring.
  * Every cycle's outcome is written to a durable, human-readable report
    (``autoloop_report.md``) specifically so a person coming back after
    days away can read a summary instead of reconstructing what happened
    from bench result JSON files.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_DIR = Path.home() / ".wells" / "evolve"
STATE_PATH = STATE_DIR / "autoloop_state.json"
REPORT_PATH = STATE_DIR / "autoloop_report.md"


@dataclass
class CycleRecord:
    cycle: int
    mutation_id: str
    rationale: str
    started_at: str
    finished_at: str = ""
    recommendation: str = ""
    baseline_lb: float | None = None
    candidate_lb: float | None = None
    promoted: bool = False
    pushed: bool = False
    error: str = ""


@dataclass
class LoopState:
    started_at: float
    deadline: float
    max_cycles: int
    cycle: int = 0
    current_mutation_id: str = ""
    stopped: bool = False
    stop_reason: str = ""
    history: list[CycleRecord] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "LoopState":
        history = [CycleRecord(**h) for h in d.get("history", [])]
        return cls(
            started_at=d["started_at"], deadline=d["deadline"],
            max_cycles=d["max_cycles"], cycle=d.get("cycle", 0),
            current_mutation_id=d.get("current_mutation_id", ""),
            stopped=d.get("stopped", False), stop_reason=d.get("stop_reason", ""),
            history=history,
        )


def _load_state() -> LoopState | None:
    if not STATE_PATH.is_file():
        return None
    try:
        return LoopState.from_json(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except Exception:
        return None


def _save_state(state: LoopState) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_json(), indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _append_report(line: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with REPORT_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _git_commit_and_maybe_push(
    workspace: str, message: str, *, push: bool, log
) -> bool:
    """Commit AGENT.md (already written by promote_mutation) and, if
    ``push`` is set, push to the current branch's upstream. Never raises —
    a commit/push failure is logged and the loop continues; losing the
    ability to push is not a reason to stop researching, and the change
    is still safely committed locally either way."""
    from wells._gitutils import git

    ok, out = git(workspace, "add", "AGENT.md")
    if not ok:
        log(f"[autoloop] git add AGENT.md failed: {out[:200]}")
        return False
    ok, out = git(workspace, "commit", "-m", message)
    if not ok:
        log(f"[autoloop] git commit failed (maybe nothing changed?): {out[:200]}")
        return False
    log(f"[autoloop] committed: {message}")
    if not push:
        return True
    ok, out = git(workspace, "push", "origin", "HEAD", timeout=120.0)
    if not ok:
        log(f"[autoloop] git push FAILED (change is still committed locally): {out[:300]}")
        return False
    log("[autoloop] pushed to origin.")
    return True


def run_autonomous_loop(
    workspace: str,
    *,
    max_cycles: int = 10,
    max_days: float = 3.0,
    split: str = "val",
    profile: str = "",
    seeds: int = 1,
    timeout: float = 1200.0,
    push: bool = True,
    heartbeat_path: str | Path | None = None,
    workers: int = 1,
    log=print,
) -> LoopState:
    """Run propose(--auto) -> gate -> promote/reject cycles until stopped.

    Bounded on two independent axes, whichever comes first:
      - ``max_cycles`` complete propose+gate cycles
      - ``max_days`` of wall-clock time since the loop first started
        (survives resume: the deadline is fixed at first start, not reset
        on every restart, so a crash-and-resume sequence can't extend the
        total unattended window past what was originally authorized)

    **Fault tolerance**, same design as the rest of this subsystem: state
    (which cycle, which mutation is in flight) is checkpointed to disk
    after every meaningful step. Re-calling this function — e.g. after
    the whole process was killed — loads that state and continues: if a
    mutation was mid-gate, gate_mutation's own per-task resume picks it
    back up (no wasted work beyond the one task in flight); if a cycle
    had fully finished, the next call starts the next cycle fresh.

    Every cycle's outcome is appended to a human-readable report at
    ``~/.wells/evolve/autoloop_report.md`` and the promote/reject decision
    for --auto's next draft is fed back via mutate._recent_mutation_history
    so the loop doesn't repeat an idea already tried.
    """
    from wells.evolve import mutate

    state = _load_state()
    if state is None:
        now = time.time()
        state = LoopState(
            started_at=now, deadline=now + max_days * 86400, max_cycles=max_cycles,
        )
        _save_state(state)
        _append_report(
            f"\n# Autonomous loop started {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"max_cycles={max_cycles} max_days={max_days} split={split!r} push={push}\n"
        )
        log(f"[autoloop] starting fresh: max_cycles={max_cycles} max_days={max_days}")
    else:
        log(
            f"[autoloop] resuming: cycle={state.cycle}/{state.max_cycles} "
            f"current_mutation_id={state.current_mutation_id!r}"
        )

    while True:
        if state.cycle >= state.max_cycles:
            state.stopped, state.stop_reason = True, "max_cycles reached"
            break
        if time.time() >= state.deadline:
            state.stopped, state.stop_reason = True, "max_days deadline reached"
            break

        if not state.current_mutation_id:
            # Fresh cycle: draft a new candidate, informed by everything
            # tried so far (mutate._recent_mutation_history reads the
            # manifest store directly, so this sees prior cycles from this
            # loop AND any manually-proposed mutations).
            state.cycle += 1
            log(f"[autoloop] === cycle {state.cycle}/{state.max_cycles}: drafting candidate ===")
            try:
                manifest = mutate.propose_mutation(
                    workspace,
                    f"autoloop cycle {state.cycle}: auto-drafted, informed by prior attempts",
                    auto=True, profile=profile, timeout=timeout,
                    heartbeat_path=heartbeat_path,
                )
            except Exception as e:
                log(f"[autoloop] propose failed: {e}")
                state.stopped, state.stop_reason = True, f"propose failed: {e}"
                break
            state.current_mutation_id = manifest.mutation_id
            record = CycleRecord(
                cycle=state.cycle, mutation_id=manifest.mutation_id,
                rationale=manifest.rationale,
                started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            state.history.append(record)
            _save_state(state)
            _append_report(f"\n## Cycle {state.cycle} — {manifest.mutation_id}\nProposed {record.started_at}\n")
        else:
            record = state.history[-1]
            log(f"[autoloop] === cycle {state.cycle}/{state.max_cycles}: resuming gate for {state.current_mutation_id} ===")

        log(f"[autoloop] gating {state.current_mutation_id} (split={split!r}) ...")
        try:
            manifest = mutate.gate_mutation(
                state.current_mutation_id, workspace, split=split, profile=profile,
                seeds=seeds, timeout=timeout, resume=True, heartbeat_path=heartbeat_path,
                workers=workers, log=log,
            )
        except Exception as e:
            log(f"[autoloop] gate failed: {e}")
            record.error = str(e)[:500]
            record.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            state.current_mutation_id = ""
            _save_state(state)
            _append_report(f"ERROR: {record.error}\n")
            continue  # move on to the next cycle rather than getting stuck forever

        record.recommendation = manifest.recommendation
        record.baseline_lb = (manifest.baseline_bench or {}).get("pass_at_1_wilson_lb")
        record.candidate_lb = (manifest.candidate_bench or {}).get("pass_at_1_wilson_lb")
        record.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")

        if manifest.recommendation == "promote":
            ok, msg = mutate.promote_mutation(state.current_mutation_id, workspace)
            log(f"[autoloop] promote: {msg}")
            record.promoted = ok
            if ok:
                pushed = _git_commit_and_maybe_push(
                    workspace,
                    f"evolve: promote {state.current_mutation_id} (autoloop cycle {state.cycle})\n\n"
                    f"{record.rationale}\n\n"
                    f"baseline LB {record.baseline_lb:.1%} -> candidate LB {record.candidate_lb:.1%}",
                    push=push, log=log,
                )
                record.pushed = pushed
        else:
            ok, msg = mutate.reject_mutation(state.current_mutation_id, workspace)
            log(f"[autoloop] reject: {msg}")

        _append_report(
            f"Gated {record.finished_at} — recommendation={record.recommendation} "
            f"baseline_lb={record.baseline_lb} candidate_lb={record.candidate_lb} "
            f"promoted={record.promoted} pushed={record.pushed}\n"
        )
        state.current_mutation_id = ""
        _save_state(state)

    _save_state(state)
    _append_report(
        f"\n# Autonomous loop stopped {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"reason: {state.stop_reason}\n"
        f"cycles completed: {state.cycle}, promoted: "
        f"{sum(1 for h in state.history if h.promoted)}\n"
    )
    log(f"[autoloop] stopped: {state.stop_reason}")
    return state
