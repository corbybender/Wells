"""Tests for Reflexion (self-improvement #1): capture, dedup, load, retrieve, inject."""

from __future__ import annotations

from pathlib import Path

import pytest

from wells import reflections


@pytest.fixture(autouse=True)
def _auto_safety(monkeypatch):
    """Capture writes go through the safety gate; force auto so they proceed."""
    monkeypatch.setenv("HARNESS_SAFETY", "auto")
    monkeypatch.setenv("WELLS_REFLECTIONS", "1")
    monkeypatch.setenv("WELLS_REFLECTIONS_EMBED", "0")
    yield


# ---------------------------------------------------------------------------
# Failure detection + capture
# ---------------------------------------------------------------------------


def test_capture_on_test_failure(tmp_path: Path):
    state = {
        "goal": "wire up the payment webhook",
        "tests_passed": False,
        "test_results": "FAILED tests/test_pay.py::test_sig - AssertionError: bad sig",
        "review_complete": False,
    }
    path = reflections.capture_from_state(state, str(tmp_path))
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "## Reflections" in text
    assert "test_failure" in text
    assert "payment webhook" in text
    assert "bad sig" in text
    # signature fingerprint embedded for dedup
    assert "<!-- sig:" in text


def test_capture_on_review_rejection(tmp_path: Path):
    state = {
        "goal": "add caching layer",
        "tests_passed": True,
        "review_complete": False,
        "review_result": "DECISION: INCOMPLETE\nThe cache key ignores the tenant id.",
    }
    path = reflections.capture_from_state(state, str(tmp_path))
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "review_rejection" in text
    # The "DECISION:" noise is stripped from the synthesized critique.
    assert "DECISION:" not in text.split("Evidence:")[0]
    assert "tenant id" in text


def test_infra_failure_not_captured(tmp_path: Path):
    """A reviewer that crashed (review_error) is not a lesson about the repo."""
    state = {
        "goal": "x",
        "review_complete": False,
        "review_error": True,
        "review_result": "API key expired",
    }
    assert reflections.capture_from_state(state, str(tmp_path)) is None
    assert not (tmp_path / ".wells" / "reflections.md").exists()


def test_clean_run_not_captured(tmp_path: Path):
    state = {"goal": "x", "review_complete": True, "tests_passed": True,
             "implementation_steps": "did the thing"}
    assert reflections.capture_from_state(state, str(tmp_path)) is None


def test_dry_run_not_captured(tmp_path: Path):
    state = {"goal": "x", "tests_passed": False, "test_results": "boom"}
    assert reflections.capture_from_state(state, str(tmp_path), dry=True) is None


def test_disabled_not_captured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WELLS_REFLECTIONS", "0")
    state = {"goal": "x", "tests_passed": False, "test_results": "boom"}
    assert reflections.capture_from_state(state, str(tmp_path)) is None
    assert not reflections.enabled()


