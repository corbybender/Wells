"""Tests for the parallel worktree fleet: spawn, run (subprocess-isolated),
compare, pick/drop cleanup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from wells import fleet


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _isolated_fleet_dirs(tmp_path: Path, monkeypatch):
    """Worktree checkouts and manifests both live under FLEET_DIR — keep
    every test confined to tmp_path instead of touching the real ~/.wells/fleet."""
    fleet_dir = tmp_path / "wells-fleet-home"
    monkeypatch.setattr(fleet, "FLEET_DIR", fleet_dir)
    monkeypatch.setattr(fleet, "FLEET_WORKTREES_DIR", fleet_dir / "worktrees")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "initial")
    _git(r, "branch", "-M", "main")
    return r


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------


def test_is_git_repo(repo: Path, tmp_path: Path):
    assert fleet.is_git_repo(str(repo)) is True
    non_repo = tmp_path / "notrepo"
    non_repo.mkdir()
    assert fleet.is_git_repo(str(non_repo)) is False


def test_current_branch(repo: Path):
    assert fleet.current_branch(str(repo)) == "main"


# ---------------------------------------------------------------------------
# spawn_worktrees
# ---------------------------------------------------------------------------


def test_spawn_worktrees_creates_n_isolated_dirs(repo: Path):
    manifest = fleet.spawn_worktrees(str(repo), "fx1", "fix the bug", 3)
    assert len(manifest.members) == 3
    for m in manifest.members:
        assert Path(m.worktree_path).is_dir()
        assert (Path(m.worktree_path) / "a.txt").read_text() == "hello\n"
        assert fleet.current_branch(m.worktree_path) == m.branch
    # Each on its own branch.
    assert len({m.branch for m in manifest.members}) == 3
    fleet._cleanup_worktrees(manifest)


def test_spawn_worktrees_rejects_non_git_dir(tmp_path: Path):
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(RuntimeError, match="not a git repository"):
        fleet.spawn_worktrees(str(d), "fx2", "task", 2)


def test_spawn_worktrees_assigns_profiles(repo: Path):
    manifest = fleet.spawn_worktrees(
        str(repo), "fx3", "task", 2, profiles=["zai", "openai"]
    )
    assert manifest.members[0].profile == "zai"
    assert manifest.members[1].profile == "openai"
    fleet._cleanup_worktrees(manifest)


def test_spawn_worktrees_rolls_back_on_partial_failure(repo: Path):
    """If worktree i fails, worktrees 0..i-1 must be removed too — no
    half-spawned fleet left on disk."""
    real_git = fleet._git
    calls = {"n": 0}

    def _flaky(cwd, *args, **kw):
        if args[:2] == ("worktree", "add"):
            calls["n"] += 1
            if calls["n"] == 2:
                return False, "simulated failure"
        return real_git(cwd, *args, **kw)

    with patch.object(fleet, "_git", side_effect=_flaky):
        with pytest.raises(RuntimeError):
            fleet.spawn_worktrees(str(repo), "fx4", "task", 3)

    # No leftover fleet worktree directories.
    fleet_root = fleet.FLEET_WORKTREES_DIR / "fx4"
    assert not fleet_root.exists() or not any(fleet_root.iterdir())
    ok, out = real_git(str(repo), "worktree", "list")
    assert "fx4" not in out


def test_spawn_worktrees_never_nested_inside_the_repo(repo: Path):
    """Regression guard: a worktree path inside the tracked repo tree
    pollutes `git status` in the main worktree and risks a broad `git add`
    there staging sibling worktrees' files into a real commit."""
    manifest = fleet.spawn_worktrees(str(repo), "fx11", "task", 1)
    wt_path = Path(manifest.members[0].worktree_path).resolve()
    assert repo.resolve() not in wt_path.parents
    fleet._cleanup_worktrees(manifest)


# ---------------------------------------------------------------------------
# Manifest persistence
# ---------------------------------------------------------------------------


