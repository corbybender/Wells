"""Tests for the bench/evolution subsystem: schema, deterministic split
assignment, corpus mining (with real dual validation on real git repos),
oracle scoring, and the Wilson-bounded metrics."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from wells import evolve
from wells.evolve import corpus as corp
from wells.evolve import runner as run_mod
from wells.evolve.schema import SchemaError, TaskSpec, Oracle


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _spec(task_id: str, split: str = "train", status: str = "verified") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repo_root="/tmp/repo",
        base_commit="a" * 40,
        problem_statement=f"problem for {task_id}",
        test_oracle=Oracle(
            fail_to_pass=["tests/test_x.py::test_a"],
            pass_to_pass=[],
            command="python -m pytest -q",
            test_files=["tests/test_x.py"],
        ),
        environment_setup=[],
        source_commit="b" * 40,
        split=split,
        status=status,
        mined_at="2026-11-20 00:00:00",
    )


def test_list_tasks_excludes_rejected(tmp_path: Path):
    """Quarantined (status=rejected) tasks stay on disk but never run."""
    tdir = corp.tasks_dir(str(tmp_path))
    tdir.mkdir(parents=True)
    good = _spec("T-good000000000000000000000000000")
    bad = _spec("T-bad000000000000000000000000000", status="rejected")
    good.save(tdir)
    bad.save(tdir)

    listed = corp.list_tasks(str(tmp_path))
    assert [t.task_id for t in listed] == [good.task_id]

    # Split filtering still applies on top of the status filter.
    val = _spec("T-val000000000000000000000000000", split="val", status="rejected")
    val.save(tdir)
    assert corp.list_tasks(str(tmp_path), "val") == []


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    ok, out = corp.git(str(repo), "rev-parse", "HEAD")
    assert ok, out
    return out.strip()


@pytest.fixture
def repo_with_fix(tmp_path: Path) -> Path:
    """A repo whose history is exactly one mineable fix:

    commit A (base): calc.py with a buggy add(); no tests yet
    commit B (fix):  fixes add() AND adds test_calc.py::test_add
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "calc.py").write_text(
        "def add(a, b):\n    return a - b  # bug\n", encoding="utf-8"
    )
    _commit(r, "initial calculator")
    (r / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (r / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _commit(r, "fix add() to sum instead of subtract")
    return r


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_task_spec_roundtrip(tmp_path: Path):
    task = TaskSpec(
        task_id="T-abc123-fix-add",
        repo_root="/repo",
        base_commit="abc1234",
        problem_statement="fix add()",
        test_oracle=Oracle(
            fail_to_pass=["test_calc.py::test_add"], command="python -m pytest -q"
        ),
        source_commit="def5678",
        split="val",
        status="verified",
        mined_at="now",
    )
    task.validate()
    path = task.save(tmp_path)
    loaded = TaskSpec.load(path)
    assert loaded == task


def test_task_spec_validate_rejects_malformed():
    bad = TaskSpec(
        task_id="../escape",
        repo_root="/r",
        base_commit="nothex!",
        problem_statement="x",
        test_oracle=Oracle(fail_to_pass=[]),
    )
    with pytest.raises(SchemaError):
        bad.validate()


def test_subject_filter_skips_noise():
    assert corp.subject_is_mineable("fix the parser crash")
    for noise in ("Merge branch 'x'", "bump version to 1.2", "chore: deps", "WIP"):
        assert not corp.subject_is_mineable(noise)


# ---------------------------------------------------------------------------
# Split assignment
# ---------------------------------------------------------------------------


def test_assign_split_is_deterministic_and_complete():
    first = {
        tid: evolve.assign_split(tid) for tid in (f"T-{i:04d}-x" for i in range(300))
    }
    second = {
        tid: evolve.assign_split(tid) for tid in (f"T-{i:04d}-x" for i in range(300))
    }
    assert first == second  # stable across calls/processes (sha1, not hash())
    assert set(first.values()) == {"train", "val", "blind"}
    # Roughly the 60/20/20 ratios — generous windows, this is a smoke check.
    counts = {
        s: sum(1 for v in first.values() if v == s) for s in ("train", "val", "blind")
    }
    assert 0.45 < counts["train"] / 300 < 0.75
    assert 0.10 < counts["val"] / 300 < 0.30
    assert 0.10 < counts["blind"] / 300 < 0.30


# ---------------------------------------------------------------------------
# Mining with real dual validation
# ---------------------------------------------------------------------------


def test_mine_corpus_admits_dual_validated_task(repo_with_fix: Path, tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    admitted, stats = evolve.mine_corpus(
        str(repo_with_fix),
        str(ws),
        max_tasks=5,
        bench_home=tmp_path / "bench",
        log=lambda *a, **k: None,
    )
    assert len(admitted) == 1
    assert stats["admitted"] == 1 and stats["tested"] == 1
    task = admitted[0]
    assert task.status == "verified"
    assert task.base_commit and task.source_commit
    assert task.test_oracle.fail_to_pass == ["test_calc.py::test_add"]

    # Corpus persisted + manifest written with split counts.
    on_disk = evolve.list_tasks(str(ws), "all")
    assert [t.task_id for t in on_disk] == [task.task_id]
    manifest = json.loads(
        (ws / ".wells" / "bench" / "corpus" / "manifest.json").read_text()
    )
    assert manifest["total"] == 1

    # Re-mining skips the already-known task.
    _, stats2 = evolve.mine_corpus(
        str(repo_with_fix),
        str(ws),
        max_tasks=5,
        bench_home=tmp_path / "bench",
        log=lambda *a, **k: None,
    )
    assert stats2["admitted"] == 0 and stats2["skipped_existing"] == 1


def test_mine_corpus_rejects_task_that_passes_at_base(tmp_path: Path):
    """A commit whose target tests already pass at base is not a fix task."""
    r = tmp_path / "repo2"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (r / "test_ok.py").write_text(
        "from mod import f\n\n\ndef test_ok():\n    assert f() == 1\n", encoding="utf-8"
    )
    _commit(r, "initial")
    # Candidate: touches source (comment) AND the test file (comment) —
    # mineable shape, but the target test already passed at base.
    (r / "mod.py").write_text(
        "def f():\n    return 1  # documented\n", encoding="utf-8"
    )
    (r / "test_ok.py").write_text(
        "from mod import f  # tidy\n\n\ndef test_ok():\n    assert f() == 1\n",
        encoding="utf-8",
    )
    _commit(r, "document f and tidy the test")

    ws = tmp_path / "ws2"
    ws.mkdir()
    _, stats = evolve.mine_corpus(
        str(r),
        str(ws),
        max_tasks=5,
        bench_home=tmp_path / "bench",
        log=lambda *a, **k: None,
    )
    assert stats["admitted"] == 0
    assert stats["rejected"] == 1
    assert any("PASS at base" in x["reason"] for x in stats["rejections"])
    assert evolve.list_tasks(str(ws), "all") == []


def test_mine_corpus_requires_git(tmp_path: Path):
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(RuntimeError, match="not a git repository"):
        evolve.mine_corpus(
            str(d), str(d), bench_home=tmp_path, log=lambda *a, **k: None
        )


def test_src_layout_worktree_imports_its_own_source(tmp_path: Path):
    """src-layout repos: the oracle must exercise the *worktree's* source,
    not the installed package. A repo shaped like Wells (src/pkg/...) with a
    buggy module fixed in the second commit must mine successfully — without
    the PYTHONPATH injection in source_env(), `import` resolves to whatever
    is installed and base/fix runs become indistinguishable."""
    r = tmp_path / "srcrepo"
    (r / "src" / "pkgmod").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "src" / "pkgmod" / "__init__.py").write_text(
        "def double(x):\n    return x  # bug: identity, not doubling\n",
        encoding="utf-8",
    )
    (r / "pyproject.toml").write_text(
        "[build-system]\nrequires=['hatchling']\nbuild-backend='hatchling.build'\n",
        encoding="utf-8",
    )
    _commit(r, "initial")
    (r / "src" / "pkgmod" / "__init__.py").write_text(
        "def double(x):\n    return x * 2\n", encoding="utf-8"
    )
    (r / "test_double.py").write_text(
        "from pkgmod import double\n\n\ndef test_double():\n    assert double(3) == 6\n",
        encoding="utf-8",
    )
    _commit(r, "fix double() to actually double")

    ws = tmp_path / "wssrc"
    ws.mkdir()
    admitted, stats = evolve.mine_corpus(
        str(r),
        str(ws),
        max_tasks=5,
        bench_home=tmp_path / "bench",
        log=lambda *a, **k: None,
    )
    assert len(admitted) == 1, stats
    assert admitted[0].status == "verified"
    assert admitted[0].test_oracle.fail_to_pass == ["test_double.py::test_double"]


# ---------------------------------------------------------------------------
# Oracle scoring + bench run (harness monkeypatched — no LLM involved)
# ---------------------------------------------------------------------------


def _fake_harness_fixing(worktree: str):
    """Fake _run_harness whose 'model' correctly applies the fix commit's
    source change (but has never seen the diff — it just knows the answer)."""

    def _fake(worktree_: str, problem: str, *, profile: str = "", timeout: float = 0):
        src = Path(worktree_) / "calc.py"
        if src.exists():
            src.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return {
            "status": "complete",
            "tokens": {
                "input": 100,
                "output": 50,
                "total": 150,
                "calls": 3,
                "cache_read": 0,
            },
            "cost_usd": 0.001,
        }

    return _fake


def test_execute_task_scores_success_and_failure(
    repo_with_fix: Path, tmp_path: Path, monkeypatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    admitted, _ = evolve.mine_corpus(
        str(repo_with_fix),
        str(ws),
        max_tasks=5,
        bench_home=tmp_path / "bench",
        log=lambda *a, **k: None,
    )
    task = admitted[0]

    # Harness that fixes the bug -> oracle green -> resolved.
    monkeypatch.setattr(
        run_mod, "_run_harness", _fake_harness_fixing(str(repo_with_fix))
    )
    good = run_mod._execute_task(
        task,
        1,
        profile="",
        worktree_root=tmp_path / "wt",
        timeout=60.0,
    )
    assert good.resolved is True
    assert good.harness_status == "complete"
    assert good.tokens_total == 150
    assert good.fail_to_pass_results == {"test_calc.py::test_add": True}

    # Harness that does nothing -> oracle red -> not resolved (the model's
    # own "complete" claim is irrelevant; only the oracle counts).
    monkeypatch.setattr(
        run_mod,
        "_run_harness",
        lambda w, p, *, profile="", timeout=0: {
            "status": "complete",
            "tokens": {
                "input": 1,
                "output": 1,
                "total": 2,
                "calls": 1,
                "cache_read": 0,
            },
        },
    )
    bad = run_mod._execute_task(
        task,
        1,
        profile="",
        worktree_root=tmp_path / "wt2",
        timeout=60.0,
    )
    assert bad.resolved is False
    assert bad.fail_to_pass_results.get("test_calc.py::test_add") is False


def test_run_bench_end_to_end_with_stubbed_harness(
    repo_with_fix: Path, tmp_path: Path, monkeypatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    admitted, _ = evolve.mine_corpus(
        str(repo_with_fix),
        str(ws),
        max_tasks=5,
        bench_home=tmp_path / "bench",
        log=lambda *a, **k: None,
    )
    assert len(admitted) == 1
    split = admitted[0].split

    monkeypatch.setattr(
        run_mod, "_run_harness", _fake_harness_fixing(str(repo_with_fix))
    )
    run = evolve.run_bench(
        str(ws),
        split,
        profile="",
        bench_home=tmp_path / "bench",
        log=lambda *a, **k: None,
    )
    assert run.summary["resolved"] == 1
    assert run.summary["pass_at_1"] == 1.0
    assert run.summary["tasks"] == 1

    saved = ws / ".wells" / "bench" / "results" / f"{run.bench_id}.json"
    assert saved.is_file()
    reloaded = run_mod.BenchRun.load(saved)
    assert reloaded.summary["pass_at_1"] == 1.0
    assert [r.task_id for r in reloaded.tasks] == [admitted[0].task_id]


def test_run_bench_checkpoints_and_resumes_after_a_simulated_crash(
    repo_with_fix: Path, tmp_path: Path, monkeypatch
):
    """Fault-injection test: a bench run that dies mid-way (process kill,
    session death — simulated here as an exception on task 2) must leave
    task 1's result on disk, and a resumed call with the same bench_id
    must pick up from there — not re-execute task 1, and finish with both
    tasks recorded."""
    r2 = tmp_path / "repo2"
    r2.mkdir()
    _git(r2, "init", "-q")
    _git(r2, "config", "user.email", "t@example.com")
    _git(r2, "config", "user.name", "t")
    (r2 / "mul.py").write_text(
        "def mul(a, b):\n    return a + b  # bug\n", encoding="utf-8"
    )
    _commit(r2, "initial multiplier")
    (r2 / "mul.py").write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
    (r2 / "test_mul.py").write_text(
        "from mul import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
        encoding="utf-8",
    )
    _commit(r2, "fix mul() to multiply instead of add")

    ws = tmp_path / "ws_crash"
    ws.mkdir()
    admitted1, _ = evolve.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench1", log=lambda *a, **k: None,
    )
    admitted2, _ = evolve.mine_corpus(
        str(r2), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench2", log=lambda *a, **k: None,
    )
    assert len(admitted1) == 1 and len(admitted2) == 1
    tasks_dir = corp.tasks_dir(str(ws))
    for t in (admitted1[0], admitted2[0]):
        t.split = "val"
        t.save(tasks_dir)

    # Task execution order (calc vs. mul first) depends on content-hash
    # -derived task ids, which are NOT stable across repo instances/runs —
    # crash on whichever task runs *second* (by call count), not on a
    # hardcoded "mul always crashes" assumption. Fixes a real flake: this
    # test passed in isolation but failed in the full suite because task
    # order flipped, crashing before task 1 ever got a chance to
    # checkpoint.
    calls: list[str] = []

    def crashing_harness(worktree, problem, *, profile="", timeout=0, extra_env=None):
        calc = Path(worktree) / "calc.py"
        name = "calc" if calc.exists() else "mul"
        calls.append(name)
        if len(calls) == 1:
            (calc if calc.exists() else Path(worktree) / "mul.py").write_text(
                "def add(a, b):\n    return a + b\n"
                if calc.exists()
                else "def mul(a, b):\n    return a * b\n",
                encoding="utf-8",
            )
            return {"status": "complete", "tokens": {"total": 10}}
        raise RuntimeError("simulated crash mid-task (e.g. process killed)")

    monkeypatch.setattr(run_mod, "_run_harness", crashing_harness)

    bench_id = "fixed-crash-test-id"
    with pytest.raises(RuntimeError, match="simulated crash"):
        evolve.run_bench(
            str(ws), "val", bench_id=bench_id,
            bench_home=tmp_path / "bench_run", log=lambda *a, **k: None,
        )

    # Checkpoint survived: the first task's result is on disk despite the
    # second task crashing.
    saved = ws / ".wells" / "bench" / "results" / f"{bench_id}.json"
    assert saved.is_file()
    partial = run_mod.BenchRun.load(saved)
    assert len(partial.tasks) == 1
    assert len(calls) == 2  # first succeeded and got checkpointed; second crashed
    first_task_id = partial.tasks[0].task_id

    def fixed_harness(worktree, problem, *, profile="", timeout=0, extra_env=None):
        calc = Path(worktree) / "calc.py"
        mul = Path(worktree) / "mul.py"
        if calc.exists():
            calls.append("calc")
            calc.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        if mul.exists():
            calls.append("mul")
            mul.write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
        return {"status": "complete", "tokens": {"total": 10}}

    monkeypatch.setattr(run_mod, "_run_harness", fixed_harness)
    calls.clear()
    run = evolve.run_bench(
        str(ws), "val", bench_id=bench_id, resume=True,
        bench_home=tmp_path / "bench_run", log=lambda *a, **k: None,
    )
    # The first task was already recorded before the crash — resume must
    # re-run only the one that crashed, never the already-checkpointed one.
    # Task ids embed a readable slug of the fix commit's subject ("fix
    # add()..." vs. "fix mul()..."), so which repo completed first is
    # recoverable from the id string itself.
    first_was_mul = "mul" in first_task_id
    assert len(calls) == 1
    assert calls[0] == ("calc" if first_was_mul else "mul")
    assert run.summary["tasks"] == 2
    assert run.summary["resolved"] == 2


def test_run_bench_rejects_unknown_split(repo_with_fix: Path, tmp_path: Path):
    ws = tmp_path / "ws3"
    ws.mkdir()
    with pytest.raises(RuntimeError, match="No 'nope' tasks"):
        evolve.run_bench(str(ws), "nope", bench_home=tmp_path, log=lambda *a, **k: None)


def test_run_bench_supports_a_split_spanning_multiple_repos(
    repo_with_fix: Path, tmp_path: Path, monkeypatch
):
    """A corpus can be mined from several external repos — a split must be
    able to mix tasks whose repo_root differs, executing each against its
    own repo rather than requiring (or assuming) one shared repo_root."""
    # Second, independent repo with its own single mineable fix.
    r2 = tmp_path / "repo2"
    r2.mkdir()
    _git(r2, "init", "-q")
    _git(r2, "config", "user.email", "t@example.com")
    _git(r2, "config", "user.name", "t")
    (r2 / "mul.py").write_text(
        "def mul(a, b):\n    return a + b  # bug\n", encoding="utf-8"
    )
    _commit(r2, "initial multiplier")
    (r2 / "mul.py").write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
    (r2 / "test_mul.py").write_text(
        "from mul import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
        encoding="utf-8",
    )
    _commit(r2, "fix mul() to multiply instead of add")

    ws = tmp_path / "ws_multi"
    ws.mkdir()
    admitted1, _ = evolve.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench1", log=lambda *a, **k: None,
    )
    admitted2, _ = evolve.mine_corpus(
        str(r2), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench2", log=lambda *a, **k: None,
    )
    assert len(admitted1) == 1 and len(admitted2) == 1

    # Force both into the same split regardless of their natural hash —
    # the scenario under test is "one split, two repos", not split luck.
    tasks_dir = corp.tasks_dir(str(ws))
    for t in (admitted1[0], admitted2[0]):
        t.split = "val"
        t.save(tasks_dir)

    def fake_run_harness(worktree, problem, *, profile="", timeout=0, extra_env=None):
        # Apply whichever fix this worktree's repo needs, by checking which
        # source file is present.
        calc = Path(worktree) / "calc.py"
        mul = Path(worktree) / "mul.py"
        if calc.exists():
            calc.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        if mul.exists():
            mul.write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
        return {"status": "complete", "tokens": {"total": 10}}

    monkeypatch.setattr(run_mod, "_run_harness", fake_run_harness)
    run = evolve.run_bench(
        str(ws), "val", bench_home=tmp_path / "bench_run", log=lambda *a, **k: None
    )
    assert run.summary["tasks"] == 2
    assert run.summary["resolved"] == 2


def test_run_bench_workers_parallelizes_and_checkpoints_safely(
    repo_with_fix: Path, tmp_path: Path, monkeypatch
):
    """workers>1 must (a) actually run concurrently — proven by wall-clock
    time dropping below what two serial 0.3s tasks would take — and (b)
    still leave a fully correct, race-free checkpointed result: every
    (task_id, seed) pair recorded exactly once, no lost or duplicated
    rows despite concurrent completions writing to the same result file."""
    r2 = tmp_path / "repo2"
    r2.mkdir()
    _git(r2, "init", "-q")
    _git(r2, "config", "user.email", "t@example.com")
    _git(r2, "config", "user.name", "t")
    (r2 / "mul.py").write_text(
        "def mul(a, b):\n    return a + b  # bug\n", encoding="utf-8"
    )
    _commit(r2, "initial multiplier")
    (r2 / "mul.py").write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
    (r2 / "test_mul.py").write_text(
        "from mul import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
        encoding="utf-8",
    )
    _commit(r2, "fix mul() to multiply instead of add")

    ws = tmp_path / "ws_parallel"
    ws.mkdir()
    admitted1, _ = evolve.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench1", log=lambda *a, **k: None,
    )
    admitted2, _ = evolve.mine_corpus(
        str(r2), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench2", log=lambda *a, **k: None,
    )
    assert len(admitted1) == 1 and len(admitted2) == 1
    tasks_dir = corp.tasks_dir(str(ws))
    for t in (admitted1[0], admitted2[0]):
        t.split = "val"
        t.save(tasks_dir)

    import time as _time

    def slow_fake_run_harness(worktree, problem, *, profile="", timeout=0, extra_env=None):
        _time.sleep(0.3)  # each "task" takes 0.3s of wall time
        calc = Path(worktree) / "calc.py"
        mul = Path(worktree) / "mul.py"
        if calc.exists():
            calc.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        if mul.exists():
            mul.write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
        return {"status": "complete", "tokens": {"total": 10}}

    monkeypatch.setattr(run_mod, "_run_harness", slow_fake_run_harness)

    # Measure serial (workers=1) first, as the fair per-environment baseline
    # — real per-task overhead (git worktree add/remove etc.) varies by
    # machine/CI, so comparing against a hardcoded wall-clock threshold
    # would be flaky. Two independent bench_ids/bench_homes so neither run
    # sees the other's checkpoint.
    t0 = _time.time()
    serial_run = evolve.run_bench(
        str(ws), "val", bench_home=tmp_path / "bench_serial", workers=1,
        log=lambda *a, **k: None,
    )
    serial_elapsed = _time.time() - t0
    assert serial_run.summary["tasks"] == 2

    t0 = _time.time()
    run = evolve.run_bench(
        str(ws), "val", bench_home=tmp_path / "bench_run", workers=2,
        log=lambda *a, **k: None,
    )
    elapsed = _time.time() - t0

    # workers=2 running two independent tasks concurrently must take
    # meaningfully less wall time than running them one at a time —
    # generous margin (25% faster) for scheduling/test-env jitter while
    # still failing if the "parallel" path is secretly serial.
    assert elapsed < serial_elapsed * 0.75, (
        f"workers=2 ({elapsed:.2f}s) was not meaningfully faster than "
        f"workers=1 ({serial_elapsed:.2f}s) — parallel path may be serial"
    )

    assert run.summary["tasks"] == 2
    assert run.summary["resolved"] == 2
    saved = ws / ".wells" / "bench" / "results" / f"{run.bench_id}.json"
    reloaded = run_mod.BenchRun.load(saved)
    recorded_ids = sorted(r.task_id for r in reloaded.tasks)
    assert recorded_ids == sorted([admitted1[0].task_id, admitted2[0].task_id])
    assert len(reloaded.tasks) == 2  # no duplicate/lost rows from the race


# ---------------------------------------------------------------------------
# Token recovery on timeout/crash (the child never printed its own JSON)
# ---------------------------------------------------------------------------


def test_recover_tokens_fills_payload_from_sink_and_cleans_up(tmp_path: Path):
    sink = tmp_path / "sometask-s1.tokens.json"
    sink.write_text(
        json.dumps({"input": 1000, "output": 200, "calls": 3, "cache_read": 400}),
        encoding="utf-8",
    )
    payload = {"status": "timeout", "error": "harness run exceeded 60s"}
    run_mod._recover_tokens(payload, sink)
    assert payload["tokens"] == {
        "input": 1000, "output": 200, "total": 1200, "calls": 3, "cache_read": 400,
    }
    assert payload["tokens_recovered"] is True
    assert not sink.exists()  # cleaned up after reading


def test_recover_tokens_missing_sink_is_a_silent_noop(tmp_path: Path):
    payload = {"status": "timeout", "error": "harness run exceeded 60s"}
    run_mod._recover_tokens(payload, tmp_path / "never-written.tokens.json")
    assert "tokens" not in payload


def test_token_ledger_mirrors_to_sink_file(tmp_path: Path, monkeypatch):
    from wells.tokens import TokenLedger

    sink = tmp_path / "ledger.tokens.json"
    ledger = TokenLedger()
    ledger.set_sink(sink)
    ledger.record(
        step="coder", task_type="code", model="zai:glm-5.2",
        input_tokens=500, output_tokens=100, cache_read_tokens=50,
    )
    on_disk = json.loads(sink.read_text(encoding="utf-8"))
    assert on_disk["input"] == 500
    assert on_disk["output"] == 100
    assert on_disk["calls"] == 1
    # A second call updates the mirrored file in place (running totals).
    ledger.record(
        step="tester", task_type="test", model="zai:glm-5.2",
        input_tokens=200, output_tokens=50,
    )
    on_disk2 = json.loads(sink.read_text(encoding="utf-8"))
    assert on_disk2["input"] == 700
    assert on_disk2["calls"] == 2


def test_wilson_lower_bound_math():
    assert evolve.wilson_lower_bound(0, 0) == 0.0
    assert evolve.wilson_lower_bound(0, 10) == 0.0
    lb_all = evolve.wilson_lower_bound(10, 10)
    assert 0.69 < lb_all < 1.0  # 10/10 is good but small-n uncertainty remains
    # Small sample: LB must sit meaningfully below the point estimate.
    assert evolve.wilson_lower_bound(6, 8) < 6 / 8
    # Perfect run with more evidence -> tighter (higher) lower bound.
    assert evolve.wilson_lower_bound(50, 50) > lb_all


def test_summarize_aggregates():
    rows = [
        run_mod.TaskResult(
            task_id="t1",
            seed=1,
            resolved=True,
            tokens_total=100,
            cost_usd=0.5,
            duration_seconds=10.0,
            harness_status="complete",
        ),
        run_mod.TaskResult(
            task_id="t1",
            seed=2,
            resolved=False,
            tokens_total=200,
            cost_usd=0.5,
            duration_seconds=20.0,
            harness_status="incomplete",
        ),
        run_mod.TaskResult(
            task_id="t2",
            seed=1,
            resolved=True,
            tokens_total=300,
            cost_usd=1.0,
            duration_seconds=30.0,
            harness_status="complete",
        ),
    ]
    s = evolve.summarize(rows)
    assert s["total_runs"] == 3 and s["resolved"] == 2
    assert s["pass_at_1"] == round(2 / 3, 4)
    assert s["any_seed_resolved"] == 2
    assert s["avg_tokens"] == 200
    assert s["total_cost_usd"] == 2.0
    assert s["harness_statuses"] == {"complete": 2, "incomplete": 1}


# ---------------------------------------------------------------------------
# Portable repos: repo_url, resolution on another machine, re-verification
# ---------------------------------------------------------------------------


def _quiet():
    return lambda *a, **k: None


def test_task_spec_roundtrip_keeps_repo_url(tmp_path: Path):
    task = _spec("T-url000000000000000000000000000")
    task.repo_url = "https://github.com/pallets/click"
    path = task.save(tmp_path)
    assert TaskSpec.load(path).repo_url == "https://github.com/pallets/click"
    # Bundles written before the field existed still load.
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["repo_url"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert TaskSpec.load(path).repo_url == ""


def test_normalize_repo_url_equates_ssh_and_https():
    assert corp.normalize_repo_url("https://github.com/o/r.git") == "github.com/o/r"
    assert corp.normalize_repo_url("git@github.com:o/r") == "github.com/o/r"
    assert corp.normalize_repo_url("HTTPS://GitHub.com/O/R/") == "github.com/o/r"
    assert corp.normalize_repo_url("") == ""


def test_resolve_prefers_a_repo_root_that_exists(repo_with_fix: Path):
    t = _spec("T-r00000000000000000000000000000")
    t.repo_root = str(repo_with_fix)
    t.repo_url = "https://example.invalid/never-cloned"
    assert corp.resolve_repo_root(t) == str(repo_with_fix)


def test_resolve_uses_workspace_when_it_is_a_clone_of_the_task_repo(
    repo_with_fix: Path, monkeypatch
):
    """No second copy of a repo the user is already sitting in."""
    _git(repo_with_fix, "remote", "add", "origin", "https://github.com/o/r.git")
    monkeypatch.setattr(
        corp, "_clone_mirror", lambda url, dest: pytest.fail("must not clone")
    )
    t = _spec("T-ws0000000000000000000000000000")
    t.repo_root = "/gone/on/this/machine"
    t.repo_url = "git@github.com:o/r"  # ssh spelling of the same repository
    assert corp.resolve_repo_root(t, workspace=str(repo_with_fix)) == str(repo_with_fix)


def test_resolve_clones_a_mirror_once(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    def fake_clone(url: str, dest: Path):
        calls.append(url)
        dest.mkdir(parents=True)
        _git(dest, "init", "-q")
        return True, ""

    monkeypatch.setattr(corp, "_clone_mirror", fake_clone)
    home = tmp_path / "bench"
    t = _spec("T-m00000000000000000000000000000")
    t.repo_root = "/elsewhere/click"
    t.repo_url = "https://github.com/pallets/click"
    first = corp.resolve_repo_root(t, bench_home=home)
    second = corp.resolve_repo_root(t, bench_home=home)
    assert first == second == str(home / "repos" / "click")
    assert calls == ["https://github.com/pallets/click"]  # once, then reused

    calls.clear()  # read-only reporting never clones
    t2 = _spec("T-n00000000000000000000000000000")
    t2.repo_url = "https://github.com/psf/requests"
    assert corp.resolve_repo_root(t2, bench_home=home, clone=False) is None
    assert calls == []


def test_resolve_without_path_or_url_is_none():
    t = _spec("T-z00000000000000000000000000000")
    t.repo_root = "/not/here"
    assert corp.resolve_repo_root(t) is None


def test_mining_records_repo_url(repo_with_fix: Path, tmp_path: Path):
    _git(repo_with_fix, "remote", "add", "origin", "https://github.com/o/r.git")
    ws = tmp_path / "ws"
    ws.mkdir()
    admitted, _ = evolve.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench", log=_quiet(),
    )
    assert admitted[0].repo_url == "https://github.com/o/r.git"


def test_mining_skips_a_task_whose_commit_sha_changed(
    repo_with_fix: Path, tmp_path: Path
):
    """After a history rewrite every SHA is new, but the task is not."""
    ws = tmp_path / "ws"
    ws.mkdir()
    admitted, _ = evolve.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench", log=_quiet(),
    )
    # The rewrite in miniature: same statement and test files, an id the
    # miner cannot recognise.
    task = admitted[0]
    tdir = corp.tasks_dir(str(ws))
    (tdir / f"{task.task_id}.json").unlink()
    task.task_id = "T-000000000000-rewritten-copy-of-the-same-fix"
    task.save(tdir)

    again, stats = evolve.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench", log=_quiet(),
    )
    assert again == [] and stats["skipped_existing"] == 1
    assert [t.task_id for t in evolve.load_corpus(str(ws))] == [task.task_id]


