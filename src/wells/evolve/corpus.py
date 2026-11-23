"""Corpus builder: mine verified task bundles out of a repo's git history.

This is the SEACS "Task Ingestion & Dataset Synthesizer" (SPEC-009 §2,
§5.1) grounded in a deterministic, SWE-bench-proven recipe:

  1. Walk non-merge commits newest-first; a commit qualifies when it changes
     at least one test file *and* at least one non-test source file. Requiring
     a test change in the same commit is what makes validation tractable: the
     test that fails at ``base_commit`` and passes after the fix is right
     there in the diff.
  2. Extract ``fail_to_pass`` test node ids from the changed *test* files at
     the fix commit (``ast``-parsed for Python — functions and methods whose
     names start with ``test_``; the file path itself for other layouts).
  3. **Dual validation** — the admission gate, entirely deterministic:
     in a throwaway worktree, check out ``base_commit`` and run the target
     tests (they MUST fail), then check out the fix commit and run them again
     (they MUST pass). Either half failing rejects the task — no LLM judgment
     anywhere in corpus construction.
  4. Assign a split (train/val/blind) by a stable hash of the task id, so
     the same corpus always partitions identically across machines, and
     re-mining after new commits never migrates an old task between splits.

Corpus storage lives in the workspace (``<ws>/.wells/bench/corpus/``) so it
can be committed and shared; validation worktrees live under
``~/.wells/bench/`` *outside* the repo for the same reason fleet keeps its
worktrees out (a nested untracked tree pollutes ``git status`` and risks
being swept into a commit).

Known phase-1 limitation (deliberate): the test command runs with the
ambient environment — a worktree has no venv/node_modules. Tasks whose
dependencies don't resolve that way simply fail validation and are skipped,
which keeps every admitted task honestly runnable. ``environment_setup``
commands (run once per worktree before validation, when provided) are the
escape hatch for repos that need more.

Portable across machines: a task records both ``repo_root`` (a path on the
machine that mined it) and ``repo_url`` (where anyone can get the same
commits). :func:`resolve_repo_root` turns that pair into a checkout on the
current machine, cloning a mirror under ``~/.wells/bench/repos/`` when
needed, and :func:`verify_task` re-runs a task's dual validation here, so a
corpus committed from one machine can actually run on another.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from wells._gitutils import git, is_git_repo
from wells.evolve.schema import TaskSpec, Oracle, subject_is_mineable

# Workspace-relative corpus layout (committed/shareable).
CORPUS_REL = Path(".wells") / "bench" / "corpus"
TASKS_DIR_NAME = "tasks"
MANIFEST_NAME = "manifest.json"

# Split sizes, per SPEC-009 §5.1: 60% train / 20% validation / 20% blind.
_SPLIT_RATIOS = (("train", 0.60), ("val", 0.20), ("blind", 0.20))

# A commit that changes more than this many files is almost always a
# refactor/rename sweep, not a fix — and blows up validation time.
_MAX_CHANGED_FILES = 40

_TEST_PATH_RE = re.compile(
    r"(^|[\\/])(tests?|spec)[\\/]|(^|[\\/])test_[^\\/]+\.py$|[^\\/]+_test\.(py|go|rs|ts|js)$|[^\\/]+\.test\.(ts|js)$",
    re.IGNORECASE,
)

_SETUP_TIMEOUT = 600.0  # per environment_setup command
_TEST_RUN_TIMEOUT = 300.0  # per validation test run
_CLONE_TIMEOUT = 900.0  # one-off mirror clone of a task's source repo


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def default_bench_home() -> Path:
    """Per-machine scratch home for bench worktrees, mirrors and runs."""
    return Path.home() / ".wells" / "bench"


def corpus_dir(workspace: str) -> Path:
    return Path(workspace) / CORPUS_REL


def tasks_dir(workspace: str) -> Path:
    return corpus_dir(workspace) / TASKS_DIR_NAME


def manifest_path(workspace: str) -> Path:
    return corpus_dir(workspace) / MANIFEST_NAME


# ---------------------------------------------------------------------------
# Repo resolution: a task's source repo on THIS machine
# ---------------------------------------------------------------------------


def normalize_repo_url(url: str) -> str:
    """Reduce a clone URL to ``host/owner/repo``.

    ``https://github.com/o/r.git`` and ``git@github.com:o/r`` name the same
    repository; comparing the reduced form is what lets a task mined from
    an ssh clone resolve against an https one.
    """
    u = (url or "").strip().lower()
    u = re.sub(r"^[a-z]+://", "", u)  # scheme
    u = re.sub(r"^[^@/]+@", "", u)  # user@ (ssh)
    if ":" in u and "/" not in u.split(":", 1)[0]:
        u = u.replace(":", "/", 1)  # scp-like host:path
    u = u.rstrip("/")
    return u[:-4] if u.endswith(".git") else u


def origin_url(repo_root: str) -> str:
    """The repo's ``origin`` clone URL, or ``""`` when it has none."""
    ok, out = git(repo_root, "config", "--get", "remote.origin.url")
    return out.strip() if ok else ""


