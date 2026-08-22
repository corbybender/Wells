"""Tests for the evolution engine: mutation manifests, propose/gate/
promote/reject over a candidate AGENT.md. All bench passes are stubbed —
no LLM calls, mirroring tests/test_evolve.py's approach."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wells import principles
from wells.evolve import corpus as corp
from wells.evolve import mutate
from wells.evolve import runner as run_mod


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


@pytest.fixture(autouse=True)
def _isolate_mutations_dir(tmp_path, monkeypatch):
    """Point the module-level MUTATIONS_DIR at a tmp dir so tests never
    touch the real ~/.wells/evolve/mutations/."""
    monkeypatch.setattr(mutate, "MUTATIONS_DIR", tmp_path / "mutations")
    yield


# ---------------------------------------------------------------------------
# Manifest persistence
# ---------------------------------------------------------------------------


def test_manifest_roundtrip(tmp_path: Path):
    m = mutate.MutationManifest(
        mutation_id="M-1",
        workspace=str(tmp_path),
        created_at="now",
        rationale="test",
        candidate_path=str(tmp_path / "AGENT.md"),
        baseline_source="bundled (default)",
    )
    path = mutate.save_manifest(m)
    assert path.is_file()
    loaded = mutate.load_manifest("M-1")
    assert loaded == m


def test_load_manifest_missing_returns_none():
    assert mutate.load_manifest("nope") is None


def test_list_manifests_sorted_newest_first(tmp_path: Path):
    import time

    for i in range(3):
        m = mutate.MutationManifest(
            mutation_id=f"M-{i}",
            workspace=str(tmp_path),
            created_at="now",
            rationale="test",
            candidate_path=str(tmp_path / "AGENT.md"),
            baseline_source="bundled (default)",
        )
        mutate.save_manifest(m)
        time.sleep(0.01)
    ids = [m.mutation_id for m in mutate.list_manifests()]
    assert ids == ["M-2", "M-1", "M-0"]


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------


def test_propose_mutation_from_file(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    candidate = tmp_path / "candidate.md"
    candidate.write_text("# New rules\n\nBe concise.\n", encoding="utf-8")

    m = mutate.propose_mutation(str(ws), "tighten conciseness", candidate_file=str(candidate))
    assert m.status == "pending"
    assert m.rationale == "tighten conciseness"
    assert Path(m.candidate_path).read_text(encoding="utf-8") == "# New rules\n\nBe concise.\n"
    assert mutate.load_manifest(m.mutation_id) == m


def test_propose_mutation_requires_file_or_auto(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError, match="candidate_file=PATH or auto=True"):
        mutate.propose_mutation(str(ws), "no source given")


def test_propose_mutation_rejects_both_file_and_auto(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    candidate = tmp_path / "candidate.md"
    candidate.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not both"):
        mutate.propose_mutation(str(ws), "r", candidate_file=str(candidate), auto=True)


def test_propose_mutation_auto_uses_stubbed_harness(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()

    def fake_run_harness(worktree, problem, *, profile="", timeout=0, extra_env=None):
        (Path(worktree) / "AGENT.md").write_text("# Drafted\n", encoding="utf-8")
        return {"status": "complete", "tokens": {"total": 10}}

    monkeypatch.setattr(run_mod, "_run_harness", fake_run_harness)
    m = mutate.propose_mutation(str(ws), "auto-draft", auto=True)
    assert Path(m.candidate_path).read_text(encoding="utf-8") == "# Drafted\n"


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _fake_harness_reading_env(worktree_: str, problem: str, *, profile="", timeout=0, extra_env=None):
    """Stub _run_harness whose 'quality' depends on the *content* of the
    WELLS_PRINCIPLES file it was pointed at — lets a test assert the
    baseline vs. candidate passes actually used different principles
    files, without hardcoding path shape (mutations dir is monkeypatched
    per-test)."""
    src = Path(worktree_) / "calc.py"
    principles_path = (extra_env or {}).get("WELLS_PRINCIPLES", "")
    text = Path(principles_path).read_text(encoding="utf-8") if principles_path else ""
    fixes_bug = "candidate principles" in text
    if fixes_bug and src.exists():
        src.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return {"status": "complete", "tokens": {"total": 10}}


def test_gate_mutation_recommends_promote_when_candidate_resolves_more(
    repo_with_fix: Path, tmp_path: Path, monkeypatch
):
    ws = tmp_path / "ws"
    ws.mkdir()
    admitted, _ = corp.mine_corpus(
        str(repo_with_fix), str(ws), max_tasks=5,
        bench_home=tmp_path / "bench", log=lambda *a, **k: None,
    )
    assert len(admitted) == 1
    split = admitted[0].split

    candidate = tmp_path / "candidate.md"
    candidate.write_text("# candidate principles\n", encoding="utf-8")
    manifest = mutate.propose_mutation(str(ws), "test", candidate_file=str(candidate))

    monkeypatch.setattr(run_mod, "_run_harness", _fake_harness_reading_env)
    gated = mutate.gate_mutation(
        manifest.mutation_id, str(ws), split=split,
        bench_home=tmp_path / "bench", log=lambda *a, **k: None,
    )

    assert gated.status == "gated"
    assert gated.baseline_bench["resolved"] == 0
    assert gated.candidate_bench["resolved"] == 1
    assert gated.recommendation == "promote"
    assert gated.replay_summary is not None


def test_gate_mutation_missing_manifest_raises(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(RuntimeError, match="No such mutation"):
        mutate.gate_mutation("nope", str(ws))


def test_gate_mutation_empty_split_raises_loud(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    candidate = tmp_path / "candidate.md"
    candidate.write_text("x", encoding="utf-8")
    manifest = mutate.propose_mutation(str(ws), "test", candidate_file=str(candidate))
    with pytest.raises(RuntimeError, match="wells bench mine"):
        mutate.gate_mutation(manifest.mutation_id, str(ws), split="val")


# ---------------------------------------------------------------------------
# Promote / reject
# ---------------------------------------------------------------------------


def test_promote_requires_gated_status(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    candidate = tmp_path / "candidate.md"
    candidate.write_text("x", encoding="utf-8")
    m = mutate.propose_mutation(str(ws), "test", candidate_file=str(candidate))
    ok, msg = mutate.promote_mutation(m.mutation_id, str(ws))
    assert ok is False
    assert "has not been gated" in msg


def test_promote_writes_agent_md_and_clears_cache(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    candidate = tmp_path / "candidate.md"
    candidate.write_text("# promoted rules\n", encoding="utf-8")
    m = mutate.propose_mutation(str(ws), "test", candidate_file=str(candidate))
    # Force it into a gated+promotable state without a real bench run.
    m.status = "gated"
    m.recommendation = "promote"
    mutate.save_manifest(m)

    cleared = []
    monkeypatch.setattr(principles, "clear_cache", lambda: cleared.append(True))

    ok, msg = mutate.promote_mutation(m.mutation_id, str(ws))
    assert ok is True
    assert (ws / "AGENT.md").read_text(encoding="utf-8") == "# promoted rules\n"
    assert cleared == [True]
    reloaded = mutate.load_manifest(m.mutation_id)
    assert reloaded.status == "promoted"
    assert reloaded.promoted_at


def test_promote_refuses_reject_recommendation_without_force(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    candidate = tmp_path / "candidate.md"
    candidate.write_text("x", encoding="utf-8")
    m = mutate.propose_mutation(str(ws), "test", candidate_file=str(candidate))
    m.status = "gated"
    m.recommendation = "reject"
    mutate.save_manifest(m)

    ok, msg = mutate.promote_mutation(m.mutation_id, str(ws))
    assert ok is False
    assert "recommends" in msg

    ok, msg = mutate.promote_mutation(m.mutation_id, str(ws), force=True)
    assert ok is True


def test_promote_missing_mutation(tmp_path: Path):
    ok, msg = mutate.promote_mutation("nope", str(tmp_path))
    assert ok is False
    assert "No such mutation" in msg


def test_reject_mutation(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    candidate = tmp_path / "candidate.md"
    candidate.write_text("x", encoding="utf-8")
    m = mutate.propose_mutation(str(ws), "test", candidate_file=str(candidate))

    ok, msg = mutate.reject_mutation(m.mutation_id, str(ws))
    assert ok is True
    assert mutate.load_manifest(m.mutation_id).status == "rejected"

    # Already-resolved mutations refuse a second reject.
    ok2, msg2 = mutate.reject_mutation(m.mutation_id, str(ws))
    assert ok2 is False