def test_dryrun_safety_gate_blocks_write(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HARNESS_SAFETY", "dryrun")
    state = {"goal": "x", "tests_passed": False, "test_results": "boom"}
    assert reflections.capture_from_state(state, str(tmp_path)) is None
    assert not (tmp_path / ".wells" / "reflections.md").exists()


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_duplicate_failure_recorded_once(tmp_path: Path):
    state = {
        "goal": "fix the flaky queue test",
        "tests_passed": False,
        "test_results": "FAILED test_queue::test_order - ordering race",
    }
    reflections.capture_from_state(state, str(tmp_path))
    second = reflections.capture_from_state(state, str(tmp_path))
    assert second is None  # identical signature → skipped
    items = reflections.load(str(tmp_path))
    assert len(items) == 1


def test_distinct_failures_both_recorded(tmp_path: Path):
    reflections.capture_from_state(
        {"goal": "fix queue test", "tests_passed": False,
         "test_results": "FAILED test_queue - race"},
        str(tmp_path),
    )
    reflections.capture_from_state(
        {"goal": "fix auth test", "tests_passed": False,
         "test_results": "FAILED test_auth - bad token"},
        str(tmp_path),
    )
    items = reflections.load(str(tmp_path))
    assert len(items) == 2
    goals = {r.goal for r in items}
    assert "fix queue test" in goals and "fix auth test" in goals


# ---------------------------------------------------------------------------
# Load + parse round-trip
# ---------------------------------------------------------------------------


def test_load_returns_empty_when_absent(tmp_path: Path):
    assert reflections.load(str(tmp_path)) == []


def test_load_parses_fields(tmp_path: Path):
    reflections.capture_from_state(
        {"goal": "do the thing", "tests_passed": False, "test_results": "FAIL boom"},
        str(tmp_path),
    )
    items = reflections.load(str(tmp_path))
    assert len(items) == 1
    r = items[0]
    assert r.failure_type == "test_failure"
    assert r.goal == "do the thing"
    assert "boom" in r.evidence
    assert len(r.signature) == 12


# ---------------------------------------------------------------------------
# Retrieval + injection
# ---------------------------------------------------------------------------


def test_retrieve_ranks_relevant_reflection_first(tmp_path: Path):
    reflections.capture_from_state(
        {"goal": "configure redis caching", "tests_passed": False,
         "test_results": "FAILED test_cache - connection refused"},
        str(tmp_path),
    )
    reflections.capture_from_state(
        {"goal": "set up pdf export", "tests_passed": False,
         "test_results": "FAILED test_pdf - font missing"},
        str(tmp_path),
    )
    top = reflections.retrieve(str(tmp_path), "add redis caching for the api")
    assert top
    assert top[0].goal == "configure redis caching"


def test_retrieve_drops_irrelevant(tmp_path: Path):
    reflections.capture_from_state(
        {"goal": "pdf export font", "tests_passed": False,
         "test_results": "FAILED test_pdf"},
        str(tmp_path),
    )
    # Completely unrelated goal → no token overlap → empty.
    assert reflections.retrieve(str(tmp_path), "zzzz qqqq xxxx") == []


def test_retrieve_respects_k(tmp_path: Path):
    for i in range(5):
        reflections.capture_from_state(
            {"goal": f"feature number {i}", "tests_passed": False,
             "test_results": f"FAILED test_{i}"},
            str(tmp_path),
        )
    top = reflections.retrieve(str(tmp_path), "feature number", k=2)
    assert len(top) <= 2


def test_inject_into_prompt_prepends_block(tmp_path: Path):
    reflections.capture_from_state(
        {"goal": "redis caching", "tests_passed": False,
         "test_results": "FAILED test_cache"},
        str(tmp_path),
    )
    out = reflections.inject_into_prompt("BASE PROMPT", str(tmp_path), "redis caching")
    assert out.startswith("PAST REFLECTIONS")
    assert "BASE PROMPT" in out
    assert "test_failure" in out


def test_inject_noop_when_empty(tmp_path: Path):
    assert reflections.inject_into_prompt("BASE", str(tmp_path), "anything") == "BASE"


def test_inject_noop_when_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WELLS_REFLECTIONS", "0")
    reflections.capture_from_state(  # capture itself is a no-op when disabled
        {"goal": "x", "tests_passed": False, "test_results": "boom"},
        str(tmp_path),
    )
    assert reflections.inject_into_prompt("BASE", str(tmp_path), "x") == "BASE"


# ---------------------------------------------------------------------------
# Compaction + clear
# ---------------------------------------------------------------------------


def test_compaction_keeps_file_under_cap(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(reflections, "MAX_FILE_BYTES", 800)
    for i in range(20):
        reflections.capture_from_state(
            {"goal": f"goal number {i} with enough words to differ",
             "tests_passed": False, "test_results": f"FAILED test_{i} detailed output"},
            str(tmp_path),
        )
    path = tmp_path / ".wells" / "reflections.md"
    assert path.stat().st_size <= 1600  # roughly capped (allow compaction overhead)
    assert "compacted" in path.read_text(encoding="utf-8")


def test_clear_removes_file(tmp_path: Path):
    reflections.capture_from_state(
        {"goal": "x", "tests_passed": False, "test_results": "boom"},
        str(tmp_path),
    )
    path = tmp_path / ".wells" / "reflections.md"
    assert path.is_file()
    assert reflections.clear(str(tmp_path)) is True
    assert not path.exists()


def test_clear_when_absent_is_noop(tmp_path: Path):
    assert reflections.clear(str(tmp_path)) is True


# ---------------------------------------------------------------------------
# CLI handler (/reflections) — empty-arg safety
# ---------------------------------------------------------------------------


def test_cli_reflections_bare_arg_does_not_crash_when_empty(tmp_path: Path, monkeypatch, capsys):
    """Regression: `/reflections` with no subcommand used to IndexError on an
    empty workspace because `"".split()[0]` raises. It must instead show the
    'No reflections yet' message."""
    import wells.cli as cli_mod

    monkeypatch.setattr(cli_mod.config, "WORKSPACE_ROOT", str(tmp_path))
    cli_mod._handle_reflections("")  # bare /reflections
    out = capsys.readouterr().out
    assert "No reflections" in out


def test_cli_reflections_list_when_empty(tmp_path: Path, monkeypatch, capsys):
    import wells.cli as cli_mod

    monkeypatch.setattr(cli_mod.config, "WORKSPACE_ROOT", str(tmp_path))
    cli_mod._handle_reflections("list")
    out = capsys.readouterr().out
    assert "No reflections" in out
