"""Tests for auto-skill authoring (self-improvement #2): qualify, propose, accept/reject."""

from __future__ import annotations

from pathlib import Path

import pytest

from wells import skill_authoring as sa
from wells import skills


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Auto safety so staging/promotion proceed; isolate from built-in skills."""
    monkeypatch.setenv("HARNESS_SAFETY", "auto")
    monkeypatch.setenv("WELLS_AUTOSKILL", "1")
    monkeypatch.setenv("WELLS_BUILTIN_SKILLS", "0")
    skills.clear_cache()
    yield
    skills.clear_cache()


def _plan_with_steps(n: int = 3) -> str:
    steps = "\n".join(f"{i}. Step number {i} touching src/mod.py." for i in range(1, n + 1))
    return (
        "COMPLEXITY: SIMPLE\n\n## Summary\nDo the thing.\n\n"
        f"## Implementation steps\n{steps}\n\n"
        "## Verification\nRun `pytest -q`.\n"
    )


def _clean_state(goal: str = "add redis caching to the api", *, steps: int = 3) -> dict:
    return {
        "goal": goal,
        "review_complete": True,
        "tests_passed": True,
        "implementation_steps": "Edited src/cache.py and src/api.py. Ran pytest -q, all green.",
        "development_plan": _plan_with_steps(steps),
    }


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------


def test_run_is_clean_true_for_verified_run():
    assert sa.run_is_clean(_clean_state()) is True


def test_run_is_clean_false_when_review_incomplete():
    s = _clean_state()
    s["review_complete"] = False
    assert sa.run_is_clean(s) is False


def test_run_is_clean_false_when_tests_red():
    s = _clean_state()
    s["tests_passed"] = False
    assert sa.run_is_clean(s) is False


def test_run_is_clean_false_when_dry():
    assert sa.run_is_clean(_clean_state(), dry=True) is False


def test_run_is_clean_false_when_aborted():
    s = _clean_state()
    s["implementation_steps"] = "Step 1 done; (aborted: step 2 did not complete)"
    assert sa.run_is_clean(s) is False


def test_run_is_clean_true_when_no_test_setup():
    """tests_passed absent (None) means no runnable suite — still a clean review."""
    s = _clean_state()
    del s["tests_passed"]
    assert sa.run_is_clean(s) is True


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------


def test_propose_stages_a_proposal(tmp_path: Path):
    path = sa.propose_from_state(_clean_state(), str(tmp_path))
    assert path is not None
    assert path.parent == tmp_path / ".wells" / "skill-proposals"
    raw = path.read_text(encoding="utf-8")
    assert "status: proposal" in raw
    assert "Source goal:" in raw
    assert "Step number 1" in raw  # plan steps made it into the body
    assert "pytest -q" in raw  # verification section included


def test_proposal_name_and_description_derived_from_goal(tmp_path: Path):
    path = sa.propose_from_state(_clean_state("Configure Redis caching"), str(tmp_path))
    assert path is not None
    prop = sa.list_proposals(str(tmp_path))[0]
    assert prop.name == "configure-redis-caching"
    assert prop.description  # non-empty


def test_proposal_not_discoverable_until_accepted(tmp_path: Path):
    """A staged proposal must NOT show up in the skill index (zero context cost)."""
    sa.propose_from_state(_clean_state(), str(tmp_path))
    idx = skills.skills_for(str(tmp_path))
    assert idx.is_empty()


def test_propose_skips_when_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WELLS_AUTOSKILL", "0")
    assert sa.propose_from_state(_clean_state(), str(tmp_path)) is None


def test_propose_skips_when_dry(tmp_path: Path):
    assert sa.propose_from_state(_clean_state(), str(tmp_path), dry=True) is None


def test_propose_skips_below_min_steps(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WELLS_AUTOSKILL_MIN_STEPS", "5")
    state = _clean_state(steps=3)
    assert sa.propose_from_state(state, str(tmp_path)) is None


def test_propose_dedup_skips_same_goal(tmp_path: Path):
    state = _clean_state("add redis caching to the api")
    first = sa.propose_from_state(state, str(tmp_path))
    second = sa.propose_from_state(state, str(tmp_path))
    assert first is not None
    assert second is None  # already proposed under the same slug
    assert len(sa.list_proposals(str(tmp_path))) == 1


def test_propose_respects_dryrun_safety(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_SAFETY", "dryrun")
    assert sa.propose_from_state(_clean_state(), str(tmp_path)) is None


# ---------------------------------------------------------------------------
# Accept / reject lifecycle
# ---------------------------------------------------------------------------


def test_accept_promotes_proposal_to_discoverable_skill(tmp_path: Path):
    sa.propose_from_state(_clean_state(), str(tmp_path))
    name = sa.list_proposals(str(tmp_path))[0].name

    ok, msg = sa.accept_proposal(name, str(tmp_path))
    assert ok, msg

    # Proposal is gone; skill is now discoverable under .wells/skills.
    assert sa.list_proposals(str(tmp_path)) == []
    promoted = tmp_path / ".wells" / "skills" / name / "SKILL.md"
    assert promoted.is_file()
    skill = skills.skills_for(str(tmp_path)).by_name(name)
    assert skill is not None
    assert "Step number 1" in skill.body


def test_accept_unknown_proposal(tmp_path: Path):
    ok, msg = sa.accept_proposal("nope", str(tmp_path))
    assert not ok
    assert "No proposal" in msg


def test_reject_deletes_proposal(tmp_path: Path):
    sa.propose_from_state(_clean_state(), str(tmp_path))
    name = sa.list_proposals(str(tmp_path))[0].name
    ok, msg = sa.reject_proposal(name, str(tmp_path))
    assert ok, msg
    assert sa.list_proposals(str(tmp_path)) == []
    # And nothing got promoted.
    assert skills.skills_for(str(tmp_path)).is_empty()


def test_reject_unknown_proposal(tmp_path: Path):
    ok, msg = sa.reject_proposal("nope", str(tmp_path))
    assert not ok


def test_accept_respects_dryrun_safety(tmp_path: Path, monkeypatch):
    # Stage under auto first, then flip to dryrun before accepting.
    sa.propose_from_state(_clean_state(), str(tmp_path))
    name = sa.list_proposals(str(tmp_path))[0].name
    monkeypatch.setenv("HARNESS_SAFETY", "dryrun")
    ok, msg = sa.accept_proposal(name, str(tmp_path))
    assert not ok
    assert "dry" in msg.lower() or "would" in msg.lower()
    # Proposal still staged, nothing promoted.
    assert len(sa.list_proposals(str(tmp_path))) == 1
    assert skills.skills_for(str(tmp_path)).is_empty()


# ---------------------------------------------------------------------------
# Edit-then-accept + proposal_body
# ---------------------------------------------------------------------------


def test_accept_with_overrides_uses_edited_description_and_body(tmp_path: Path):
    sa.propose_from_state(_clean_state(), str(tmp_path))
    name = sa.list_proposals(str(tmp_path))[0].name
    ok, msg = sa.accept_proposal(
        name, str(tmp_path),
        description="CUSTOM edited description",
        body="CUSTOM edited body line.",
    )
    assert ok, msg
    skill = skills.skills_for(str(tmp_path)).by_name(name)
    assert skill is not None
    assert skill.description == "CUSTOM edited description"
    assert "CUSTOM edited body line." in skill.body


def test_proposal_body_returns_staged_body(tmp_path: Path):
    sa.propose_from_state(_clean_state(), str(tmp_path))
    name = sa.list_proposals(str(tmp_path))[0].name
    ok, body = sa.proposal_body(name, str(tmp_path))
    assert ok
    assert "Step number 1" in body  # from _clean_state's plan


def test_proposal_body_unknown(tmp_path: Path):
    ok, msg = sa.proposal_body("nope", str(tmp_path))
    assert not ok
    assert "No proposal" in msg


# ---------------------------------------------------------------------------
# Post-run panel (surfaces proposals + reflections after a run)
# ---------------------------------------------------------------------------


def test_post_run_panel_noop_without_self_improvement_data():
    """No skill_proposed / reflection_written → no output at all."""
    import io
    from contextlib import redirect_stdout
    from wells.main import _print_post_run_panel

    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_post_run_panel({})
    assert buf.getvalue() == ""


def test_post_run_panel_names_the_proposed_skill():
    """When skill_proposed is set, the panel surfaces the name + accept command."""
    import io
    from contextlib import redirect_stdout
    from wells.main import _print_post_run_panel

    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_post_run_panel({
            "skill_proposed": "/ws/.wells/skill-proposals/my-thing.md",
        })
    out = buf.getvalue()
    assert "my-thing" in out
    assert "accept" in out
    assert "self-improvement" in out


def test_post_run_panel_mentions_captured_reflection():
    import io
    import re
    from contextlib import redirect_stdout
    from wells.main import _print_post_run_panel

    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_post_run_panel({"reflection_written": "/ws/.wells/reflections.md"})
    # Rich console wraps markup in ANSI codes; strip them for matching.
    out = re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())
    assert "reflection" in out.lower()
    assert "/reflections" in out
