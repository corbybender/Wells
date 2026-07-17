"""Tests for the user-scriptable hooks system (.wells/hooks.yaml)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from wells import hooks


def _write_hooks_yaml(workspace: Path, text: str) -> None:
    d = workspace / ".wells"
    d.mkdir(parents=True, exist_ok=True)
    (d / "hooks.yaml").write_text(text, encoding="utf-8")


# A tiny cross-platform "hook" is just a python -c invocation via sys.executable,
# so these tests don't depend on a shell being bash vs cmd vs pwsh.
_PY = sys.executable.replace("\\", "/")


# ---------------------------------------------------------------------------
# load_hooks
# ---------------------------------------------------------------------------


def test_load_hooks_empty_when_no_file(tmp_path: Path):
    assert hooks.load_hooks(str(tmp_path)) == []


def test_load_hooks_parses_valid_entries(tmp_path: Path):
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: PreToolUse
    matcher: "run_command"
    command: "{_PY} -c pass"
    timeout: 5
  - event: Stop
    command: "{_PY} -c pass"
""")
    found = hooks.load_hooks(str(tmp_path))
    assert len(found) == 2
    assert found[0].event == "PreToolUse"
    assert found[0].matcher.search("run_command")
    assert found[0].timeout == 5
    assert found[1].event == "Stop" and found[1].matcher is None


def test_load_hooks_skips_invalid_entries_keeps_valid_ones(tmp_path: Path):
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: NotARealEvent
    command: "{_PY} -c pass"
  - event: PreToolUse
    command: ""
  - not_a_dict_entry
  - event: PreToolUse
    command: "{_PY} -c pass"
""")
    found = hooks.load_hooks(str(tmp_path))
    assert len(found) == 1


def test_load_hooks_malformed_yaml_returns_empty(tmp_path: Path):
    _write_hooks_yaml(tmp_path, "hooks: [this is not: valid: yaml: [")
    assert hooks.load_hooks(str(tmp_path)) == []


def test_load_hooks_disabled_via_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WELLS_HOOKS", "0")
    _write_hooks_yaml(tmp_path, f"hooks:\n  - event: Stop\n    command: \"{_PY} -c pass\"\n")
    assert hooks.load_hooks(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# PreToolUse: block on nonzero exit
# ---------------------------------------------------------------------------


def test_pre_tool_use_allows_when_no_hooks(tmp_path: Path):
    allowed, reason = hooks.fire_pre_tool_use(str(tmp_path), "write_file", {"path": "x"})
    assert allowed is True and reason == ""


def test_pre_tool_use_blocks_on_nonzero_exit(tmp_path: Path):
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: PreToolUse
    command: "{_PY} -c \\"import sys; print('nope, not allowed'); sys.exit(1)\\""
""")
    allowed, reason = hooks.fire_pre_tool_use(str(tmp_path), "run_command", {"command": "rm x"})
    assert allowed is False
    assert "nope, not allowed" in reason


def test_pre_tool_use_matcher_filters_by_tool_name(tmp_path: Path):
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: PreToolUse
    matcher: "run_command"
    command: "{_PY} -c \\"import sys; sys.exit(1)\\""
""")
    blocked_allowed, _ = hooks.fire_pre_tool_use(str(tmp_path), "run_command", {})
    unaffected_allowed, _ = hooks.fire_pre_tool_use(str(tmp_path), "read_file", {})
    assert blocked_allowed is False
    assert unaffected_allowed is True


def test_pre_tool_use_allows_on_zero_exit(tmp_path: Path):
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: PreToolUse
    command: "{_PY} -c pass"
""")
    allowed, reason = hooks.fire_pre_tool_use(str(tmp_path), "read_file", {})
    assert allowed is True and reason == ""


