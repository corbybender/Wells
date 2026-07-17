"""Tests for user-defined slash commands (.wells/commands/*.md)."""

from __future__ import annotations

from pathlib import Path

from wells import cli, commands


# ---------------------------------------------------------------------------
# discover / expand
# ---------------------------------------------------------------------------


def test_discover_empty_when_no_commands_dir(tmp_path: Path):
    assert commands.discover(str(tmp_path)) == {}


def test_discover_finds_md_files_by_stem(tmp_path: Path):
    d = tmp_path / ".wells" / "commands"
    d.mkdir(parents=True)
    (d / "review-pr.md").write_text("Review the current diff.\n", encoding="utf-8")
    (d / "add-tests.md").write_text("Add tests for $ARGUMENTS.\n", encoding="utf-8")
    (d / "notes.txt").write_text("not markdown, ignored\n", encoding="utf-8")

    found = commands.discover(str(tmp_path))
    assert set(found.keys()) == {"review-pr", "add-tests"}
    assert found["review-pr"] == d / "review-pr.md"


def test_expand_substitutes_arguments():
    out = commands.expand("Add tests for $ARGUMENTS covering edge cases.", "auth.py")
    assert out == "Add tests for auth.py covering edge cases."


def test_expand_substitutes_multiple_occurrences():
    out = commands.expand("$ARGUMENTS and again $ARGUMENTS", "X")
    assert out == "X and again X"


def test_expand_appends_args_when_no_placeholder():
    out = commands.expand("Review the current diff.", "focus on security")
    assert out == "Review the current diff.\n\nfocus on security"


def test_expand_no_args_no_placeholder_returns_template_unchanged():
    assert commands.expand("Review the current diff.", "") == "Review the current diff."


def test_expand_literal_dollar_sign_in_replacement_not_reinterpreted():
    """re.sub treats backslash-escapes in the replacement specially unless a
    plain callable is used — $100 in the user's args must survive verbatim."""
    out = commands.expand("Cost analysis: $ARGUMENTS", "totaling $100")
    assert out == "Cost analysis: totaling $100"


# ---------------------------------------------------------------------------
# resolve (name lookup + builtin shadowing guard)
# ---------------------------------------------------------------------------


def test_resolve_returns_none_for_plain_text(tmp_path: Path):
    assert commands.resolve(str(tmp_path), "not a command", builtin_names=set()) is None


def test_resolve_returns_none_when_no_matching_file(tmp_path: Path):
    assert commands.resolve(str(tmp_path), "/nonexistent", builtin_names=set()) is None


def test_resolve_never_shadows_a_builtin(tmp_path: Path):
    d = tmp_path / ".wells" / "commands"
    d.mkdir(parents=True)
    (d / "help.md").write_text("this should never fire\n", encoding="utf-8")
    result = commands.resolve(str(tmp_path), "/help", builtin_names={"help"})
    assert result is None


def test_resolve_expands_custom_command_with_args(tmp_path: Path):
    d = tmp_path / ".wells" / "commands"
    d.mkdir(parents=True)
    (d / "add-tests.md").write_text("Add tests for $ARGUMENTS.\n", encoding="utf-8")
    result = commands.resolve(str(tmp_path), "/add-tests auth.py", builtin_names={"help"})
    assert result == "Add tests for auth.py.\n"


def test_resolve_case_insensitive_command_name(tmp_path: Path):
    d = tmp_path / ".wells" / "commands"
    d.mkdir(parents=True)
    (d / "review-pr.md").write_text("Review it.\n", encoding="utf-8")
    result = commands.resolve(str(tmp_path), "/Review-PR", builtin_names=set())
    assert result == "Review it.\n"


# ---------------------------------------------------------------------------
# list_commands (for /help)
# ---------------------------------------------------------------------------


def test_list_commands_uses_first_nonblank_line_as_description(tmp_path: Path):
    d = tmp_path / ".wells" / "commands"
    d.mkdir(parents=True)
    (d / "review-pr.md").write_text(
        "\n\n# Review the current PR\nDo a thorough review.\n", encoding="utf-8"
    )
    out = commands.list_commands(str(tmp_path))
    assert out == [("/review-pr", "Review the current PR")]


def test_list_commands_empty_when_no_dir(tmp_path: Path):
    assert commands.list_commands(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# cli.builtin_command_names + /help integration
# ---------------------------------------------------------------------------


def test_builtin_command_names_covers_help_and_undo():
    names = cli.builtin_command_names()
    assert "help" in names and "undo" in names
    assert all(not n.startswith("/") for n in names)


def test_print_help_lists_custom_commands(tmp_path: Path, monkeypatch, capsys):
    from wells import config as _config

    d = tmp_path / ".wells" / "commands"
    d.mkdir(parents=True)
    (d / "deploy.md").write_text("# Deploy to staging\nRun the deploy script.\n",
                                  encoding="utf-8")
    monkeypatch.setattr(_config, "WORKSPACE_ROOT", str(tmp_path))
    cli._print_help()
    out = capsys.readouterr().out
    assert "/deploy" in out
    assert "Deploy to staging" in out