def repo_mirror_dir(url: str, bench_home: Path | None = None) -> Path:
    """Where Wells keeps its own clone of ``url``: ``<bench_home>/repos/<name>``."""
    name = normalize_repo_url(url).rsplit("/", 1)[-1]
    return (bench_home or default_bench_home()) / "repos" / (name or "repo")


def _clone_mirror(url: str, dest: Path) -> tuple[bool, str]:
    """Clone ``url`` to ``dest``. Returns (ok, error tail)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "clone", "--quiet", url, str(dest)],
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return False, (proc.stderr or "").strip()[-300:]
    return True, ""


def resolve_repo_root(
    task: TaskSpec,
    *,
    workspace: str | None = None,
    bench_home: Path | None = None,
    clone: bool = True,
    log=None,
) -> str | None:
    """A checkout of the task's repo that exists on this machine, or None.

    ``repo_root`` is an absolute path on whichever machine mined the task.
    A committed corpus is therefore unusable the moment it is cloned
    elsewhere, which is how 58 of this project's 70 tasks ended up pointing
    at directories that existed on one machine only. Resolution order:

    1. ``repo_root`` itself, when it is a git repo here.
    2. The workspace, when it is a clone of the task's ``repo_url`` (no
       second copy of a repo the user is already sitting in).
    3. A mirror Wells manages under ``<bench_home>/repos/<name>``, cloned
       from ``repo_url`` the first time it is needed (unless ``clone`` is
       false, for read-only reporting).

    A task with neither a reachable path nor a URL cannot be resolved; the
    caller decides whether that is fatal (a bench run) or just a report.
    """
    if task.repo_root and is_git_repo(task.repo_root):
        return task.repo_root
    if not task.repo_url:
        return None
    if workspace and is_git_repo(workspace):
        if normalize_repo_url(origin_url(workspace)) == normalize_repo_url(task.repo_url):
            return str(workspace)
    mirror = repo_mirror_dir(task.repo_url, bench_home)
    if is_git_repo(str(mirror)):
        return str(mirror)
    if not clone:
        return None
    if log:
        log(f"[repos] cloning {task.repo_url} -> {mirror}")
    ok, err = _clone_mirror(task.repo_url, mirror)
    if not ok:
        if log:
            log(f"[repos] clone failed: {err}")
        return None
    return str(mirror)


# ---------------------------------------------------------------------------
# Split assignment — deterministic, stable across machines and re-minings
# ---------------------------------------------------------------------------


def assign_split(task_id: str) -> str:
    """Map a task id to train/val/blind via a stable 60/20/20 hash split.

    sha1 (not Python's randomized hash) so the partition is identical
    everywhere; the task id (derived from the commit sha) never changes, so
    an admitted task's split is permanent.
    """
    h = int.from_bytes(hashlib.sha1(task_id.encode("utf-8")).digest()[:8], "big")
    bucket = (h % 10_000) / 10_000.0
    acc = 0.0
    for name, ratio in _SPLIT_RATIOS:
        acc += ratio
        if bucket < acc:
            return name
    return _SPLIT_RATIOS[-1][0]


# ---------------------------------------------------------------------------
# Commit walking & test extraction
# ---------------------------------------------------------------------------


@dataclass
class CommitCandidate:
    sha: str
    subject: str
    body: str
    base_commit: str
    changed_files: list[str]
    test_files: list[str]


def _list_changed_files(repo_root: str, sha: str) -> list[str]:
    ok, out = git(repo_root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    if not ok:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path.replace("\\", "/")))


def _parse_test_node_ids(source: str, file_path: str) -> list[str]:
    """Extract pytest node ids (``path::test_fn``, ``path::Class::test_fn``)
    from one Python test module via ``ast``.

    Anything unparseable (non-Python, syntax error) yields the bare file
    path — pytest accepts a path alone and runs every test in it, which is
    coarser but still a valid oracle.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [file_path]
    ids: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            ids.append(f"{file_path}::{node.name}")
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name.startswith("test_"):
                    ids.append(f"{file_path}::{node.name}::{sub.name}")
    return ids or [file_path]


def _extract_fail_to_pass(repo_root: str, cand: CommitCandidate) -> list[str]:
    """Node ids for tests added/changed by the fix commit.

    Read from the *fix* commit's tree (``git show sha:path``) — the state
    the tests must eventually pass against. Deleted test files contribute
    nothing (they don't exist to run).
    """
    ids: list[str] = []
    for tf in cand.test_files:
        if not tf.endswith(".py"):
            ids.append(tf)
            continue
        ok, out = git(repo_root, "show", f"{cand.sha}:{tf.replace(chr(92), '/')}")
        if ok:
            ids.extend(_parse_test_node_ids(out, tf))
    # De-dup, keep order (stable oracles make diffs between runs readable).
    seen: set[str] = set()
    return [x for x in ids if not (x in seen or seen.add(x))]


def iter_candidates(repo_root: str, *, max_commits: int = 500) -> list[CommitCandidate]:
    """Walk history newest-first, returning commits worth dual-validating."""
    ok, out = git(
        repo_root,
        "log",
        "--no-merges",
        "-n",
        str(max_commits),
        "--format=%H%x00%s%x00%b%x1e",
    )
    if not ok:
        return []
    candidates: list[CommitCandidate] = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\x00", 2)
        if len(parts) != 3:
            continue
        sha, subject, body = (p.strip() for p in parts)
        if not subject_is_mineable(subject):
            continue
        ok2, base = git(repo_root, "rev-parse", f"{sha}^")
        if not ok2 or not base:
            continue  # root commit has no parent — nothing to fix
        changed = _list_changed_files(repo_root, sha)
        if not changed or len(changed) > _MAX_CHANGED_FILES:
            continue
        test_files = [f for f in changed if is_test_path(f)]
        source_files = [f for f in changed if not is_test_path(f)]
        if not test_files or not source_files:
            continue
        candidates.append(
            CommitCandidate(
                sha=sha,
                subject=subject,
                body=body,
                base_commit=base,
                changed_files=changed,
                test_files=test_files,
            )
        )
    return candidates


# ---------------------------------------------------------------------------
# Dual validation — the deterministic admission gate
# ---------------------------------------------------------------------------


def _make_task_id(sha: str, subject: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")[:32]
    slug = re.sub(r"-+", "-", slug) or "fix"
    return f"T-{sha[:10]}-{slug}"


def _detect_test_command(repo_root: str) -> str:
    """Same ladder the harness's ``run_tests`` tool uses (tools.py), inlined
    here so the oracle runs exactly what a live Wells run would run without
    reaching into tools' private ToolContext-typed helper. Keep the two in
    sync if the ladder in tools.py ever changes.
    """
    root = Path(repo_root)
    if (root / "pyproject.toml").exists() and (root / ".venv").exists():
        return "uv run python -m pytest -q"
    if (root / "pyproject.toml").exists():
        return "python -m pytest -q"
    if (root / "package.json").exists():
        return "npm test --silent"
    if (root / "Cargo.toml").exists():
        return "cargo test"
    if (root / "go.mod").exists():
        return "go test ./..."
    return "python -m pytest -q"


def resolve_command_argv(command: str) -> list[str]:
    """Split a test command, resolving a bare ``python`` to ``sys.executable``.

    A bare ``python`` on PATH can resolve to an interpreter without pytest
    (uv's managed base Python, a system install, ...). The interpreter Wells
    itself is running under is the one the user set up — substituting its
    absolute path makes oracle runs deterministic instead of PATH-dependent.
    """
    argv = command.split()
    if argv and argv[0].lower() in ("python", "python3", "python.exe", "py"):
        argv[0] = sys.executable
    return argv


def source_env(worktree: str) -> dict:
    """Subprocess env that puts the *worktree's own source* first on sys.path.

    The subtle failure this prevents: in a src-layout repo (Wells itself),
    a worktree checked out at a historical commit still imports the
    *currently installed* package from site-packages — the editable/.pth
    install wins because nothing else is ahead of it. Base and fix runs
    then execute identical code and the oracle never sees the agent's (or
    history's) edits at all.

    Prepending ``<worktree>/src`` (or the worktree root for flat layouts)
    to PYTHONPATH puts the worktree's source ahead of site-packages, so
    test runs genuinely exercise the checked-out code. PYTHONPATH entries
    sit before site-packages but after the script dir — exactly the
    precedence needed.
    """
    import os

    wt = Path(worktree)
    src = wt / "src"
    prefix = str(src if src.is_dir() else wt)
    existing = os.environ.get("PYTHONPATH", "")
    parts = [prefix] + [p for p in existing.split(os.pathsep) if p]
    # The launching shell's proxy settings are dropped: requests' own suite
    # asserts on the absence of ``no_proxy``, so an agent session that
    # exports NO_PROXY made a correct fix commit fail validation. An oracle
    # must measure the patch, not the environment it was started from.
    inherited = {
        k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")
    }
    return {
        **inherited,
        "UV_LINK_MODE": "copy",
        "PYTHONPATH": os.pathsep.join(parts),
    }


def _run_tests_in(
    worktree: str,
    test_command: str,
    node_ids: list[str],
    *,
    env_setup: list[str] | None = None,
) -> tuple[bool | None, str]:
    """Run ``test_command <node_ids>`` inside ``worktree``.

    Returns (passed, tail). ``passed`` is None when the run couldn't execute
    at all (command not found, timeout) — an infra failure, not a test
    verdict, and never mistaken for one by callers.
    """
    env = source_env(worktree)
    if env_setup:
        for cmd in env_setup:
            try:
                subprocess.run(
                    cmd,
                    shell=True,
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=_SETUP_TIMEOUT,
                    env=env,
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception:
                return None, f"environment_setup failed: {cmd}"

    argv = resolve_command_argv(test_command) + list(node_ids)
    try:
        proc = subprocess.run(
            argv,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=_TEST_RUN_TIMEOUT,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return None, f"{type(e).__name__}: {e}"
    tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-25:])
    return proc.returncode == 0, tail


def _dual_validate(
    repo_root: str,
    cand: CommitCandidate,
    fail_to_pass: list[str],
    test_command: str,
    *,
    env_setup: list[str] | None,
    worktree_root: Path,
) -> tuple[bool, str]:
    """The gate: tests must FAIL at base and PASS at the fix commit.

    One worktree, two checkouts — created and always removed here. A task
    admitted by this gate is provably runnable and provably discriminating
    (a wrong patch cannot pass it by accident of a broken suite).
    """
    wt = str(worktree_root / cand.sha[:12])
    ok, out = git(repo_root, "worktree", "add", "--detach", wt, cand.base_commit)
    if not ok:
        return False, f"worktree add failed: {out[:200]}"
    try:
        # Half 1: at base, the target tests must fail.
        base_pass, base_tail = _run_tests_in(
            wt,
            test_command,
            fail_to_pass,
            env_setup=env_setup,
        )
        if base_pass is None:
            return False, f"base run infra-failed: {base_tail[:200]}"
        if base_pass:
            return False, "target tests already PASS at base — not a fix task"

        # Half 2: at the fix commit, the same tests must pass.
        ok, out = git(wt, "checkout", "--detach", cand.sha)
        if not ok:
            return False, f"checkout of fix commit failed: {out[:200]}"
        fix_pass, fix_tail = _run_tests_in(
            wt,
            test_command,
            fail_to_pass,
            env_setup=env_setup,
        )
        if fix_pass is None:
            return False, f"fix run infra-failed: {fix_tail[:200]}"
        if not fix_pass:
            return False, f"target tests FAIL at fix commit: {fix_tail[:200]}"
        return True, ""
    finally:
        git(repo_root, "worktree", "remove", "--force", wt)


# ---------------------------------------------------------------------------
# Re-verification: is a stored task still valid on this machine?
# ---------------------------------------------------------------------------


def verify_task(
    workspace: str,
    task: TaskSpec,
    *,
    bench_home: Path | None = None,
    log=None,
) -> tuple[bool, str]:
    """Re-run one stored task's dual validation here and record the result.

    ``status: verified`` in a bundle is a claim about the machine that
    verified it: the repo has to be obtainable here and the tests have to
    run here, and a corpus committed from another machine has never been
    checked against either. Returns ``(ok, reason)``.

    A success is stored in the bundle (it now holds on that machine and on
    this one). A failure deliberately is not: the task may be perfectly
    valid where it was verified, and is often only missing a test plugin
    here. It goes into a per-machine record instead
    (:func:`local_verify_failures`), which bench runs on this machine use
    to leave the task out rather than score it against an oracle that
    cannot pass.
    """
    status = "unverified"
    repo_root = resolve_repo_root(
        task, workspace=workspace, bench_home=bench_home, log=log
    )
    if repo_root is None:
        reason = (
            "source repo not obtainable on this machine: repo_root="
            f"{task.repo_root!r} is not a git repo here and repo_url="
            f"{task.repo_url or '(none)'!r}"
        )
    elif not task.source_commit:
        reason = "task has no source_commit to validate against"
    else:
        worktree_root = (bench_home or default_bench_home()) / "mine"
        worktree_root.mkdir(parents=True, exist_ok=True)
        ok, reason = _dual_validate(
            repo_root,
            CommitCandidate(
                sha=task.source_commit,
                subject="",
                body="",
                base_commit=task.base_commit,
                changed_files=[],
                test_files=list(task.test_oracle.test_files),
            ),
            list(task.test_oracle.fail_to_pass),
            task.test_oracle.command or _detect_test_command(repo_root),
            env_setup=list(task.environment_setup) or None,
            worktree_root=worktree_root,
        )
        if ok:
            status, reason = "verified", ""
    if status == "verified":
        task.status = "verified"
        task.save(tasks_dir(workspace))
    _record_local_verify(workspace, task.task_id, status == "verified", reason)
    return status == "verified", reason


# ---------------------------------------------------------------------------
# Per-machine verification record
# ---------------------------------------------------------------------------
# Lives next to the corpus and is git-ignored like everything else under
# .wells/: whether an oracle can run is a fact about the machine, so it must
# never travel with the corpus the way ``status`` does.

LOCAL_VERIFY_NAME = "local-verify.json"


def local_verify_failures(workspace: str) -> dict[str, str]:
    """task_id -> reason, for tasks whose validation failed on this machine."""
    try:
        data = json.loads(
            (corpus_dir(workspace) / LOCAL_VERIFY_NAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        str(k): str(v.get("reason", ""))
        for k, v in data.items()
        if isinstance(v, dict) and not v.get("ok")
    }


def _record_local_verify(workspace: str, task_id: str, ok: bool, reason: str) -> None:
    failures = local_verify_failures(workspace)
    if ok:
        failures.pop(task_id, None)
    else:
        failures[task_id] = reason
    path = corpus_dir(workspace) / LOCAL_VERIFY_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        tid: {"ok": False, "reason": why, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        for tid, why in sorted(failures.items())
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def verify_corpus(
    workspace: str,
    tasks: list[TaskSpec],
    *,
    bench_home: Path | None = None,
    log=None,
) -> list[tuple[TaskSpec, bool, str]]:
    """Verify each task (see :func:`verify_task`) and refresh the manifest."""
    results = []
    for task in tasks:
        ok, reason = verify_task(
            workspace, task, bench_home=bench_home, log=log
        )
        results.append((task, ok, reason))
    save_manifest(workspace, load_corpus(workspace))
    return results


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def save_manifest(workspace: str, tasks: list[TaskSpec]) -> Path:
    path = manifest_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(tasks),
        "splits": {
            name: sum(1 for t in tasks if t.split == name) for name, _ in _SPLIT_RATIOS
        },
        "tasks": [
            {
                "task_id": t.task_id,
                "split": t.split,
                "status": t.status,
                "source_commit": t.source_commit,
                "subject": t.problem_statement.splitlines()[0][:80]
                if t.problem_statement
                else "",
            }
            for t in tasks
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_corpus(workspace: str) -> list[TaskSpec]:
    """Load every task bundle on disk, validating each (fail loudly)."""
    from wells.evolve.schema import SchemaError

    tdir = tasks_dir(workspace)
    if not tdir.is_dir():
        return []
    out: list[TaskSpec] = []
    for p in sorted(tdir.glob("*.json")):
        try:
            out.append(TaskSpec.load(p))
        except (json.JSONDecodeError, KeyError, SchemaError) as e:
            raise SchemaError(f"corpus file {p.name} is malformed: {e}") from e
    return out


def list_tasks(workspace: str, split: str = "all") -> list[TaskSpec]:
    tasks = [
        t for t in load_corpus(workspace) if t.status != "rejected"
    ]  # quarantined tasks stay on disk (audit trail) but never run
    if split == "all":
        return tasks
    return [t for t in tasks if t.split == split]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def mine_corpus(
    repo_root: str,
    workspace: str,
    *,
    max_tasks: int = 50,
    max_commits: int = 500,
    skip_validation: bool = False,
    env_setup: list[str] | None = None,
    bench_home: Path | None = None,
    log=print,
) -> tuple[list[TaskSpec], dict]:
    """Mine the repo's history into verified task bundles.

    Returns ``(admitted_tasks, stats)``. Only *admitted* tasks are written
    to disk; rejected candidates are counted, never stored — a corpus
    directory only ever contains runnable, discriminating tasks.
    """
    if not is_git_repo(repo_root):
        raise RuntimeError(
            f"{repo_root} is not a git repository — bench mining requires git."
        )

    bench_home = bench_home or default_bench_home()
    worktree_root = bench_home / "mine"
    worktree_root.mkdir(parents=True, exist_ok=True)

    existing_tasks = load_corpus(workspace)
    known_ids = {t.task_id for t in existing_tasks}
    # A history rewrite changes every SHA, so a task id (sha[:10] + slug)
    # stops recognising its own commit: re-mining a rewritten repo would
    # re-admit every task already in the corpus under a new id. The fix
    # commit's message together with the test files it changed identify a
    # task across rewrites.
    known_keys = {
        (t.problem_statement, tuple(sorted(t.test_oracle.test_files)))
        for t in existing_tasks
    }
    # Recorded on every task so another machine can obtain the same commits
    # even though repo_root is a path that only exists on this one.
    repo_url = origin_url(repo_root)
    test_command = _detect_test_command(repo_root)

    stats = {
        "candidates": 0,
        "tested": 0,
        "admitted": 0,
        "rejected": 0,
        "skipped_existing": 0,
        "rejections": [],
    }
    admitted: list[TaskSpec] = []

    for cand in iter_candidates(repo_root, max_commits=max_commits):
        if len(admitted) >= max_tasks:
            break
        stats["candidates"] += 1
        task_id = _make_task_id(cand.sha, cand.subject)
        if task_id in known_ids:
            stats["skipped_existing"] += 1
            continue

        fail_to_pass = _extract_fail_to_pass(repo_root, cand)
        if not fail_to_pass:
            continue

        problem = f"{cand.subject}\n\n{cand.body}".strip() or cand.subject
        key = (problem, tuple(sorted(cand.test_files)))
        if key in known_keys:
            stats["skipped_existing"] += 1
            continue
        known_keys.add(key)
        task = TaskSpec(
            task_id=task_id,
            repo_root=str(Path(repo_root).resolve()),
            repo_url=repo_url,
            base_commit=cand.base_commit,
            problem_statement=problem,
            test_oracle=Oracle(
                fail_to_pass=fail_to_pass,
                pass_to_pass=[],
                command=test_command,
                test_files=list(cand.test_files),
            ),
            environment_setup=list(env_setup or []),
            source_commit=cand.sha,
            split=assign_split(task_id),
            status="unverified",
            mined_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        task.validate()

        if skip_validation:
            task.status = "unverified"
            task.save(tasks_dir(workspace))
            admitted.append(task)
            stats["admitted"] += 1
            log(f"  [mine] + {task_id} ({task.split}, unverified)")
            continue

        stats["tested"] += 1
        ok, reason = _dual_validate(
            repo_root,
            cand,
            fail_to_pass,
            test_command,
            env_setup=env_setup,
            worktree_root=worktree_root,
        )
        if not ok:
            stats["rejected"] += 1
            stats["rejections"].append({"task_id": task_id, "reason": reason[:300]})
            log(f"  [mine] - {task_id} rejected: {reason[:120]}")
            continue

        task.status = "verified"
        task.save(tasks_dir(workspace))
        admitted.append(task)
        stats["admitted"] += 1
        log(f"  [mine] + {task_id} ({task.split}, verified)")

    all_tasks = load_corpus(workspace)
    save_manifest(workspace, all_tasks)
    try:
        shutil.rmtree(worktree_root, ignore_errors=True)
    except OSError:
        pass
    return admitted, stats
