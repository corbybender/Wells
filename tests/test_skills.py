"""Tests for the agent skills system: discovery, parsing, prompt injection, load_skill tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from wells import skills, tools


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace with a skills/ folder containing two skills."""
    sk = tmp_path / "skills"
    # Skill in a folder (recommended layout)
    (sk / "release").mkdir(parents=True)
    (sk / "release" / "SKILL.md").write_text(
        "---\nname: release\ndescription: How to cut a release.\n---\n"
        "1. Bump version\n2. Tag it\n3. Publish\n",
        encoding="utf-8",
    )
    # Second skill in a folder
    (sk / "add-provider").mkdir()
    (sk / "add-provider" / "SKILL.md").write_text(
        "---\nname: add-provider\ndescription: Add a new model provider profile.\n---\n"
        "Edit providers.py and add a profile block.\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_cache():
    skills.clear_cache()
    yield
    skills.clear_cache()


# ---------------------------------------------------------------------------
# Discovery + parsing
# ---------------------------------------------------------------------------


def test_discovers_skills_in_folders(workspace: Path):
    idx = skills.skills_for(str(workspace))
    names = {s.name for s in idx.skills}
    assert names == {"release", "add-provider"}


def test_skill_has_name_description_body(workspace: Path):
    idx = skills.skills_for(str(workspace))
    rel = idx.by_name("release")
    assert rel is not None
    assert rel.description == "How to cut a release."
    assert "Bump version" in rel.body


def test_by_name_case_insensitive(workspace: Path):
    idx = skills.skills_for(str(workspace))
    assert idx.by_name("RELEASE") is not None
    assert idx.by_name("Release") is not None


def test_by_name_unknown_returns_none(workspace: Path):
    idx = skills.skills_for(str(workspace))
    assert idx.by_name("nope") is None


def test_empty_workspace_has_no_skills(tmp_path: Path):
    idx = skills.skills_for(str(tmp_path))
    assert idx.is_empty()


def test_loose_skill_md_at_root(tmp_path: Path):
    """A skills/SKILL.md (no folder) is also discovered."""
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "SKILL.md").write_text(
        "---\nname: lone\ndescription: a single skill.\n---\nBody.\n", encoding="utf-8"
    )
    idx = skills.skills_for(str(tmp_path))
    assert idx.by_name("lone") is not None


def test_skill_name_defaults_to_folder_name(tmp_path: Path):
    """When front-matter omits name, the folder name is used."""
    (tmp_path / "skills" / "thing").mkdir(parents=True)
    (tmp_path / "skills" / "thing" / "SKILL.md").write_text(
        "---\ndescription: no name field.\n---\nBody.\n", encoding="utf-8"
    )
    idx = skills.skills_for(str(tmp_path))
    assert idx.by_name("thing") is not None


def test_malformed_front_matter_still_loads(tmp_path: Path):
    (tmp_path / "skills" / "x").mkdir(parents=True)
    (tmp_path / "skills" / "x" / "SKILL.md").write_text(
        "no front matter at all, just a body", encoding="utf-8"
    )
    idx = skills.skills_for(str(tmp_path))
    s = idx.by_name("x")
    assert s is not None
    assert "no front matter" in s.body


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def test_skill_index_block_empty_when_no_skills(tmp_path: Path):
    assert skills.skill_index_block(str(tmp_path)) == ""


def test_skill_index_block_lists_skills(workspace: Path):
    block = skills.skill_index_block(str(workspace))
    assert "AVAILABLE SKILLS" in block
    assert "- release:" in block
    assert "- add-provider:" in block
    assert "load_skill" in block


def test_inject_into_prompt_appends_block(workspace: Path):
    out = skills.inject_into_prompt("BASE PROMPT", str(workspace))
    assert out.startswith("BASE PROMPT")
    assert "AVAILABLE SKILLS" in out


def test_inject_into_prompt_noop_without_skills(tmp_path: Path):
    assert skills.inject_into_prompt("BASE", str(tmp_path)) == "BASE"


# ---------------------------------------------------------------------------
# load_skill_body
# ---------------------------------------------------------------------------


def test_load_skill_body_returns_content(workspace: Path):
    ok, body = skills.load_skill_body("release", str(workspace))
    assert ok
    assert "Bump version" in body


def test_load_skill_body_unknown_lists_available(workspace: Path):
    ok, body = skills.load_skill_body("missing", str(workspace))
    assert not ok
    assert "release" in body
    assert "add-provider" in body


def test_load_skill_body_empty_workspace(tmp_path: Path):
    ok, body = skills.load_skill_body("anything", str(tmp_path))
    assert not ok
    assert "No skills" in body


def test_load_skill_body_truncates_large_body(tmp_path: Path):
    (tmp_path / "skills" / "big").mkdir(parents=True)
    (tmp_path / "skills" / "big" / "SKILL.md").write_text(
        "---\nname: big\ndescription: d.\n---\n" + "x" * 20000, encoding="utf-8"
    )
    ok, body = skills.load_skill_body("big", str(tmp_path))
    assert ok
    assert "truncated" in body


