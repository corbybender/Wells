"""Tests for the subagent persona system: discovery, parsing, CRUD, prompt
injection, and wiring into subagents.py/background.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from wells import personas, tools


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace with an agents/ folder containing one persona."""
    ag = tmp_path / "agents"
    (ag / "security-reviewer").mkdir(parents=True)
    (ag / "security-reviewer" / "PERSONA.md").write_text(
        "---\nname: security-reviewer\n"
        "description: Reviews diffs for security issues.\ntools: readonly\n---\n"
        "You are a senior security reviewer. Look for injection and secrets.\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_cache():
    personas.clear_cache()
    yield
    personas.clear_cache()


# ---------------------------------------------------------------------------
# Discovery + parsing
# ---------------------------------------------------------------------------


def test_discovers_persona_in_folder(workspace: Path):
    idx = personas.personas_for(str(workspace))
    names = {p.name for p in idx.personas}
    assert names == {"security-reviewer"}


def test_persona_has_name_description_tools_prompt(workspace: Path):
    idx = personas.personas_for(str(workspace))
    p = idx.by_name("security-reviewer")
    assert p is not None
    assert p.description == "Reviews diffs for security issues."
    assert p.toolset == "readonly"
    assert "senior security reviewer" in p.system_prompt


def test_by_name_case_insensitive(workspace: Path):
    idx = personas.personas_for(str(workspace))
    assert idx.by_name("SECURITY-REVIEWER") is not None


def test_by_name_unknown_returns_none(workspace: Path):
    idx = personas.personas_for(str(workspace))
    assert idx.by_name("nope") is None


def test_empty_workspace_has_no_personas(tmp_path: Path):
    assert personas.personas_for(str(tmp_path)).is_empty()


def test_loose_persona_md_at_root(tmp_path: Path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "PERSONA.md").write_text(
        "---\nname: lone\ndescription: single persona.\n---\nBody.\n", encoding="utf-8"
    )
    idx = personas.personas_for(str(tmp_path))
    assert idx.by_name("lone") is not None


def test_persona_name_defaults_to_folder_name(tmp_path: Path):
    (tmp_path / "agents" / "thing").mkdir(parents=True)
    (tmp_path / "agents" / "thing" / "PERSONA.md").write_text(
        "---\ndescription: no name given.\n---\nBody.\n", encoding="utf-8"
    )
    idx = personas.personas_for(str(tmp_path))
    assert idx.by_name("thing") is not None


def test_invalid_toolset_falls_back_to_full(tmp_path: Path):
    (tmp_path / "agents" / "x").mkdir(parents=True)
    (tmp_path / "agents" / "x" / "PERSONA.md").write_text(
        "---\nname: x\ntools: bogus\n---\nBody.\n", encoding="utf-8"
    )
    p = personas.personas_for(str(tmp_path)).by_name("x")
    assert p.toolset == "full"


def test_default_toolset_is_full(tmp_path: Path):
    (tmp_path / "agents" / "x").mkdir(parents=True)
    (tmp_path / "agents" / "x" / "PERSONA.md").write_text(
        "---\nname: x\n---\nBody.\n", encoding="utf-8"
    )
    p = personas.personas_for(str(tmp_path)).by_name("x")
    assert p.toolset == "full"


def test_disabled_via_env(workspace: Path, monkeypatch):
    monkeypatch.setenv("WELLS_AGENTS", "0")
    assert personas.personas_for(str(workspace)).is_empty()


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def test_index_block_lists_persona(workspace: Path):
    block = personas.persona_index_block(str(workspace))
    assert "security-reviewer" in block
    assert "Reviews diffs for security issues." in block
    assert "AVAILABLE SUBAGENT PERSONAS" in block


def test_index_block_empty_when_no_personas(tmp_path: Path):
    assert personas.persona_index_block(str(tmp_path)) == ""


def test_inject_into_prompt_appends_block(workspace: Path):
    out = personas.inject_into_prompt("BASE PROMPT", str(workspace))
    assert out.startswith("BASE PROMPT")
    assert "security-reviewer" in out


def test_inject_into_prompt_noop_when_empty(tmp_path: Path):
    assert personas.inject_into_prompt("BASE", str(tmp_path)) == "BASE"


def test_system_prompt_never_in_index_block(workspace: Path):
    """The full system_prompt must not appear in the parent-facing index --
    only name + description. Only the SUBAGENT run should see the full body."""
    block = personas.persona_index_block(str(workspace))
    assert "Look for injection and secrets" not in block


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expect_err",
    [
        ("", True),
        ("Bad_Name", True),
        ("-leading", True),
        ("trailing-", True),
        ("double--hyphen", True),
        ("valid-name", False),
        ("valid123", False),
    ],
)
def test_validate_name(name, expect_err):
    err = personas.validate_name(name)
    assert (err is not None) == expect_err


@pytest.mark.parametrize(
    "toolset,expect_err",
    [("readonly", False), ("exec", False), ("full", False), ("bogus", True), ("", True)],
)
def test_validate_toolset(toolset, expect_err):
    err = personas.validate_toolset(toolset)
    assert (err is not None) == expect_err


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_persona(tmp_path: Path):
    ok, msg = personas.create_persona(
        "reviewer", "Reviews code.", "You are a reviewer.", "readonly", str(tmp_path)
    )
    assert ok, msg
    p = personas.personas_for(str(tmp_path)).by_name("reviewer")
    assert p is not None
    assert p.toolset == "readonly"
    assert p.system_prompt == "You are a reviewer."


