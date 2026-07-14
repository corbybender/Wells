"""Tests for the deterministic recovery-hint registry (recovery_hints.py)."""

from __future__ import annotations

from wells import recovery_hints


def test_command_not_found_by_exit_code():
    assert recovery_hints.hint_for("$ pip install x\n[exit 127]", 127) is not None


def test_command_not_found_by_message_posix():
    assert recovery_hints.hint_for("/bin/sh: pip: command not found", 127) is not None


def test_command_not_found_by_message_windows():
    text = "The term 'pip' is not recognized as the name of a cmdlet..."
    assert recovery_hints.hint_for(text, 1) is not None


def test_pip_externally_managed():
    text = "error: externally-managed-environment"
    hint = recovery_hints.hint_for(text, 1)
    assert hint is not None
    assert "venv" in hint


def test_module_not_found():
    text = "ModuleNotFoundError: No module named 'tree_sitter'"
    hint = recovery_hints.hint_for(text, 1)
    assert hint is not None
    assert "interpreter" in hint


def test_port_in_use():
    text = "Error: listen EADDRINUSE: address already in use :::3000"
    assert recovery_hints.hint_for(text, 1) is not None


def test_no_match_returns_none():
    assert recovery_hints.hint_for("build succeeded, 0 errors", 0) is None


def test_unrelated_failure_returns_none():
    assert recovery_hints.hint_for("AssertionError: expected 2, got 3", 1) is None
