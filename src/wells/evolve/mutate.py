"""SEACS Phase 2 — the evolution engine: mutate, gate, promote/reject.

Everything in :mod:`wells.evolve.corpus` and :mod:`wells.evolve.runner` is
measurement infrastructure — it produces an honest, oracle-scored pass@1
for a harness configuration, nothing more. This module is the first thing
that *acts* on that number: it proposes a candidate replacement for the
harness's own operating principles (``AGENT.md``), gates it against a
baseline using the existing bench runner, and promotes or rejects it.

Why ``AGENT.md`` and not tools/skills for this first version: it has an
already-designed write seam. :mod:`wells.principles` resolves the active
constitution as ``$WELLS_PRINCIPLES`` env var > ``<workspace>/AGENT.md`` >
the bundled default — the env var is *already* the highest-precedence
step, so gating a candidate is just setting one environment variable per
bench subprocess (see ``extra_env`` on :func:`wells.evolve.runner.run_bench`)
with zero changes to how a task's own worktree is built. Skills already
have their own propose/accept/reject flow
(:mod:`wells.skill_authoring`); tool descriptions are hardcoded Python
literals with no data-driven override yet — both are future extensions of
this same propose/gate/promote shape, not rebuilt here.

Mutation candidates and their gate results persist to
``~/.wells/evolve/mutations/<mutation_id>.json`` (one file per mutation,
mirroring :mod:`wells.fleet`'s ``~/.wells/fleet/<fleet_id>.json`` manifest
pattern) plus a sibling ``AGENT.md`` holding the candidate text. Promotion
writes that text into ``<workspace>/AGENT.md`` — a plain tracked file, so
committing it is the caller's own git workflow (this module never commits
on the caller's behalf).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

MUTATIONS_DIR = Path.home() / ".wells" / "evolve" / "mutations"


def _ensure_dir() -> None:
    MUTATIONS_DIR.mkdir(parents=True, exist_ok=True)


def new_mutation_id() -> str:
    ts = time.strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


@dataclass
class MutationManifest:
    mutation_id: str
    workspace: str
    created_at: str
    rationale: str
    candidate_path: str  # absolute path to the candidate AGENT.md text
    baseline_source: str  # principles.source_label(workspace) at propose time
    split: str = ""
    task_filter: str = ""
    status: str = "pending"  # pending -> gated -> promoted | rejected
    baseline_bench: dict | None = None
    candidate_bench: dict | None = None
    replay_summary: dict | None = None
    recommendation: str = ""  # "promote" | "reject" | ""
    promoted_at: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "MutationManifest":
        return cls(
            mutation_id=d["mutation_id"],
            workspace=d["workspace"],
            created_at=d["created_at"],
            rationale=d.get("rationale", ""),
            candidate_path=d["candidate_path"],
            baseline_source=d.get("baseline_source", ""),
            split=d.get("split", ""),
            task_filter=d.get("task_filter", ""),
            status=d.get("status", "pending"),
            baseline_bench=d.get("baseline_bench"),
            candidate_bench=d.get("candidate_bench"),
            replay_summary=d.get("replay_summary"),
            recommendation=d.get("recommendation", ""),
            promoted_at=d.get("promoted_at", ""),
        )


def save_manifest(m: MutationManifest) -> Path:
    _ensure_dir()
    path = MUTATIONS_DIR / f"{m.mutation_id}.json"
    path.write_text(json.dumps(m.to_json(), indent=2, default=str), encoding="utf-8")
    return path


def load_manifest(mutation_id: str) -> MutationManifest | None:
    path = MUTATIONS_DIR / f"{mutation_id}.json"
    if not path.is_file():
        return None
    try:
        return MutationManifest.from_json(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def list_manifests() -> list[MutationManifest]:
    _ensure_dir()
    out = []
    for p in sorted(MUTATIONS_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            out.append(MutationManifest.from_json(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------


def propose_mutation(
    workspace: str,
    rationale: str,
    *,
    candidate_file: str = "",
    auto: bool = False,
    profile: str = "",
    timeout: float = 1800.0,
    heartbeat_path: str | Path | None = None,
) -> MutationManifest:
    """Create a candidate AGENT.md and record it as a pending mutation.

    Exactly one of ``candidate_file`` (a human/agent-authored replacement
    text, read as-is — zero LLM cost, deterministic) or ``auto=True`` (has
    the harness draft its own candidate via one real headless run — costs
    real tokens, opt-in only) must be given.

    ``heartbeat_path``, when ``auto=True``, is touched periodically during
    the draft (same mechanism as run_bench's mid-task ticks) — without
    this, an external watchdog polling the heartbeat sees no update for
    the whole draft call and, if that exceeds its staleness threshold,
    concludes the process is dead and kills it mid-draft. Since propose's
    own state isn't saved until *after* it returns, that kill abandons
    the in-flight draft entirely rather than resuming it — a real bug
    that wasted several real drafting calls in autoloop before this was
    wired in.
    """
    from wells import principles

    if candidate_file and auto:
        raise ValueError("propose_mutation: pass candidate_file OR auto=True, not both.")

    baseline_source = principles.source_label(workspace)

    if candidate_file:
        candidate_text = Path(candidate_file).read_text(encoding="utf-8")
    elif auto:
        candidate_text = _draft_candidate(
            workspace, profile=profile, timeout=timeout,
            history_context=_recent_mutation_history(),
            heartbeat_path=heartbeat_path,
        )
    else:
        raise ValueError("propose_mutation: needs candidate_file=PATH or auto=True.")

    mutation_id = new_mutation_id()
    _ensure_dir()
    mdir = MUTATIONS_DIR / mutation_id
    mdir.mkdir(parents=True, exist_ok=True)
    candidate_path = mdir / "AGENT.md"
    candidate_path.write_text(candidate_text, encoding="utf-8")

    manifest = MutationManifest(
        mutation_id=mutation_id,
        workspace=str(Path(workspace).resolve()),
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        rationale=rationale,
        candidate_path=str(candidate_path),
        baseline_source=baseline_source,
    )
    save_manifest(manifest)
    return manifest


def _recent_mutation_history(limit: int = 8) -> str:
    """Summarize past gated mutations (rationale + outcome) for the --auto
    prompt, so a fresh draft doesn't blindly re-propose an idea already
    tried and rejected (or re-propose something already promoted)."""
    lines: list[str] = []
    for m in list_manifests()[:limit]:
        if m.status not in ("gated", "promoted", "rejected"):
            continue
        base_lb = (m.baseline_bench or {}).get("pass_at_1_wilson_lb")
        cand_lb = (m.candidate_bench or {}).get("pass_at_1_wilson_lb")
        metrics = (
            f"baseline_lb={base_lb:.1%} candidate_lb={cand_lb:.1%}"
            if base_lb is not None and cand_lb is not None
            else "(not gated)"
        )
        outcome = m.recommendation or "unknown"
        lines.append(
            f"- [{outcome}, status={m.status}] {m.rationale[:120]!r} -- {metrics}"
        )
    return "\n".join(lines) or "(no prior mutations recorded)"


def _draft_candidate(
    workspace: str, *, profile: str, timeout: float, history_context: str = "",
    heartbeat_path: str | Path | None = None,
) -> str:
    """Have the harness rewrite AGENT.md itself, seeded with recent bench
    failures as context. One real headless run — the only place in this
    module that spends tokens.
    """
    import shutil
    import tempfile

    from wells import principles
    from wells.evolve.runner import _HeartbeatWriter, _run_harness, list_results
    from wells.evolve.schema import TaskSpec

    hb = _HeartbeatWriter(heartbeat_path, job="evolve-propose-draft") if heartbeat_path else None
    if hb:
        hb.write(status="running", phase="draft")

    failure_notes: list[str] = []
    for p in list_results(workspace)[:2]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in data.get("tasks", []):
            if row.get("resolved"):
                continue
            failure_notes.append(
                f"- task {row.get('task_id')}: status={row.get('harness_status')} "
                f"error={(row.get('error') or '')[:150]}"
            )
    failures_block = "\n".join(failure_notes[:10]) or "(no recorded bench failures found)"

    current_text = principles.principles_text(workspace)

    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td)
        (scratch / "AGENT.md").write_text(current_text, encoding="utf-8")
        goal = (
            "Improve your own operating principles. The file AGENT.md in this "
            "workspace is your behavioral constitution — read it, then rewrite "
            "it (in place, via write_file) to better address these recent "
            "bench-run failures:\n\n"
            f"{failures_block}\n\n"
            "Prior mutation attempts and their real, measured outcomes "
            "(promote = beat baseline; reject = tied or lost) — do not "
            "re-propose an idea already rejected here, and don't undo one "
            "already promoted:\n\n"
            f"{history_context or '(no prior mutations recorded)'}\n\n"
            "Keep the tone and structure of the existing rules; make targeted "
            "improvements, not a rewrite from scratch. Do not touch any other "
            "file."
        )
        harness_kwargs = {"profile": profile, "timeout": timeout}
        if hb:
            harness_kwargs["on_tick"] = lambda: hb.write(status="running", phase="draft")
        _run_harness(str(scratch), goal, **harness_kwargs)
        return (scratch / "AGENT.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def gate_mutation(
    mutation_id: str,
    workspace: str,
    *,
    split: str = "val",
    profile: str = "",
    seeds: int = 1,
    timeout: float = 1800.0,
    task_filter: str = "",
    bench_home: Path | None = None,
    resume: bool = False,
    heartbeat_path: str | Path | None = None,
    log=print,
) -> MutationManifest:
    """Run baseline vs. candidate bench passes over ``split`` and a replay
    pass over the trace corpus; record a promote/reject recommendation.

    Does not promote — see :func:`promote_mutation`.

    **Fault tolerance.** Both passes use stable bench ids derived from
    ``mutation_id`` (not a fresh random one each call), so re-invoking
    with ``resume=True`` after a death (killed session, crash) picks up
    exactly where either pass stopped — each :func:`~wells.evolve.runner.
    run_bench` call is itself checkpointed per-task (see its own
    docstring), so nothing earlier than the one task in flight when the
    process died is ever redone. ``heartbeat_path``, if given, is passed
    straight through to both passes.
    """
    from wells import principles, traces
    from wells.evolve import corpus
    from wells.evolve.runner import run_bench

    manifest = load_manifest(mutation_id)
    if manifest is None:
        raise RuntimeError(f"No such mutation: {mutation_id}")

    tasks = corpus.list_tasks(workspace, split)
    if not tasks:
        raise RuntimeError(
            f"No {split!r} tasks in corpus under {workspace} — cannot gate. "
            f"Run `wells bench mine --workspace <repo>` to grow the corpus, "
            f"or pass --split train (not a true holdout, use with caution)."
        )

    baseline_principles_path = principles.find_principles_file(workspace)
    baseline_env = {
        "WELLS_PRINCIPLES": (
            str(baseline_principles_path)
            if baseline_principles_path
            else str(principles.BUNDLED_PATH)
        )
    }
    candidate_env = {"WELLS_PRINCIPLES": manifest.candidate_path}

    log(f"[evolve {mutation_id}] baseline pass (split={split!r}) ...")
    baseline_run = run_bench(
        workspace, split, profile=profile, seeds=seeds, timeout=timeout,
        task_filter=task_filter, bench_home=bench_home, extra_env=baseline_env,
        bench_id=f"{mutation_id}-baseline", resume=resume, heartbeat_path=heartbeat_path,
        log=log,
    )
    log(f"[evolve {mutation_id}] candidate pass (split={split!r}) ...")
    candidate_run = run_bench(
        workspace, split, profile=profile, seeds=seeds, timeout=timeout,
        task_filter=task_filter, bench_home=bench_home, extra_env=candidate_env,
        bench_id=f"{mutation_id}-candidate", resume=resume, heartbeat_path=heartbeat_path,
        log=log,
    )

    # Replay is a free, no-LLM sanity check over the recorded trace corpus.
    # Its model is fully scripted (see traces.replay's docstring) so it
    # cannot detect a principles-*text* regression — only a harness *code*
    # regression. Included because handoff.md specifies it and it costs
    # nothing, not because it's a strong signal for a principles-only
    # mutation like this one.
    trace_paths = traces.list_traces(workspace)
    replay_results = [traces.replay(p) for p in trace_paths]
    matched = sum(1 for r in replay_results if r.get("match"))
    replay_summary = {
        "total": len(replay_results),
        "matched": matched,
        "match_rate": round(matched / len(replay_results), 4) if replay_results else 1.0,
    }

    baseline_lb = baseline_run.summary.get("pass_at_1_wilson_lb", 0.0)
    candidate_lb = candidate_run.summary.get("pass_at_1_wilson_lb", 0.0)
    recommendation = "promote" if candidate_lb >= baseline_lb else "reject"

    manifest.split = split
    manifest.task_filter = task_filter
    manifest.status = "gated"
    manifest.baseline_bench = baseline_run.summary
    manifest.candidate_bench = candidate_run.summary
    manifest.replay_summary = replay_summary
    manifest.recommendation = recommendation
    save_manifest(manifest)
    log(
        f"[evolve {mutation_id}] baseline pass@1 LB={baseline_lb:.1%} "
        f"candidate pass@1 LB={candidate_lb:.1%} -> {recommendation}"
    )
    return manifest


# ---------------------------------------------------------------------------
# Promote / reject
# ---------------------------------------------------------------------------


def promote_mutation(
    mutation_id: str, workspace: str, *, force: bool = False
) -> tuple[bool, str]:
    from wells import principles

    manifest = load_manifest(mutation_id)
    if manifest is None:
        return False, f"No such mutation: {mutation_id}"
    if manifest.status == "promoted":
        return False, f"Mutation {mutation_id} was already promoted."
    if manifest.status != "gated" and not force:
        return False, (
            f"Mutation {mutation_id} has not been gated yet (status={manifest.status!r}). "
            f"Run `wells evolve gate {mutation_id}` first, or pass --force."
        )
    if manifest.recommendation != "promote" and not force:
        return False, (
            f"Mutation {mutation_id}'s gate result recommends {manifest.recommendation!r}, "
            f"not promotion. Pass --force to override."
        )

    candidate_text = Path(manifest.candidate_path).read_text(encoding="utf-8")
    dest = Path(workspace) / "AGENT.md"
    dest.write_text(candidate_text, encoding="utf-8")
    principles.clear_cache()

    manifest.status = "promoted"
    manifest.promoted_at = time.strftime("%Y-%m-%d %H:%M:%S")
    save_manifest(manifest)
    return True, (
        f"Promoted {mutation_id} -> {dest}. This is a normal tracked file — "
        f"review and commit it as you would any other change."
    )


def reject_mutation(mutation_id: str, workspace: str) -> tuple[bool, str]:
    manifest = load_manifest(mutation_id)
    if manifest is None:
        return False, f"No such mutation: {mutation_id}"
    if manifest.status in ("promoted", "rejected"):
        return False, f"Mutation {mutation_id} was already {manifest.status}."
    manifest.status = "rejected"
    save_manifest(manifest)
    return True, f"Rejected {mutation_id}. Candidate kept on disk for reference."
