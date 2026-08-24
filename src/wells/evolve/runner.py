"""Bench runner: execute a corpus split and score it against the oracle.

For every task in the selected split (optionally N seeds each), the runner:

  1. creates a throwaway ``git worktree`` at ``base_commit`` (outside the
     repo, under ``~/.wells/bench/`` — same isolation reasoning as fleet),
  2. runs a **full headless Wells harness** in it as a genuine child process
     (``--workspace <worktree> --output-format json -p "<problem>"``) — the
     same subprocess isolation fleet uses, so each task gets its own model
     profile, token ledger, and environment,
  3. scores the result **deterministically**: the harness's own claim of
     COMPLETE/INCOMPLETE is ignored; the oracle's ``fail_to_pass`` (and
     ``pass_to_pass``, when present) tests are executed in the worktree and
     their exit code is the verdict. A run that "finished" with a red suite
     is not resolved; a run that timed out mid-edit with a green suite is.
  4. records per-task metrics (resolved, tokens, cost, duration, harness
     status) into ``<ws>/.wells/bench/results/<bench_id>.json``.

Results carry enough context to be comparable across runs — bench id,
model profile, per-task rows, and aggregate metrics including a Wilson
95% lower bound on pass@1 so two harness versions are compared on the
*conservative* estimate, not the noisy point estimate (SPEC-009 §4.2's
statistical gating, phase 1 of it).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from wells._gitutils import git, is_git_repo
from wells.evolve.corpus import list_tasks, resolve_command_argv
from wells.evolve.schema import TaskSpec

RESULTS_REL = Path(".wells") / "bench" / "results"

_RUN_TIMEOUT = 1800.0  # 30min wall-clock per harness run
_ORACLE_TIMEOUT = 600.0


def results_dir(workspace: str) -> Path:
    return Path(workspace) / RESULTS_REL


def new_bench_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------


def wilson_lower_bound(k: int, n: int, *, z: float = 1.96) -> float:
    """95% Wilson score interval lower bound for k successes in n trials.

    Comparing Wilson LBs (instead of raw pass rates) is what lets the
    evolution gate distinguish "actually better" from "lucky run" — with
    small validation splits the two are easy to confuse.
    """
    if n <= 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


# ---------------------------------------------------------------------------
# Per-task execution
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    task_id: str
    seed: int
    resolved: bool
    harness_status: str = ""  # complete | incomplete | error — informational only
    tokens_total: int = 0
    cost_usd: float | None = None
    duration_seconds: float = 0.0
    error: str = ""
    fail_to_pass_results: dict[str, bool] = field(default_factory=dict)


def _kill_tree(pid: int) -> None:
    """Kill a process and all its descendants.

    ``Popen.kill``/``taskkill`` without ``/T`` only terminates the direct
    child; any grandchild holding the stdout pipe keeps it open, and the
    parent's drain then blocks forever (a real bench hang observed on
    Windows). Windows has no process groups for this, so ``taskkill /T``
    is the tool; POSIX gets a process-group kill (requires the child to be
    a group leader — see ``start_new_session`` in ``_run_harness``).
    """
    import signal
    import subprocess as _sp

    if sys.platform == "win32":
        _sp.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _run_harness(
    worktree: str,
    problem: str,
    *,
    profile: str = "",
    timeout: float = _RUN_TIMEOUT,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """One headless Wells run in a child process; returns the JSON payload.

    Child-process isolation is not an implementation detail: the model
    profile and token ledger are process-wide globals, so concurrent tasks
    in one process would corrupt each other (see fleet._run_member, which
    learned this the hard way).

    Uses Popen (not subprocess.run) with a hard tree-kill and a *bounded*
    drain: subprocess.run's own timeout path re-drains with no timeout
    after killing the child, which hangs forever if an orphaned grandchild
    still holds the pipe.
    """
    import os

    env = dict(os.environ)
    env["UV_LINK_MODE"] = "copy"
    # Force UTF-8 on the child's OWN stdio: a piped child python defaults to
    # the console codepage (cp1252) on Windows, and the harness's Rich
    # console output (em-dashes, check marks) then crashes the run mid-flight
    # — exit 1, no JSON payload, tokens lost. PYTHONUTF8 flips the child's
    # whole IO layer; the explicit encoding/errors below cover our side of
    # the pipe regardless of platform.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if profile:
        env["MODEL_PROFILE"] = profile
    if extra_env:
        env.update(extra_env)
    argv = [
        "--workspace",
        worktree,
        "--output-format",
        "json",
        "-p",
        problem,
    ]
    proc = subprocess.Popen(
        [sys.executable, "-c", "from wells.main import main; main()", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        encoding="utf-8",
        errors="replace",
        # POSIX: make the child its own process-group leader so a timeout
        # can killpg() the whole tree (grandchildren holding the stdout
        # pipe are what made the Windows hang). Harmless no-op semantics
        # on Windows, where _kill_tree uses taskkill /T instead.
        start_new_session=(sys.platform != "win32"),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=30)  # bounded drain
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return {
            "status": "timeout",
            "error": f"harness run exceeded {timeout}s and was tree-killed",
        }

    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {
        "status": "error",
        "error": f"no JSON on stdout (exit {proc.returncode}); stderr tail: {(err or '')[-400:]}",
    }


def _score_oracle(worktree: str, task: TaskSpec) -> tuple[bool, dict[str, bool], str]:
    """Run the oracle tests; the exit code is the verdict, nothing else.

    ``fail_to_pass`` must all pass. ``pass_to_pass`` (empty in mined
    phase-1 tasks) must all keep passing — included now so the scoring path
    is already correct when the corpus grows them.
    """
    from wells.evolve.corpus import source_env as _source_env

    oracle = task.test_oracle
    targets = list(oracle.fail_to_pass) + list(oracle.pass_to_pass)
    if not targets:
        return False, {}, "oracle has no test targets"
    command = oracle.command or "python -m pytest -q"
    argv = resolve_command_argv(command) + targets
    per_test: dict[str, bool] = {}
    try:
        proc = subprocess.run(
            argv,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=_ORACLE_TIMEOUT,
            env=_source_env(worktree),
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0:
            return True, {t: True for t in targets}, ""
        # Suite red: rerun EVERY target individually — both to record which
        # fail_to_pass the patch actually fixed (partial-credit diagnostics)
        # and to check pass_to_pass regressions the combined run hid.
        cmd_argv = resolve_command_argv(command)
        for t in targets:
            p = subprocess.run(
                cmd_argv + [t],
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=_ORACLE_TIMEOUT,
                env=_source_env(worktree),
                encoding="utf-8",
                errors="replace",
            )
            per_test[t] = p.returncode == 0
        resolved = all(per_test.get(t, False) for t in oracle.fail_to_pass) and all(
            per_test.get(t, False) for t in oracle.pass_to_pass
        )
        return resolved, per_test, ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, {}, f"oracle infra failure: {type(e).__name__}: {e}"[:300]


def _execute_task(
    task: TaskSpec,
    seed: int,
    *,
    profile: str,
    worktree_root: Path,
    timeout: float,
    extra_env: dict[str, str] | None = None,
) -> TaskResult:
    """Worktree at base → run harness → score oracle → always clean up.

    Uses ``task.repo_root`` (not a caller-supplied repo path) — a corpus
    can span multiple repos (each task carries its own origin), so the
    worktree must be created against the specific repo this task was
    mined from, never a shared/assumed one.
    """
    repo_root = task.repo_root
    wt = str(worktree_root / f"{task.task_id}-s{seed}")
    result = TaskResult(task_id=task.task_id, seed=seed, resolved=False)
    ok, out = git(repo_root, "worktree", "add", "--detach", wt, task.base_commit)
    if not ok:
        result.error = f"worktree add failed: {out[:200]}"
        return result
    try:
        t0 = time.time()
        try:
            # extra_env only passed through when set: keeps this call
            # compatible with test stubs that monkeypatch _run_harness with
            # a narrower signature (profile/timeout only, no extra_env).
            harness_kwargs = {"profile": profile, "timeout": timeout}
            if extra_env:
                harness_kwargs["extra_env"] = extra_env
            payload = _run_harness(wt, task.problem_statement, **harness_kwargs)
            result.harness_status = payload.get("status", "error")
            tokens = payload.get("tokens") or {}
            result.tokens_total = int(tokens.get("total") or 0)
            result.cost_usd = payload.get("cost_usd")
            if payload.get("status") == "error" and not result.error:
                result.error = (payload.get("error") or "")[:300]
        except subprocess.TimeoutExpired:
            result.error = f"harness run timed out after {timeout}s"
            result.harness_status = "timeout"
        result.duration_seconds = round(time.time() - t0, 1)
        _apply_test_patch(wt, task)
        result.resolved, result.fail_to_pass_results, oracle_err = _score_oracle(
            wt, task
        )
        if oracle_err and not result.error:
            result.error = oracle_err
        return result
    finally:
        git(repo_root, "worktree", "remove", "--force", wt)


def _apply_test_patch(worktree: str, task: TaskSpec) -> None:
    """Apply the fix commit's test files onto the agent's worktree.

    SWE-bench semantics: the agent works from the problem statement alone
    (base state, no target tests); at scoring time the *authoritative* test
    versions from the fix commit are checked out over whatever is there —
    adding the new tests and overwriting any edits the agent made to them.
    Best-effort: a failure here surfaces through the oracle run's own
    output rather than aborting the row.
    """
    test_files = list(task.test_oracle.test_files)
    if not test_files and task.source_commit:
        ok, out = git(
            worktree,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            task.source_commit,
        )
        if ok:
            from wells.evolve.corpus import is_test_path

            test_files = [f for f in out.splitlines() if f.strip() and is_test_path(f)]
    if not test_files or not task.source_commit:
        return
    git(worktree, "checkout", task.source_commit, "--", *test_files)


# ---------------------------------------------------------------------------
# Bench run + results
# ---------------------------------------------------------------------------


@dataclass
class BenchRun:
    bench_id: str
    split: str
    profile: str
    created_at: str
    tasks: list[TaskResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def save(self, workspace: str) -> Path:
        path = results_dir(workspace) / f"{self.bench_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "BenchRun":
        d = json.loads(path.read_text(encoding="utf-8"))
        rows = [TaskResult(**r) for r in d.get("tasks", [])]
        return cls(
            bench_id=d["bench_id"],
            split=d.get("split", ""),
            profile=d.get("profile", ""),
            created_at=d.get("created_at", ""),
            tasks=rows,
            summary=d.get("summary", {}),
        )


def summarize(rows: list[TaskResult]) -> dict:
    n = len(rows)
    resolved = sum(1 for r in rows if r.resolved)
    durations = [r.duration_seconds for r in rows if r.duration_seconds > 0]
    token_totals = [r.tokens_total for r in rows if r.tokens_total > 0]
    costs = [r.cost_usd for r in rows if r.cost_usd is not None]
    by_task: dict[str, list[bool]] = {}
    for r in rows:
        by_task.setdefault(r.task_id, []).append(r.resolved)
    return {
        "total_runs": n,
        "tasks": len(by_task),
        "resolved": resolved,
        "pass_at_1": round(resolved / n, 4) if n else 0.0,
        "pass_at_1_wilson_lb": round(wilson_lower_bound(resolved, n), 4),
        "any_seed_resolved": sum(1 for v in by_task.values() if any(v)),
        "avg_duration_seconds": round(sum(durations) / len(durations), 1)
        if durations
        else 0.0,
        "avg_tokens": int(sum(token_totals) / len(token_totals)) if token_totals else 0,
        "total_cost_usd": round(sum(costs), 4) if costs else None,
        "harness_statuses": _count_statuses(rows),
    }


def _count_statuses(rows: list[TaskResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        s = r.harness_status or "unknown"
        out[s] = out.get(s, 0) + 1
    return out


def run_bench(
    workspace: str,
    split: str = "val",
    *,
    profile: str = "",
    limit: int = 0,
    seeds: int = 1,
    timeout: float = _RUN_TIMEOUT,
    task_filter: str = "",
    bench_home: Path | None = None,
    extra_env: dict[str, str] | None = None,
    log=print,
) -> BenchRun:
    """Run one bench over a split and persist the results.

    ``task_filter`` restricts the run to tasks whose id starts with the
    given prefix (task ids are long; prefixes are how people type them) —
    the split is still the declared source of truth, so a filtered result
    never masquerades as a full-split number.

    ``extra_env`` is passed through to each task's harness subprocess
    (e.g. ``{"WELLS_PRINCIPLES": "/path/to/candidate/AGENT.md"}`` for
    evolve's mutation gating) — never mutates this process's own
    ``os.environ``, so concurrent/sequential bench runs in one process
    (baseline pass then candidate pass) can't leak into each other.
    """
    tasks = list_tasks(workspace, split)
    if not tasks:
        raise RuntimeError(
            f"No {split!r} tasks in corpus under {workspace} — run `wells bench mine` first."
        )
    if task_filter:
        tasks = [t for t in tasks if t.task_id.startswith(task_filter)]
        if not tasks:
            raise RuntimeError(f"No task matching {task_filter!r} in split {split!r}.")
    if limit > 0:
        tasks = tasks[:limit]
    if seeds < 1:
        seeds = 1

    bench_id = new_bench_id()
    bench_home = bench_home or (Path.home() / ".wells" / "bench")
    worktree_root = bench_home / "runs" / bench_id
    worktree_root.mkdir(parents=True, exist_ok=True)

    # A corpus can span multiple repos (e.g. mined from several external
    # projects) — each task carries its own repo_root and is executed
    # against it directly (see _execute_task). Only requirement: every
    # task must actually have one, and it must resolve to a real repo on
    # this machine (mined tasks from repos that were only ever local
    # clones — like the requests/click mining today — need those clones
    # present to gate against).
    missing = [t.task_id for t in tasks if not t.repo_root]
    if missing:
        raise RuntimeError(
            f"{len(missing)} task(s) in split {split!r} have no repo_root: "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
        )
    repo_roots = sorted({t.repo_root for t in tasks})
    absent = [r for r in repo_roots if not is_git_repo(r)]
    if absent:
        raise RuntimeError(
            f"repo_root(s) not found as a git repo on this machine: {absent} "
            "— clone them (or restore the clone path) before gating this split."
        )

    run = BenchRun(
        bench_id=bench_id,
        split=split,
        profile=profile or "(active)",
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    log(
        f"[bench {bench_id}] {len(tasks)} task(s) x {seeds} seed(s) on split={split!r}, profile={run.profile}"
    )
    try:
        for i, task in enumerate(tasks, 1):
            for seed in range(1, seeds + 1):
                log(f"[bench {bench_id}] ({i}/{len(tasks)} s{seed}) {task.task_id} ...")
                row = _execute_task(
                    task,
                    seed,
                    profile=profile,
                    worktree_root=worktree_root,
                    timeout=timeout,
                    extra_env=extra_env,
                )
                run.tasks.append(row)
                log(
                    f"[bench {bench_id}] -> resolved={row.resolved} "
                    f"status={row.harness_status} tokens={row.tokens_total:,} "
                    f"{row.duration_seconds}s"
                    + (f" err={row.error[:80]}" if row.error else "")
                )
    finally:
        for r in repo_roots:
            git(r, "worktree", "prune")
        shutil.rmtree(worktree_root, ignore_errors=True)

    run.summary = summarize(run.tasks)
    run.save(workspace)
    log(
        f"[bench {bench_id}] pass@1={run.summary['pass_at_1']:.1%} "
        f"(Wilson LB {run.summary['pass_at_1_wilson_lb']:.1%}) "
        f"over {run.summary['total_runs']} run(s)"
    )
    return run


def list_results(workspace: str) -> list[Path]:
    d = results_dir(workspace)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