# ---------------------------------------------------------------------------
# Feature gating
# ---------------------------------------------------------------------------


def test_disabled_via_env(monkeypatch, workspace: Path):
    monkeypatch.setenv("WELLS_SKILLS", "0")
    assert not skills.enabled()
    idx = skills.skills_for(str(workspace))
    assert idx.is_empty()


# ---------------------------------------------------------------------------
# load_skill tool integration
# ---------------------------------------------------------------------------


def test_load_skill_tool_dispatch(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto")
    r = tools.dispatch("load_skill", {"name": "release"}, ctx)
    assert r.ok
    assert "Bump version" in r.output


def test_load_skill_tool_unknown_name(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto")
    r = tools.dispatch("load_skill", {"name": "nope"}, ctx)
    assert not r.ok
    assert "release" in r.output  # lists available


def test_load_skill_tool_missing_name_arg(workspace: Path):
    ctx = tools.ToolContext(workspace=str(workspace), safety="auto")
    r = tools.dispatch("load_skill", {"name": ""}, ctx)
    assert not r.ok


def test_load_skill_is_registered():
    names = [t.name for t in tools.ALL_TOOLS]
    assert "load_skill" in names


# ---------------------------------------------------------------------------
# Mutation operations: create / read-raw / update / delete
# ---------------------------------------------------------------------------

@pytest.fixture
def safe_workspace(tmp_path: Path) -> Path:
    """Workspace with auto safety so write operations proceed."""
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_cache_mutation():
    """Ensure no stale cache between mutation tests."""
    skills.clear_cache()
    yield
    skills.clear_cache()


# -- name validation --


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "with space",
        "-leading",
        "trailing-",
        "double--hyphen",
        "has.dots",
        "a" * 65,
    ],
)
def test_validate_name_rejects_invalid(bad: str):
    assert skills.validate_name(bad) is not None


@pytest.mark.parametrize("good", ["release", "add-provider", "abc123", "x", "a-b-c-1"])
def test_validate_name_accepts_valid(good: str):
    assert skills.validate_name(good) is None


def test_validate_name_normalizes_case():
    """UPPERCASE names are accepted and normalized to lowercase."""
    assert skills.validate_name("UPPER") is None  # valid after lowercasing
    ok, _ = skills.create_skill("UPPER", "d", "b", str(Path("/tmp/_wells_test_norm")))
    # cleanup if created
    if ok:
        import shutil
        shutil.rmtree(Path("/tmp/_wells_test_norm/skills/upper"), ignore_errors=True)


# -- create_skill --


def test_create_skill_writes_file(safe_workspace: Path):
    ok, msg = skills.create_skill("release", "How to release.", "1. Tag\n2. Publish\n", str(safe_workspace))
    assert ok
    path = safe_workspace / "skills" / "release" / "SKILL.md"
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert "name: release" in content
    assert "How to release." in content
    assert "1. Tag" in content


def test_create_skill_appears_in_index(safe_workspace: Path):
    skills.create_skill("x", "desc", "body", str(safe_workspace))
    idx = skills.skills_for(str(safe_workspace))
    assert idx.by_name("x") is not None


def test_create_skill_rejects_duplicate(safe_workspace: Path):
    skills.create_skill("x", "d", "b", str(safe_workspace))
    ok, msg = skills.create_skill("x", "d", "b", str(safe_workspace))
    assert not ok
    assert "already exists" in msg.lower()


def test_create_skill_rejects_bad_name(safe_workspace: Path):
    ok, msg = skills.create_skill("Bad Name!", "d", "b", str(safe_workspace))
    assert not ok
    assert "must be" in msg.lower()