def test_save_and_load_manifest_roundtrip(repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DIR", tmp_path / "fleetdir")
    manifest = fleet.spawn_worktrees(str(repo), "fx5", "task", 2)
    manifest.members[0].status = "complete"
    manifest.members[0].tokens_total = 1234
    fleet.save_manifest(manifest)

    loaded = fleet.load_manifest("fx5")
    assert loaded is not None
    assert loaded.fleet_id == "fx5"
    assert loaded.members[0].status == "complete"
    assert loaded.members[0].tokens_total == 1234
    fleet._cleanup_worktrees(manifest)


def test_load_manifest_missing_returns_none(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DIR", tmp_path / "fleetdir")
    assert fleet.load_manifest("does-not-exist") is None


def test_list_manifests_newest_first(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DIR", tmp_path / "fleetdir")
    import time as _t
    for i in range(3):
        m = fleet.FleetManifest(
            fleet_id=f"id{i}", repo_root="r", base_branch="main", task="t",
            created_at="now",
        )
        fleet.save_manifest(m)
        _t.sleep(0.01)
    names = [m.fleet_id for m in fleet.list_manifests()]
    assert names == ["id2", "id1", "id0"]


# ---------------------------------------------------------------------------
# _run_member (subprocess isolation — no real LLM/subprocess.run mocked)
# ---------------------------------------------------------------------------


def _fake_subprocess_result(payload: dict, returncode: int = 0) -> "subprocess.CompletedProcess":
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=json.dumps(payload) + "\n", stderr="",
    )


def test_run_member_parses_json_result(repo: Path):
    manifest = fleet.spawn_worktrees(str(repo), "fx6", "task", 1)
    member = manifest.members[0]
    payload = {
        "status": "complete", "summary": "did it", "git_summary": "1 file changed",
        "tokens": {"total": 500}, "cost_usd": 0.01,
    }
    with patch.object(fleet.subprocess, "run", return_value=_fake_subprocess_result(payload)):
        fleet._run_member(manifest, member)
    assert member.status == "complete"
    assert member.summary == "did it"
    assert member.tokens_total == 500
    assert member.cost_usd == 0.01
    fleet._cleanup_worktrees(manifest)


def test_run_member_passes_profile_env_to_subprocess(repo: Path):
    manifest = fleet.spawn_worktrees(str(repo), "fx7", "task", 1, profiles=["openrouter"])
    member = manifest.members[0]
    captured = {}

    def _fake_run(cmd, capture_output, text, env, timeout):
        captured["env"] = env
        captured["cmd"] = cmd
        return _fake_subprocess_result({"status": "complete", "summary": "x"})

    with patch.object(fleet.subprocess, "run", side_effect=_fake_run):
        fleet._run_member(manifest, member)
    assert captured["env"]["MODEL_PROFILE"] == "openrouter"
    assert "--workspace" in captured["cmd"]
    assert member.worktree_path in captured["cmd"]
    fleet._cleanup_worktrees(manifest)


def test_run_member_no_json_output_is_reported_as_error(repo: Path):
    manifest = fleet.spawn_worktrees(str(repo), "fx8", "task", 1)
    member = manifest.members[0]
    bad = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    with patch.object(fleet.subprocess, "run", return_value=bad):
        fleet._run_member(manifest, member)
    assert member.status == "error"
    assert "boom" in member.error or "no JSON" in member.error
    fleet._cleanup_worktrees(manifest)


def test_run_member_timeout_reported_as_error(repo: Path):
    manifest = fleet.spawn_worktrees(str(repo), "fx9", "task", 1)
    member = manifest.members[0]
    with patch.object(
        fleet.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="wells", timeout=1),
    ):
        fleet._run_member(manifest, member)
    assert member.status == "error"
    assert "timed out" in member.error
    fleet._cleanup_worktrees(manifest)


def test_run_member_does_not_mutate_shared_config_globals(repo: Path):
    """The whole point of subprocess isolation: a member's profile must
    never touch this process's config.ACTIVE_PROFILE."""
    from wells import config
    original = config.ACTIVE_PROFILE
    manifest = fleet.spawn_worktrees(str(repo), "fx10", "task", 1, profiles=["some-other-profile"])
    member = manifest.members[0]
    with patch.object(
        fleet.subprocess, "run",
        return_value=_fake_subprocess_result({"status": "complete", "summary": "x"}),
    ):
        fleet._run_member(manifest, member)
    assert config.ACTIVE_PROFILE == original
    fleet._cleanup_worktrees(manifest)


# ---------------------------------------------------------------------------
# run_fleet: end-to-end spawn + concurrent run (subprocess mocked)
# ---------------------------------------------------------------------------


def test_run_fleet_end_to_end(repo: Path, tmp_path: Path, monkeypatch):
    """spawn_worktrees's own git calls go through fleet._git (real
    subprocess); only the member-run subprocess (identified by invoking
    sys.executable, not `git`) is faked — a blanket subprocess.run patch
    would also break the git plumbing spawn_worktrees needs."""
    import sys as _sys
    monkeypatch.setattr(fleet, "FLEET_DIR", tmp_path / "fleetdir")
    real_run = subprocess.run

    def _fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == _sys.executable:
            return _fake_subprocess_result({
                "status": "complete", "summary": "done", "tokens": {"total": 100},
            })
        return real_run(cmd, *a, **kw)

    with patch.object(fleet.subprocess, "run", side_effect=_fake_run):
        manifest = fleet.run_fleet(str(repo), "add a feature", 2)

    assert len(manifest.members) == 2
    assert all(m.status == "complete" for m in manifest.members)
    reloaded = fleet.load_manifest(manifest.fleet_id)
    assert reloaded is not None and len(reloaded.members) == 2
    fleet._cleanup_worktrees(manifest)


# ---------------------------------------------------------------------------
# pick_winner / drop_fleet
# ---------------------------------------------------------------------------


def test_pick_winner_merges_and_cleans_up(repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DIR", tmp_path / "fleetdir")
    manifest = fleet.spawn_worktrees(str(repo), "fxpick", "task", 2)
    fleet.save_manifest(manifest)

    winner = manifest.members[0]
    (Path(winner.worktree_path) / "winner.txt").write_text("I won\n", encoding="utf-8")
    _git(winner.worktree_path, "add", "winner.txt")
    _git(winner.worktree_path, "commit", "-q", "-m", "winning change")

    ok, msg = fleet.pick_winner("fxpick", 0)
    assert ok, msg
    assert (repo / "winner.txt").read_text() == "I won\n"  # merged into base

    # Cleanup: no worktrees, no fleet branches left.
    out = _git(str(repo), "worktree", "list").stdout
    assert "fxpick" not in out
    branches = _git(str(repo), "branch", "--list").stdout
    assert "fxpick" not in branches

    reloaded = fleet.load_manifest("fxpick")
    assert reloaded.resolved is True
    assert reloaded.winner == 0


def test_pick_winner_unknown_fleet(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DIR", tmp_path / "fleetdir")
    ok, msg = fleet.pick_winner("nope", 0)
    assert ok is False


def test_pick_winner_already_resolved_refuses(repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DIR", tmp_path / "fleetdir")
    manifest = fleet.spawn_worktrees(str(repo), "fxres", "task", 1)
    manifest.resolved = True
    manifest.winner = 0
    fleet.save_manifest(manifest)
    ok, msg = fleet.pick_winner("fxres", 0)
    assert ok is False
    assert "already resolved" in msg
    fleet._cleanup_worktrees(manifest)


def test_drop_fleet_removes_everything_without_merging(repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fleet, "FLEET_DIR", tmp_path / "fleetdir")
    manifest = fleet.spawn_worktrees(str(repo), "fxdrop", "task", 2)
    fleet.save_manifest(manifest)

    ok, msg = fleet.drop_fleet("fxdrop")
    assert ok, msg
    out = _git(str(repo), "worktree", "list").stdout
    assert "fxdrop" not in out
    # main branch untouched — nothing was merged.
    assert fleet.current_branch(str(repo)) == "main"
    reloaded = fleet.load_manifest("fxdrop")
    assert reloaded.resolved is True and reloaded.winner is None
