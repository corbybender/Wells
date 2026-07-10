"""Tests for the stop-reason banner and suggest-continue affordance.

When a run ends for any reason other than clean completion (step cap, token
budget, max iterations, cancellation, error), cli.py surfaces a clear,
actionable banner explaining WHY it stopped and WHAT the user can do.

These tests cover the pure helpers (_format_stop_banner, _is_placeholder_summary)
directly — they are the contract the cli.py call sites depend on.
"""

from __future__ import annotations

import pytest

from wells import cli


# ---------------------------------------------------------------------------
# _format_stop_banner
# ---------------------------------------------------------------------------


def test_done_returns_no_banner():
    """A clean finish produces no banner — no noise on success."""
    assert cli._format_stop_banner(reason="done", steps=10) == []
    assert cli._format_stop_banner(reason="complete", steps=10) == []
    assert cli._format_stop_banner(reason="", steps=10) == []


def test_max_steps_banner_has_reason_and_actions():
    """The step-cap banner must say WHY (cap hit) and WHAT to do (continue/raise)."""
    lines = cli._format_stop_banner(
        reason="max_steps", steps=100, step_cap=100
    )
    joined = " ".join(lines).lower()
    # WHY: name the reason and the step count.
    assert "tool-step cap" in joined
    assert "100" in joined
    # WHAT: at least one concrete next step mentioning the cap and continuing.
    assert "continue" in joined
    assert "max_tool_steps" in joined
    # Prominence: the headline uses bold-yellow.
    assert any("[bold yellow]" in ln for ln in lines)


def test_budget_banner_shows_used_and_cap():
    """The budget banner must show actual token usage and the configured cap."""
    lines = cli._format_stop_banner(
        reason="budget", budget=2_000_000, budget_used=2_030_587
    )
    joined = " ".join(lines)
    # The user sees their actual numbers, not a generic message.
    assert "2,030,587" in joined
    assert "2,000,000" in joined
    assert "continue" in joined.lower()
    assert "max_run_tokens" in joined.lower()
    # Budget overruns are red (more severe than step-cap yellow).
    assert any("[bold red]" in ln for ln in lines)


def test_max_iterations_banner_explains_reviewer_loop():
    """The iteration-cap banner must explain the coder↔reviewer dynamic."""
    lines = cli._format_stop_banner(
        reason="max_iterations", iterations=3, max_iter=3
    )
    joined = " ".join(lines).lower()
    assert "iteration" in joined
    assert "incomplete" in joined
    assert "reviewer" in joined
    assert "max_iterations" in joined
    assert "continue" in joined


def test_cancelled_banner_offers_resume_and_undo():
    """Cancelled runs leave partial state — banner offers both resume and undo."""
    lines = cli._format_stop_banner(reason="cancelled")
    joined = " ".join(lines).lower()
    assert "cancel" in joined
    assert "continue" in joined
    assert "/undo" in joined


def test_error_banner_includes_error_text():
    """Errors surface the underlying message and point at /doctor."""
    lines = cli._format_stop_banner(
        reason="error", error="RuntimeError: network down"
    )
    joined = " ".join(lines)
    assert "RuntimeError: network down" in joined
    assert "/doctor" in joined
    assert any("[bold red]" in ln for ln in lines)


def test_unknown_reason_still_produces_a_banner():
    """An unrecognized reason must not silently pass — surface *something*."""
    lines = cli._format_stop_banner(reason="mystery")
    assert len(lines) >= 1
    assert "mystery" in " ".join(lines)


def test_budget_without_numbers_does_not_crash():
    """Caller may omit budget/used — banner must degrade gracefully."""
    lines = cli._format_stop_banner(reason="budget")
    assert len(lines) >= 1
    assert "token budget" in " ".join(lines).lower()


# ---------------------------------------------------------------------------
# _print_stop_banner (return value contract)
# ---------------------------------------------------------------------------


def test_print_stop_banner_returns_true_when_printed(capsys: pytest.CaptureFixture[str]):
    """Returns True iff a banner was actually printed — callers use this to
    decide whether to arm the TUI continue-affordance."""
    printed = cli._print_stop_banner(reason="max_steps", steps=5, step_cap=5)
    out = capsys.readouterr().out
    assert printed is True
    assert "tool-step cap" in out.lower()


def test_print_stop_banner_returns_false_for_done(capsys: pytest.CaptureFixture[str]):
    printed = cli._print_stop_banner(reason="done", steps=5)
    out = capsys.readouterr().out
    assert printed is False
    # Nothing printed for a clean finish.
    assert out == ""


# ---------------------------------------------------------------------------
# _is_placeholder_summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        "(reached step cap)",
        "(no output)",
        "(cancelled by user)",
        "(stopped: token budget of 2000000 reached at 2030587 tokens)",
        "  (Reached Step Cap)  ",  # case + whitespace insensitive
    ],
)
def test_placeholder_summaries_are_detected(summary: str):
    """The executor emits these stock strings when it has no real summary;
    showing them as the answer would be confusing — they must be filtered."""
    assert cli._is_placeholder_summary(summary) is True


@pytest.mark.parametrize(
    "summary",
    [
        "I've fixed the bug by editing foo.py:12.",
        "(reached step cap) and then some more",  # not exact
        "",
        "done",
        "The deployment succeeded.",
    ],
)
def test_real_summaries_pass_through(summary: str):
    """Real model output must NOT be filtered — only exact placeholders."""
    assert cli._is_placeholder_summary(summary) is False


# ---------------------------------------------------------------------------
# _set_suggest_continue + _REPL_STATE wiring
# ---------------------------------------------------------------------------


def test_suggest_continue_flag_roundtrip():
    """The flag is settable, defaults off, and lives in _REPL_STATE (where the
    TUI's _restore_input looks for it)."""
    original = cli._REPL_STATE.get("suggest_continue", False)
    try:
        cli._REPL_STATE["suggest_continue"] = False
        cli._set_suggest_continue(True)
        assert cli._REPL_STATE["suggest_continue"] is True
        cli._set_suggest_continue(False)
        assert cli._REPL_STATE["suggest_continue"] is False
    finally:
        cli._REPL_STATE["suggest_continue"] = original


def test_repl_state_has_suggest_continue_key():
    """The default _REPL_STATE dict must include the flag so the TUI can pop()
    it without a KeyError on the very first (pre-run) restore."""
    assert "suggest_continue" in cli._REPL_STATE