def test_create_skill_respects_dryrun(safe_workspace: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_SAFETY", "dryrun")
    ok, msg = skills.create_skill("x", "d", "b", str(safe_workspace))
    assert not ok
    assert "dry" in msg.lower() or "would" in msg.lower()
    assert not (safe_workspace / "skills" / "x").exists()


# -- read_skill_raw --


def test_read_skill_raw_returns_full_file(safe_workspace: Path):
    skills.create_skill("x", "The desc.", "Line 1\nLine 2\n", str(safe_workspace))
    ok, raw = skills.read_skill_raw("x", str(safe_workspace))
    assert ok
    assert "---" in raw  # front-matter present
    assert "name: x" in raw
    assert "The desc." in raw
    assert "Line 1" in raw


def test_read_skill_raw_unknown(safe_workspace: Path):
    ok, raw = skills.read_skill_raw("missing", str(safe_workspace))
    assert not ok
    assert "Unknown" in raw


# -- update_skill --


def test_update_skill_changes_description(safe_workspace: Path):
    skills.create_skill("x", "old desc", "body", str(safe_workspace))
    ok, msg = skills.update_skill("x", str(safe_workspace), description="new desc")
    assert ok
    skill = skills.skills_for(str(safe_workspace)).by_name("x")
    assert skill.description == "new desc"
    assert skill.body == "body"  # unchanged


def test_update_skill_changes_body(safe_workspace: Path):
    skills.create_skill("x", "desc", "old body", str(safe_workspace))
    ok, msg = skills.update_skill("x", str(safe_workspace), body="new body")
    assert ok
    skill = skills.skills_for(str(safe_workspace)).by_name("x")
    assert skill.body == "new body"
    assert skill.description == "desc"  # unchanged


def test_update_skill_unknown(safe_workspace: Path):
    ok, msg = skills.update_skill("missing", str(safe_workspace), body="x")
    assert not ok


def test_update_skill_respects_dryrun(safe_workspace: Path, monkeypatch):
    skills.create_skill("x", "desc", "old", str(safe_workspace))
    monkeypatch.setenv("HARNESS_SAFETY", "dryrun")
    ok, msg = skills.update_skill("x", str(safe_workspace), body="new")
    assert not ok
    # Body unchanged.
    assert skills.skills_for(str(safe_workspace)).by_name("x").body == "old"


# -- delete_skill --


def test_delete_skill_removes_folder(safe_workspace: Path):
    skills.create_skill("x", "d", "b", str(safe_workspace))
    skill_dir = safe_workspace / "skills" / "x"
    assert skill_dir.exists()
    ok, msg = skills.delete_skill("x", str(safe_workspace))
    assert ok
    assert not skill_dir.exists()
    assert skills.skills_for(str(safe_workspace)).by_name("x") is None


def test_delete_skill_unknown(safe_workspace: Path):
    ok, msg = skills.delete_skill("missing", str(safe_workspace))
    assert not ok


def test_delete_skill_respects_dryrun(safe_workspace: Path, monkeypatch):
    skills.create_skill("x", "d", "b", str(safe_workspace))
    monkeypatch.setenv("HARNESS_SAFETY", "dryrun")
    ok, msg = skills.delete_skill("x", str(safe_workspace))
    assert not ok
    assert (safe_workspace / "skills" / "x").exists()


# -- skill_file_path --


def test_skill_file_path_returns_path(safe_workspace: Path):
    skills.create_skill("x", "d", "b", str(safe_workspace))
    path = skills.skill_file_path("x", str(safe_workspace))
    assert path is not None
    assert path.name == "SKILL.md"


def test_skill_file_path_unknown_returns_none(safe_workspace: Path):
    assert skills.skill_file_path("missing", str(safe_workspace)) is None


# -- CLI handler (/skills list + remove are non-blocking; safe to test) --


def test_cli_skills_list_empty(safe_workspace: Path, monkeypatch, capsys):
    import wells.cli as cli_mod
    monkeypatch.setattr(cli_mod.config, "WORKSPACE_ROOT", str(safe_workspace))
    cli_mod._handle_skills("list")
    out = capsys.readouterr().out
    assert "No skills" in out or "no skills" in out.lower()


def test_cli_skills_list_shows_skills(safe_workspace: Path, monkeypatch, capsys):
    import wells.cli as cli_mod
    skills.create_skill("release", "Release howto.", "body", str(safe_workspace))
    monkeypatch.setattr(cli_mod.config, "WORKSPACE_ROOT", str(safe_workspace))
    cli_mod._handle_skills("list")
    out = capsys.readouterr().out
    assert "release" in out
    assert "Release howto." in out


def test_cli_skills_show(safe_workspace: Path, monkeypatch, capsys):
    import wells.cli as cli_mod
    skills.create_skill("release", "desc", "Tag it\n", str(safe_workspace))
    monkeypatch.setattr(cli_mod.config, "WORKSPACE_ROOT", str(safe_workspace))
    cli_mod._handle_skills("show release")
    out = capsys.readouterr().out
    assert "Tag it" in out
    assert "name: release" in out


def test_cli_skills_remove(safe_workspace: Path, monkeypatch, capsys):
    import wells.cli as cli_mod
    skills.create_skill("release", "d", "b", str(safe_workspace))
    monkeypatch.setattr(cli_mod.config, "WORKSPACE_ROOT", str(safe_workspace))
    cli_mod._handle_skills("remove release")
    out = capsys.readouterr().out
    assert "Deleted" in out
    assert skills.skills_for(str(safe_workspace)).by_name("release") is None


def test_cli_skills_show_unknown(safe_workspace: Path, monkeypatch, capsys):
    import wells.cli as cli_mod
    monkeypatch.setattr(cli_mod.config, "WORKSPACE_ROOT", str(safe_workspace))
    cli_mod._handle_skills("show missing")
    out = capsys.readouterr().out
    assert "Unknown" in out or "missing" in out.lower()