def test_run_bench_resolves_a_repo_that_is_not_at_its_recorded_path(
    repo_with_fix: Path, tmp_path: Path, monkeypatch
):
    """The corpus was committed on another machine: repo_root is gone, but
    the repo is reachable by URL (a local file:// stand-in for GitHub)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    admitted, _ = evolve.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench_mine", log=_quiet(),
    )
    task = admitted[0]
    task.split = "val"
    task.save(corp.tasks_dir(str(ws)))

    moved = tmp_path / "elsewhere" / "r"
    moved.parent.mkdir()
    _git(tmp_path, "clone", "-q", str(repo_with_fix), str(moved))
    task.repo_root = "/no/such/path/on/this/machine"
    task.repo_url = moved.as_uri()
    task.save(corp.tasks_dir(str(ws)))

    def fake_harness(worktree, problem, *, profile="", timeout=0, extra_env=None):
        Path(worktree, "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        return {"status": "complete", "tokens": {"total": 1}}

    monkeypatch.setattr(run_mod, "_run_harness", fake_harness)
    run = evolve.run_bench(
        str(ws), "val", bench_home=tmp_path / "bench_run", log=_quiet()
    )
    assert run.summary["resolved"] == 1


def test_verify_task_reports_validity_on_this_machine(
    repo_with_fix: Path, tmp_path: Path
):
    ws = tmp_path / "ws"
    ws.mkdir()
    admitted, _ = evolve.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench_mine", log=_quiet(),
    )
    task = admitted[0]
    task.status = "unverified"
    task.save(corp.tasks_dir(str(ws)))
    home = tmp_path / "bench_verify"
    real_base = task.base_commit

    ok, why = corp.verify_task(str(ws), task, bench_home=home)
    assert ok and why == "" and task.status == "verified"
    assert corp.local_verify_failures(str(ws)) == {}

    # Tamper: base == fix, so the target tests pass at "base". Not a fix
    # here — but that is this machine's finding, not a fact about the task:
    # the bundle keeps the status it was verified with elsewhere, and the
    # failure is remembered locally instead.
    task.base_commit = task.source_commit
    ok, why = corp.verify_task(str(ws), task, bench_home=home)
    assert not ok and "already PASS at base" in why
    assert evolve.load_corpus(str(ws))[0].status == "verified"
    assert corp.local_verify_failures(str(ws)) == {task.task_id: why}

    # Repair it and a later verification clears the local record.
    task.base_commit = real_base
    ok, _ = corp.verify_task(str(ws), task, bench_home=home)
    assert ok and corp.local_verify_failures(str(ws)) == {}


def test_verify_task_explains_an_unobtainable_repo(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    t = _spec("T-u00000000000000000000000000000")
    t.repo_root = "/only/on/the/machine/that/mined/it"
    t.save(corp.tasks_dir(str(ws)))
    ok, why = corp.verify_task(str(ws), t, bench_home=tmp_path / "bench")
    assert not ok
    assert "not obtainable" in why and "/only/on/the/machine" in why
    assert evolve.load_corpus(str(ws))[0].status == "verified"  # bundle untouched
    assert corp.local_verify_failures(str(ws)) == {t.task_id: why}


def test_run_bench_leaves_out_tasks_that_failed_verification_here(
    repo_with_fix: Path, tmp_path: Path, monkeypatch
):
    """An oracle that cannot pass on this machine would score the machine,
    not the harness, so such tasks are dropped from the run by default."""
    ws = tmp_path / "ws"
    ws.mkdir()
    admitted, _ = evolve.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench_mine", log=_quiet(),
    )
    task = admitted[0]
    task.split = "val"
    task.save(corp.tasks_dir(str(ws)))
    corp._record_local_verify(str(ws), task.task_id, False, "needs a plugin")

    def fake_harness(worktree, problem, *, profile="", timeout=0, extra_env=None):
        Path(worktree, "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        return {"status": "complete", "tokens": {"total": 1}}

    monkeypatch.setattr(run_mod, "_run_harness", fake_harness)
    logs: list[str] = []
    log = logs.append
    with pytest.raises(RuntimeError, match="failed validation on this machine"):
        evolve.run_bench(str(ws), "val", bench_home=tmp_path / "bench_run", log=log)
    assert any("leaving out 1 task(s)" in line for line in logs)

    run = evolve.run_bench(
        str(ws), "val", bench_home=tmp_path / "bench_run",
        include_unverified=True, log=log,
    )
    assert run.summary["resolved"] == 1


def test_source_env_drops_the_launching_shells_proxy_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("https_proxy", "http://proxy:3128")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy:1080")
    monkeypatch.setenv("SOME_OTHER", "kept")
    env = corp.source_env(str(tmp_path))
    assert not {k for k in env if k.lower().endswith("_proxy")}
    assert env["SOME_OTHER"] == "kept"
    assert env["PYTHONPATH"].startswith(str(tmp_path))