def test_pre_tool_use_receives_json_payload_on_stdin(tmp_path: Path):
    """The hook script inspects its own stdin — proves the payload actually
    carries the tool name/args, not just fires blind."""
    script = tmp_path / "check.py"
    script.write_text(
        "import sys, json\n"
        "data = json.loads(sys.stdin.read())\n"
        "sys.exit(0 if data.get('tool_name') == 'write_file' and "
        "data.get('tool_args', {}).get('path') == 'secret.txt' else 1)\n",
        encoding="utf-8",
    )
    _write_hooks_yaml(tmp_path, f'hooks:\n  - event: PreToolUse\n    command: "{_PY} {str(script).replace(chr(92), "/")}"\n')
    allowed, _ = hooks.fire_pre_tool_use(
        str(tmp_path), "write_file", {"path": "secret.txt"}
    )
    assert allowed is True  # script only exits 0 when it saw the right payload


# ---------------------------------------------------------------------------
# PostToolUse: never blocks, notes get appended
# ---------------------------------------------------------------------------


def test_post_tool_use_never_blocks_even_on_nonzero_exit(tmp_path: Path):
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: PostToolUse
    command: "{_PY} -c \\"import sys; print('lint warning'); sys.exit(1)\\""
""")
    notes = hooks.fire_post_tool_use(
        str(tmp_path), "write_file", {"path": "x.py"}, ok=True, output_preview="wrote it"
    )
    assert any("lint warning" in n for n in notes)  # ran and captured output...
    # ...but there's no return value that could block anything — the
    # function signature itself (list[str], not a tuple) proves it.


def test_post_tool_use_no_notes_when_hook_prints_nothing(tmp_path: Path):
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: PostToolUse
    command: "{_PY} -c pass"
""")
    notes = hooks.fire_post_tool_use(str(tmp_path), "read_file", {}, ok=True, output_preview="")
    assert notes == []


# ---------------------------------------------------------------------------
# UserPromptSubmit
# ---------------------------------------------------------------------------


def test_user_prompt_submit_blocks_dangerous_goal(tmp_path: Path):
    script = tmp_path / "gate.py"
    script.write_text(
        "import sys, json\n"
        "data = json.loads(sys.stdin.read())\n"
        "sys.exit(1 if 'production' in data.get('prompt', '') else 0)\n",
        encoding="utf-8",
    )
    _write_hooks_yaml(tmp_path, f'hooks:\n  - event: UserPromptSubmit\n    command: "{_PY} {str(script).replace(chr(92), "/")}"\n')
    blocked_allowed, reason = hooks.fire_user_prompt_submit(
        str(tmp_path), "delete the production database"
    )
    safe_allowed, _ = hooks.fire_user_prompt_submit(str(tmp_path), "fix the typo in README")
    assert blocked_allowed is False
    assert safe_allowed is True


# ---------------------------------------------------------------------------
# Stop: fire-and-forget, never raises
# ---------------------------------------------------------------------------


def test_stop_hook_runs_without_raising_regardless_of_exit_code(tmp_path: Path):
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: Stop
    command: "{_PY} -c \\"import sys; sys.exit(1)\\""
""")
    hooks.fire_stop(str(tmp_path), stopped_reason="complete", summary="did the thing")  # no raise


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_hook_execution_failure_never_blocks(tmp_path: Path, monkeypatch):
    """If the hook process itself can't be started (missing shell, OS-level
    failure), _run_hook returns None and PreToolUse must fall open, not
    silently brick the harness on a broken hook config."""
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: PreToolUse
    command: "{_PY} -c pass"
""")
    monkeypatch.setattr(
        hooks.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("could not start process")),
    )
    allowed, reason = hooks.fire_pre_tool_use(str(tmp_path), "read_file", {})
    assert allowed is True


def test_hook_timeout_does_not_raise(tmp_path: Path):
    _write_hooks_yaml(tmp_path, f"""
hooks:
  - event: PreToolUse
    command: "{_PY} -c \\"import time; time.sleep(5)\\""
    timeout: 0.1
""")
    allowed, _ = hooks.fire_pre_tool_use(str(tmp_path), "read_file", {})
    assert allowed is True  # timeout -> outcome is None -> never blocks
