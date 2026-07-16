"""Tests for the stepwise coder: one executor run per plan step, fresh
context each time (the structural fix for small-context models losing the
thread across a long single run)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from wells import config
from wells.agents import coder as coder_mod
from wells.executor import ExecutorResult


PLAN = """COMPLEXITY: COMPLEX

## Summary
Add a config flag and wire it through.

## Affected files
- src/app.py — add flag

## Implementation steps
1. Edit src/app.py line 10 — add the FLAG constant.
   Default it to False.
2. Edit src/app.py line 40 — read FLAG in main().
3. Add a test in tests/test_app.py covering both flag states.

## Verification
Run pytest tests/test_app.py -q and confirm all tests pass.

## Risks / gotchas
1. Watch for the legacy FLAG in old_config.py — do not confuse the two.
"""


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


def test_parse_plan_steps_extracts_numbered_steps_with_continuations():
    steps = coder_mod._parse_plan_steps(PLAN)
    assert len(steps) == 3
    assert steps[0].startswith("1. Edit src/app.py line 10")
    assert "Default it to False." in steps[0]  # continuation line kept
    assert steps[2].startswith("3. Add a test")


def test_parse_plan_steps_ignores_numbered_lists_outside_the_section():
    """The '1.' under Risks must not be parsed as an implementation step."""
    steps = coder_mod._parse_plan_steps(PLAN)
    assert not any("old_config.py" in s for s in steps)


def test_parse_plan_steps_empty_without_heading():
    assert coder_mod._parse_plan_steps("just do the thing:\n1. a\n2. b") == []
    assert coder_mod._parse_plan_steps("") == []


def test_section_extracts_verification():
    v = coder_mod._section(PLAN, "Verification")
    assert "pytest tests/test_app.py" in v
    assert "Risks" not in v


# ---------------------------------------------------------------------------
# Activation policy
# ---------------------------------------------------------------------------


def test_stepwise_active_forced_on_and_off():
    with patch.object(config, "WELLS_STEPWISE", "1"):
        assert coder_mod._stepwise_active() is True
    with patch.object(config, "WELLS_STEPWISE", "0"):
        assert coder_mod._stepwise_active() is False


def test_stepwise_auto_follows_local_ollama_detection():
    from wells import providers

    local = providers.ProviderProfile(
        name="q", kind="openai", model="qwen2.5-coder:7b",
        base_url="http://127.0.0.1:11434/v1",
    )
    cloud = providers.ProviderProfile(
        name="zai", kind="openai", model="glm-5.2",
        base_url="https://api.z.ai/api/coding/paas/v4/",
    )
    with (
        patch.object(config, "WELLS_STEPWISE", "auto"),
        patch.object(providers, "load_profile", return_value=local),
    ):
        assert coder_mod._stepwise_active() is True
    with (
        patch.object(config, "WELLS_STEPWISE", "auto"),
        patch.object(providers, "load_profile", return_value=cloud),
    ):
        assert coder_mod._stepwise_active() is False


# ---------------------------------------------------------------------------
# Stepwise coder integration
# ---------------------------------------------------------------------------


def _state(tmp_path: Path) -> dict:
    return {
        "goal": "add the config flag",
        "development_plan": PLAN,
        "workspace_root": str(tmp_path),
        "iteration": 0,
    }


def _ok(summary: str) -> ExecutorResult:
    return ExecutorResult(summary=summary, steps_taken=2, stopped_reason="done")


def test_stepwise_coder_runs_one_executor_call_per_step(tmp_path: Path):
    tasks: list[str] = []

    def fake_run(*, task, ctx, step_label, **kw):
        tasks.append(task)
        return _ok(f"completed ({step_label})")

    with (
        patch.object(config, "WELLS_STEPWISE", "1"),
        patch.object(coder_mod, "run_executor", side_effect=fake_run),
    ):
        out = coder_mod.coder(_state(tmp_path))

    # 3 steps + 1 verification run
    assert len(tasks) == 4
    assert all("YOUR CURRENT STEP" in t for t in tasks[:3])
    assert "pytest tests/test_app.py" in tasks[3]
    # Carry-over ledger: step 2's task must list step 1 as completed.
    assert "STEPS ALREADY COMPLETED" in tasks[1]
    assert "Edit src/app.py line 10" in tasks[1]
    # Step 1's task must not claim anything is done yet.
    assert "(none yet" in tasks[0]
    assert "Step 1 [done]" in out["implementation_steps"]
    assert "Verification [done]" in out["implementation_steps"]


def test_stepwise_coder_aborts_remaining_steps_on_failure(tmp_path: Path):
    calls: list[str] = []

    def fake_run(*, task, ctx, step_label, **kw):
        calls.append(step_label)
        if step_label.endswith(".2"):
            return ExecutorResult(summary="(stuck)", stopped_reason="stuck_loop")
        return _ok("fine")

    with (
        patch.object(config, "WELLS_STEPWISE", "1"),
        patch.object(coder_mod, "run_executor", side_effect=fake_run),
    ):
        out = coder_mod.coder(_state(tmp_path))

    assert calls == ["coder-1.1", "coder-1.2"]  # step 3 + verify never ran
    assert "aborted" in out["implementation_steps"]


def test_coder_uses_single_run_when_stepwise_off(tmp_path: Path):
    calls: list[str] = []

    def fake_run(*, task, ctx, step_label, **kw):
        calls.append(step_label)
        return _ok("did it all in one run")

    with (
        patch.object(config, "WELLS_STEPWISE", "0"),
        patch.object(coder_mod, "run_executor", side_effect=fake_run),
    ):
        out = coder_mod.coder(_state(tmp_path))

    assert calls == ["coder-1"]
    assert out["code_changes"] == "did it all in one run"


def test_coder_skips_stepwise_on_review_loop_iterations(tmp_path: Path):
    calls: list[str] = []

    def fake_run(*, task, ctx, step_label, **kw):
        calls.append(step_label)
        return _ok("addressed feedback")

    state = _state(tmp_path)
    state["iteration"] = 1  # coming back from a review loop -> iteration 2
    state["review_result"] = "INCOMPLETE: fix the test"
    with (
        patch.object(config, "WELLS_STEPWISE", "1"),
        patch.object(coder_mod, "run_executor", side_effect=fake_run),
    ):
        coder_mod.coder(state)

    assert calls == ["coder-2"]
