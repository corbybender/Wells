"""Tests for the autonomous propose->gate->promote/reject loop. This is
the highest-consequence part of the evolve subsystem (unattended commits,
optionally unattended pushes to origin) so these tests are deliberately
thorough about the bounding, resume, and promote/push-gating behavior —
not just the happy path.

All git operations are stubbed (monkeypatched _git_commit_and_maybe_push
or the underlying wells._gitutils.git) — no test here ever touches a real
remote."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from wells.evolve import autoloop, mutate


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    """Point autoloop's state/report files and mutate's manifest store at
    tmp dirs so tests never touch ~/.wells/evolve/ for real."""
    state_dir = tmp_path / "autoloop_state"
    monkeypatch.setattr(autoloop, "STATE_DIR", state_dir)
    monkeypatch.setattr(autoloop, "STATE_PATH", state_dir / "autoloop_state.json")
    monkeypatch.setattr(autoloop, "REPORT_PATH", state_dir / "autoloop_report.md")
    monkeypatch.setattr(mutate, "MUTATIONS_DIR", tmp_path / "mutations")
    yield


def _fake_propose(workspace, rationale, *, auto=False, profile="", timeout=0,
                   candidate_file="", heartbeat_path=None):
    candidate_path = Path(workspace) / f"candidate-{len(mutate.list_manifests())}.md"
    candidate_path.write_text("# fake candidate AGENT.md\n", encoding="utf-8")
    m = mutate.MutationManifest(
        mutation_id=f"auto-{len(mutate.list_manifests())}",
        workspace=workspace, created_at="now", rationale=rationale,
        candidate_path=str(candidate_path),
        baseline_source="bundled (default)",
    )
    mutate.save_manifest(m)
    return m


def _fake_gate_factory(recommendation: str):
    def _fake_gate(mutation_id, workspace, *, split="val", profile="", seeds=1,
                    timeout=0, task_filter="", bench_home=None, resume=False,
                    heartbeat_path=None, workers=1, log=print):
        m = mutate.load_manifest(mutation_id)
        m.status = "gated"
        m.baseline_bench = {"pass_at_1_wilson_lb": 0.5}
        m.candidate_bench = {
            "pass_at_1_wilson_lb": 0.6 if recommendation == "promote" else 0.3
        }
        m.recommendation = recommendation
        mutate.save_manifest(m)
        return m
    return _fake_gate


def test_loop_stops_at_max_cycles(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(mutate, "propose_mutation", _fake_propose)
    monkeypatch.setattr(mutate, "gate_mutation", _fake_gate_factory("reject"))
    monkeypatch.setattr(autoloop, "_git_commit_and_maybe_push", lambda *a, **k: True)

    state = autoloop.run_autonomous_loop(
        str(ws), max_cycles=3, max_days=999, split="val", log=lambda *a, **k: None
    )
    assert state.stopped is True
    assert state.stop_reason == "max_cycles reached"
    assert state.cycle == 3
    assert len(state.history) == 3
    assert all(h.recommendation == "reject" for h in state.history)
    assert all(not h.promoted for h in state.history)


def test_loop_stops_at_deadline_even_with_cycles_remaining(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(mutate, "propose_mutation", _fake_propose)
    monkeypatch.setattr(mutate, "gate_mutation", _fake_gate_factory("reject"))
    monkeypatch.setattr(autoloop, "_git_commit_and_maybe_push", lambda *a, **k: True)

    # max_days so small the deadline is already past after the first cycle.
    state = autoloop.run_autonomous_loop(
        str(ws), max_cycles=1000, max_days=1e-9, split="val", log=lambda *a, **k: None
    )
    assert state.stopped is True
    assert state.stop_reason == "max_days deadline reached"
    assert state.cycle < 1000  # did not run anywhere near max_cycles


def test_loop_promotes_and_pushes_only_on_recommend_promote(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENT.md").write_text("original", encoding="utf-8")
    monkeypatch.setattr(mutate, "propose_mutation", _fake_propose)
    monkeypatch.setattr(mutate, "gate_mutation", _fake_gate_factory("promote"))

    push_calls = []
    monkeypatch.setattr(
        autoloop, "_git_commit_and_maybe_push",
        lambda workspace, message, *, push, log: push_calls.append(push) or True,
    )

    state = autoloop.run_autonomous_loop(
        str(ws), max_cycles=1, max_days=999, split="val", push=True, log=lambda *a, **k: None
    )
    assert state.history[0].promoted is True
    assert push_calls == [True]


def test_loop_reject_never_promotes_or_commits(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(mutate, "propose_mutation", _fake_propose)
    monkeypatch.setattr(mutate, "gate_mutation", _fake_gate_factory("reject"))

    push_calls = []
    monkeypatch.setattr(
        autoloop, "_git_commit_and_maybe_push",
        lambda *a, **k: push_calls.append(1) or True,
    )

    state = autoloop.run_autonomous_loop(
        str(ws), max_cycles=1, max_days=999, split="val", log=lambda *a, **k: None
    )
    assert state.history[0].promoted is False
    assert push_calls == []  # commit/push helper must never be called on reject


def test_push_false_is_threaded_through_to_commit_helper(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(mutate, "propose_mutation", _fake_propose)
    monkeypatch.setattr(mutate, "gate_mutation", _fake_gate_factory("promote"))

    seen_push = []
    monkeypatch.setattr(
        autoloop, "_git_commit_and_maybe_push",
        lambda workspace, message, *, push, log: seen_push.append(push) or True,
    )

    autoloop.run_autonomous_loop(
        str(ws), max_cycles=1, max_days=999, split="val", push=False, log=lambda *a, **k: None
    )
    assert seen_push == [False]


def test_resume_after_simulated_death_mid_gate_does_not_repropose(tmp_path: Path, monkeypatch):
    """The critical fault-tolerance property at the loop level: if the
    process dies with a mutation already proposed but not yet gated,
    calling run_autonomous_loop again must gate THAT mutation, never
    draft a fresh one and abandon the in-flight work."""
    ws = tmp_path / "ws"
    ws.mkdir()

    propose_calls = []

    def counting_propose(*a, **k):
        propose_calls.append(1)
        return _fake_propose(*a, **k)

    monkeypatch.setattr(mutate, "propose_mutation", counting_propose)
    monkeypatch.setattr(mutate, "gate_mutation", _fake_gate_factory("reject"))
    monkeypatch.setattr(autoloop, "_git_commit_and_maybe_push", lambda *a, **k: True)

    # Simulate a death right after propose (before gate ever ran): write
    # loop state with current_mutation_id set, as run_autonomous_loop
    # itself would have, but don't actually run a cycle.
    now = time.time()
    m = _fake_propose(str(ws), "cycle 1")
    state = autoloop.LoopState(
        started_at=now, deadline=now + 999 * 86400, max_cycles=5, cycle=1,
        current_mutation_id=m.mutation_id,
    )
    state.history.append(
        autoloop.CycleRecord(cycle=1, mutation_id=m.mutation_id, rationale="cycle 1",
                              started_at="now")
    )
    autoloop._save_state(state)
    propose_calls.clear()  # the manual propose above shouldn't count

    resumed = autoloop.run_autonomous_loop(
        str(ws), max_cycles=5, max_days=999, split="val", log=lambda *a, **k: None
    )
    # Cycle 1's gate ran (and completed, since our stub always finishes
    # immediately) without a second propose call for cycle 1 — the loop
    # then moved on to draft cycle 2 for real, which IS a legitimate
    # propose call.
    assert resumed.history[0].mutation_id == m.mutation_id
    assert resumed.history[0].recommendation == "reject"
    assert len(propose_calls) == resumed.cycle - 1  # one propose per cycle after the resumed one


def test_recent_mutation_history_summarizes_prior_outcomes(tmp_path: Path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m1 = mutate.propose_mutation(str(ws), "idea A — do X", candidate_file=str(_write(tmp_path, "a")))
    m1.status, m1.recommendation = "rejected", "reject"
    m1.baseline_bench = {"pass_at_1_wilson_lb": 0.5}
    m1.candidate_bench = {"pass_at_1_wilson_lb": 0.3}
    mutate.save_manifest(m1)

    m2 = mutate.propose_mutation(str(ws), "idea B — do Y", candidate_file=str(_write(tmp_path, "b")))
    m2.status, m2.recommendation = "promoted", "promote"
    m2.baseline_bench = {"pass_at_1_wilson_lb": 0.5}
    m2.candidate_bench = {"pass_at_1_wilson_lb": 0.7}
    mutate.save_manifest(m2)

    summary = mutate._recent_mutation_history()
    assert "idea A" in summary and "reject" in summary
    assert "idea B" in summary and "promote" in summary
    assert "30.0%" in summary  # candidate_lb for the rejected one shows up


def _write(tmp_path: Path, name: str) -> Path:
    p = tmp_path / f"candidate_{name}.md"
    p.write_text(f"# candidate {name}\n", encoding="utf-8")
    return p
