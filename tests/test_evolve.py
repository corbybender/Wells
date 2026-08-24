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


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


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
