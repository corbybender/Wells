"""Cross-harness compatibility: skill path sharing + sandbox auto-upgrade."""

from __future__ import annotations

from pathlib import Path

from wells import safety, skills


def _make_skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} test skill\n---\nBody of {name}.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Cross-harness skill discovery (.claude/.agents/.codex skills)
# ---------------------------------------------------------------------------


def test_claude_skills_discovered(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    _make_skill(tmp_path / ".claude" / "skills", "claude-pack")
    idx = skills.skills_for(str(tmp_path))
    assert idx.by_name("claude-pack") is not None


def test_agents_and_codex_skills_discovered(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    _make_skill(tmp_path / ".agents" / "skills", "agents-pack")
    _make_skill(tmp_path / ".codex" / "skills", "codex-pack")
    idx = skills.skills_for(str(tmp_path))
    assert idx.by_name("agents-pack") is not None
    assert idx.by_name("codex-pack") is not None


def test_workspace_skills_shadow_crossharness(tmp_path: Path, monkeypatch):
    # First-wins: <workspace>/skills beats .claude/skills on name collision.
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    _make_skill(tmp_path / "skills", "dup")
    _make_skill(tmp_path / ".claude" / "skills", "dup")
    skill = skills.skills_for(str(tmp_path)).by_name("dup")
    assert skill is not None
    assert "skills" in str(skill.path) and ".claude" not in str(skill.path)


def test_cross_skills_opt_out(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WELLS_CROSS_SKILLS", "0")
    _make_skill(tmp_path / ".claude" / "skills", "hidden-pack")
    idx = skills.skills_for(str(tmp_path))
    assert idx.by_name("hidden-pack") is None


# ---------------------------------------------------------------------------
# Sandbox auto-upgrade (Codex-style default sandbox, opt-in for now)
# ---------------------------------------------------------------------------


def test_policy_auto_upgrades_to_sandbox(monkeypatch):
    monkeypatch.setenv("WELLS_SANDBOX_AUTO", "1")
    monkeypatch.setattr(safety, "_sandbox_probe_result", True)
    assert safety.policy("auto") == "sandbox"
    assert safety.policy(None) == "sandbox"


def test_policy_no_upgrade_without_opt_in(monkeypatch):
    monkeypatch.delenv("WELLS_SANDBOX_AUTO", raising=False)
    monkeypatch.setattr(safety, "_sandbox_probe_result", True)
    assert safety.policy("auto") == "auto"


def test_policy_no_upgrade_without_runtime(monkeypatch):
    monkeypatch.setenv("WELLS_SANDBOX_AUTO", "1")
    monkeypatch.setattr(safety, "_sandbox_probe_result", False)
    assert safety.policy("auto") == "auto"


def test_explicit_modes_never_upgraded(monkeypatch):
    monkeypatch.setenv("WELLS_SANDBOX_AUTO", "1")
    monkeypatch.setattr(safety, "_sandbox_probe_result", True)
    assert safety.policy("dryrun") == "dryrun"
    assert safety.policy("approve") == "approve"
    assert safety.policy("sandbox") == "sandbox"
