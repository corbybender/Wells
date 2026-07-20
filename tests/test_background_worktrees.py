"""Tests for worktree-per-subagent isolation (bg_start role="worktree").

Two layers:
  * :mod:`wells.worktree` primitives — create / commit / cherry-pick / reap,
    exercised directly against a real git repo in tmp_path (no mocks).
  * :mod:`wells.background` integration — ``bg_start role="worktree"`` spawns
    a sub-agent in its own worktree, ``bg_collect`` cherry-picks the result
    back into the parent. The sub-agent's run_subagent is mocked since we
    only need to verify the worktree lifecycle + merge semantics.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from wells import background, tools, worktree
from wells.subagents import SubagentReport, SubagentSpec
from wells.tools import ToolContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _isolated_bg_worktree_dir(tmp_path: Path, monkeypatch):
    """Worktree checkouts live under BG_WORKTREES_DIR (~/.wells/bg-worktrees).
    Keep tests confined to tmp_path so they don't pollute the real home dir."""
    wt_root = tmp_path / "bg-worktrees"
    monkeypatch.setattr(worktree, "BG_WORKTREES_DIR", wt_root)
    # Reset the registry between tests so slots never leak across.
    background.REGISTRY.reset()
    yield
    background.REGISTRY.reset()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one committed file."""
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


@pytest.fixture
def ctx(repo: Path) -> ToolContext:
    return ToolContext(workspace=str(repo), safety="auto")


# ---------------------------------------------------------------------------
# worktree.create_worktree
# ---------------------------------------------------------------------------


def test_create_worktree_off_head(repo: Path):
    h = worktree.create_worktree(str(repo), slot_id="wt-1", task="refactor auth")
    assert Path(h.worktree_path).is_dir()
    # The worktree has the parent's file content.
    assert (Path(h.worktree_path) / "a.txt").read_text() == "hello\n"
    # It's on its own branch.
    assert worktree.current_branch(h.worktree_path) == h.branch
    assert h.branch.startswith("wells-bg/wt-1/")
    # base_sha is the parent HEAD at spawn time.
    assert h.base_sha == worktree.head_sha(str(repo))
    # Cleanup.
    worktree.remove_worktree(str(repo), h)
    assert not Path(h.worktree_path).exists()


def test_create_worktree_rejects_non_git(tmp_path: Path):
    d = tmp_path / "plain"
    d.mkdir()
    with pytest.raises(RuntimeError, match="not a git repository"):
        worktree.create_worktree(str(d), slot_id="wt-x", task="t")


def test_create_worktree_never_nested_inside_repo(repo: Path):
    """A worktree inside the tracked repo tree pollutes git status and risks
    a broad `git add -A` staging sibling admin files."""
    h = worktree.create_worktree(str(repo), slot_id="wt-nest", task="t")
    wt_resolved = Path(h.worktree_path).resolve()
    assert repo.resolve() not in wt_resolved.parents
    worktree.remove_worktree(str(repo), h)


# ---------------------------------------------------------------------------
# worktree.commit_pending + cherry_pick_into_parent
# ---------------------------------------------------------------------------


def test_commit_pending_no_edits_returns_empty_sha(repo: Path):
    h = worktree.create_worktree(str(repo), slot_id="wt-empty", task="t")
    c = worktree.commit_pending(h, message="msg")
    assert c.ok
    assert c.tip_sha == ""
    worktree.remove_worktree(str(repo), h)


def test_commit_pending_creates_one_commit(repo: Path):
    h = worktree.create_worktree(str(repo), slot_id="wt-edit", task="t")
    (Path(h.worktree_path) / "a.txt").write_text("changed\n", encoding="utf-8")
    (Path(h.worktree_path) / "new.txt").write_text("new\n", encoding="utf-8")
    c = worktree.commit_pending(h, message="edit")
    assert c.ok
    assert c.tip_sha and c.tip_sha != h.base_sha
    # Parent untouched.
    assert (repo / "a.txt").read_text() == "hello\n"
    assert not (repo / "new.txt").exists()
    worktree.remove_worktree(str(repo), h)


def test_cherry_pick_clean_merge_applies_to_parent(repo: Path):
    h = worktree.create_worktree(str(repo), slot_id="wt-clean", task="t")
    (Path(h.worktree_path) / "a.txt").write_text("changed\n", encoding="utf-8")
    c = worktree.commit_pending(h, message="edit")
    m = worktree.cherry_pick_into_parent(str(repo), h, tip_sha=c.tip_sha)
    assert m.ok and not m.conflict and not m.skipped
    # Parent now has the change.
    assert (repo / "a.txt").read_text() == "changed\n"
    worktree.remove_worktree(str(repo), h)


def test_cherry_pick_skipped_when_tip_equals_base(repo: Path):
    h = worktree.create_worktree(str(repo), slot_id="wt-skip", task="t")
    m = worktree.cherry_pick_into_parent(str(repo), h, tip_sha=h.base_sha)
    assert m.ok and m.skipped
    worktree.remove_worktree(str(repo), h)


def test_cherry_pick_conflict_aborts_and_returns_diff(repo: Path):
    """When the parent and the worktree edit the same lines divergently,
    cherry-pick must abort cleanly and return the worktree-vs-base diff so
    the parent agent can re-apply manually."""
    h = worktree.create_worktree(str(repo), slot_id="wt-conflict", task="t")
    # Worktree edits line 1 to "from-worktree".
    (Path(h.worktree_path) / "a.txt").write_text("from-worktree\n", encoding="utf-8")
    c = worktree.commit_pending(h, message="worktree edit")
    # Parent edits the same line to "from-parent" AFTER the worktree spawned.
    (repo / "a.txt").write_text("from-parent\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "parent edit")

    m = worktree.cherry_pick_into_parent(str(repo), h, tip_sha=c.tip_sha)
    assert not m.ok
    assert m.conflict
    assert "from-worktree" in m.diff  # diff contains the worktree's change
    # Parent's working tree is intact (cherry-pick aborted).
    assert (repo / "a.txt").read_text() == "from-parent\n"
    # Parent HEAD is still the parent's own commit (no half-merge state).
    assert worktree.head_sha(str(repo)) != h.base_sha
    worktree.remove_worktree(str(repo), h)


def test_remove_worktree_idempotent(repo: Path):
    """Calling remove twice must not raise (cancel path can double-reap)."""
    h = worktree.create_worktree(str(repo), slot_id="wt-idem", task="t")
    worktree.remove_worktree(str(repo), h)
    worktree.remove_worktree(str(repo), h)  # second call is a no-op
    assert not Path(h.worktree_path).exists()


# ---------------------------------------------------------------------------
# background._bg_start role="worktree" integration
# ---------------------------------------------------------------------------


def _stub_subagent_report(*args, **kwargs) -> SubagentReport:
    """Stand-in for run_subagent: the subagent 'edits' the worktree directly
    so we can verify the merge back into the parent."""
    # The SubagentSpec is the first positional arg; its task is irrelevant
    # here. The actual ctx.workspace points at the worktree path because
    # background._bg_start built run_ctx = replace(ctx, workspace=wt).
    spec: SubagentSpec = args[0]
    ctx: ToolContext = args[1]
    (Path(ctx.workspace) / "a.txt").write_text("from-bg\n", encoding="utf-8")
    return SubagentReport(
        name=spec.name,
        ok=True,
        summary="edited a.txt",
        steps_taken=2,
    )


def test_bg_start_role_worktree_creates_worktree_and_merges_on_collect(
    repo: Path, ctx: ToolContext
):
    with patch("wells.background.run_subagent", side_effect=_stub_subagent_report):
        r = tools.dispatch(
            "bg_start",
            {"task": "edit a.txt", "role": "worktree"},
            ctx,
        )
    assert r.ok, r.error
    assert "worktree-isolated" in r.output

    # Wait for the (mocked, fast) subagent to finish.
    _drain_registry()

    r = tools.dispatch("bg_collect", {}, ctx)
    assert r.ok, r.error
    # The merge landed in the parent.
    assert (repo / "a.txt").read_text() == "from-bg\n"
    # The merge footer is in the report.
    assert (
        "merged worktree branch" in r.output.lower()
        or "merged cleanly" in r.output.lower()
    )


def test_bg_start_role_worktree_conflict_returns_diff(repo: Path, ctx: ToolContext):
    """Parent edits the same line while the worktree subagent runs; collect
    must abort the cherry-pick, return the diff, and reap the worktree."""

    def _slow_edit_subagent(spec, ctx, **kw):
        # Subagent makes its edit.
        (Path(ctx.workspace) / "a.txt").write_text("from-worktree\n", encoding="utf-8")
        return SubagentReport(name=spec.name, ok=True, summary="edited", steps_taken=1)

    with patch("wells.background.run_subagent", side_effect=_slow_edit_subagent):
        tools.dispatch(
            "bg_start",
            {"task": "edit a.txt", "role": "worktree"},
            ctx,
        )
        # While the subagent "runs", the parent edits the same line + commits.
        # The mocked subagent returns synchronously, so we simulate parent
        # divergence BEFORE the registry thread collects.
        # Give the thread a moment to do its work first.
        _drain_registry()
        (repo / "a.txt").write_text("from-parent\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-q", "-m", "parent divergence")

    r = tools.dispatch("bg_collect", {}, ctx)
    # On conflict, collect returns ok=False (the merge didn't land — the parent
    # must re-apply manually from the diff). The diff text is in the output.
    assert not r.ok
    out = r.output.lower()
    assert "conflict" in out or "cherry-pick" in out
    assert "from-worktree" in r.output  # diff text present


def test_bg_start_role_worktree_rejects_non_git(tmp_path: Path):
    d = tmp_path / "plain"
    d.mkdir()
    ctx = ToolContext(workspace=str(d), safety="auto")
    r = tools.dispatch(
        "bg_start",
        {"task": "x", "role": "worktree"},
        ctx,
    )
    assert not r.ok
    assert "worktree" in r.error.lower()


def test_bg_start_role_worktree_disabled_via_env(monkeypatch, repo: Path):
    monkeypatch.setenv("WELLS_BG_WORKTREES", "0")
    assert not background.worktrees_enabled()
    ctx = ToolContext(workspace=str(repo), safety="auto")
    r = tools.dispatch(
        "bg_start",
        {"task": "x", "role": "worktree"},
        ctx,
    )
    assert not r.ok
    assert "disabled" in r.error.lower()


def test_bg_start_role_worktree_bad_role_rejected(ctx: ToolContext):
    r = tools.dispatch(
        "bg_start",
        {"task": "x", "role": "bogus"},
        ctx,
    )
    assert not r.ok
    assert "research" in r.error and "fix" in r.error and "worktree" in r.error


def test_reset_reaps_pending_worktrees(repo: Path, ctx: ToolContext):
    """reset() must clean up any worktree still on a slot — the agent may
    never call bg_collect before the next run starts."""

    def _hanging_subagent(spec, ctx, **kw):
        # Sleep longer than the test; reset() should mark us cancelled and
        # reap the worktree out from under us.
        time.sleep(2.0)
        return SubagentReport(name=spec.name, ok=True, summary="", steps_taken=0)

    with patch("wells.background.run_subagent", side_effect=_hanging_subagent):
        tools.dispatch(
            "bg_start",
            {"task": "x", "role": "worktree"},
            ctx,
        )
        # Don't wait — reset immediately, simulating the next run starting.
        time.sleep(0.05)  # let the slot register
        cancelled = background.REGISTRY.reset()
        assert cancelled >= 1

    # No worktree directories left on disk.
    assert not list(worktree.BG_WORKTREES_DIR.glob("wt-*"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _drain_registry(timeout: float = 5.0) -> None:
    """Wait until every running slot reaches a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if background.REGISTRY.pending() == 0:
            return
        time.sleep(0.02)
    raise AssertionError("registry did not drain within timeout")