def test_create_persona_rejects_bad_name(tmp_path: Path):
    ok, msg = personas.create_persona("Bad Name", "x", "y", "full", str(tmp_path))
    assert not ok


def test_create_persona_rejects_bad_toolset(tmp_path: Path):
    ok, msg = personas.create_persona("ok-name", "x", "y", "bogus", str(tmp_path))
    assert not ok
    assert "tools" in msg.lower()


def test_create_persona_refuses_duplicate(workspace: Path):
    ok, msg = personas.create_persona(
        "security-reviewer", "dup", "dup", "full", str(workspace)
    )
    assert not ok
    assert "already exists" in msg


def test_update_persona_description_only(workspace: Path):
    ok, msg = personas.update_persona(
        "security-reviewer", str(workspace), description="New desc."
    )
    assert ok, msg
    p = personas.personas_for(str(workspace)).by_name("security-reviewer")
    assert p.description == "New desc."
    assert p.toolset == "readonly"  # unchanged


def test_update_persona_toolset(workspace: Path):
    ok, msg = personas.update_persona("security-reviewer", str(workspace), toolset="full")
    assert ok, msg
    p = personas.personas_for(str(workspace)).by_name("security-reviewer")
    assert p.toolset == "full"


def test_update_persona_rejects_bad_toolset(workspace: Path):
    ok, msg = personas.update_persona("security-reviewer", str(workspace), toolset="bogus")
    assert not ok


def test_update_unknown_persona_fails(tmp_path: Path):
    ok, msg = personas.update_persona("nope", str(tmp_path), description="x")
    assert not ok


def test_delete_persona(workspace: Path):
    ok, msg = personas.delete_persona("security-reviewer", str(workspace))
    assert ok, msg
    assert personas.personas_for(str(workspace)).is_empty()


def test_delete_unknown_persona_fails(tmp_path: Path):
    ok, msg = personas.delete_persona("nope", str(tmp_path))
    assert not ok


def test_read_persona_raw(workspace: Path):
    ok, raw = personas.read_persona_raw("security-reviewer", str(workspace))
    assert ok
    assert "name: security-reviewer" in raw
    assert "tools: readonly" in raw


def test_read_persona_raw_unknown(tmp_path: Path):
    ok, raw = personas.read_persona_raw("nope", str(tmp_path))
    assert not ok


# ---------------------------------------------------------------------------
# Wiring into subagents.py / background.py
# ---------------------------------------------------------------------------


def test_persona_subagent_uses_system_prompt_as_prefix(workspace: Path):
    from wells import subagents

    p = personas.personas_for(str(workspace)).by_name("security-reviewer")
    spec = subagents.persona_subagent("t1", p, "do the thing", "fix")
    assert spec.system_prefix == p.system_prompt
    assert spec.task == "do the thing"
    assert spec.role == "fix"


def test_persona_subagent_research_role_forces_readonly(workspace: Path):
    """Even a persona requesting tools=full must be capped to readonly when
    role=research -- the role is the safety ceiling, not the persona."""
    from wells import subagents

    ok, _ = personas.update_persona("security-reviewer", str(workspace), toolset="full")
    assert ok
    p = personas.personas_for(str(workspace)).by_name("security-reviewer")
    assert p.toolset == "full"
    spec = subagents.persona_subagent("t1", p, "investigate", "research")
    assert spec.toolset == "readonly"


def test_persona_subagent_fix_role_uses_persona_toolset(workspace: Path):
    from wells import subagents

    p = personas.personas_for(str(workspace)).by_name("security-reviewer")  # readonly
    spec = subagents.persona_subagent("t1", p, "do it", "fix")
    assert spec.toolset == "readonly"  # persona's own tools: readonly, honored for fix too


def test_bg_start_unknown_persona_errors(workspace: Path):
    tools._ensure_optional_registered()
    ctx = tools.ToolContext(workspace=str(workspace))
    r = tools.dispatch(
        "bg_start", {"task": "x", "persona": "does-not-exist"}, ctx
    )
    assert not r.ok
    assert "unknown persona" in r.error.lower()


def test_bg_start_persona_resolves_and_starts(workspace: Path):
    """Mocks run_subagent (like test_background.py) so this never makes a
    real LLM call -- only checks that persona resolution + bg_start's
    immediate response work, not the eventual subagent report."""
    from unittest.mock import patch as _patch
    from wells import background
    from wells.subagents import SubagentReport

    background.REGISTRY.reset()
    tools._ensure_optional_registered()
    ctx = tools.ToolContext(workspace=str(workspace))
    with _patch(
        "wells.background.run_subagent",
        return_value=SubagentReport(name="x", ok=True, summary="ok", steps_taken=1),
    ):
        r = tools.dispatch(
            "bg_start",
            {"task": "look for issues", "role": "research", "persona": "security-reviewer"},
            ctx,
        )
    assert r.ok, r.error
    assert "security-reviewer" in r.output
    background.REGISTRY.reset()


def test_bg_start_tool_schema_has_persona_param():
    from wells import background

    props = background.BG_START_TOOL.input_schema["properties"]
    assert "persona" in props
