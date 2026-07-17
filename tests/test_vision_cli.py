"""Tests for vision wiring at the CLI/TUI layer: /image, /paste-image, and
the --image flag threading into _run_goal / the graph's AgentState."""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from wells import cli, config, main


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def _clean_pending_images():
    cli._REPL_STATE["pending_images"] = []
    yield
    cli._REPL_STATE["pending_images"] = []


@pytest.fixture
def png_path(tmp_path: Path) -> Path:
    p = tmp_path / "shot.png"
    p.write_bytes(_PNG_BYTES)
    return p


# ---------------------------------------------------------------------------
# /image
# ---------------------------------------------------------------------------


def test_image_command_stages_a_valid_path(png_path: Path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(png_path.parent))
    cli.handle_slash_command(f"/image {png_path}")
    assert cli._REPL_STATE["pending_images"] == [str(png_path)]


def test_image_command_rejects_bad_path(tmp_path: Path, capsys):
    cli.handle_slash_command(f"/image {tmp_path / 'nope.png'}")
    assert cli._REPL_STATE["pending_images"] == []
    assert "not found" in capsys.readouterr().out


def test_image_command_clear(png_path: Path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(png_path.parent))
    cli.handle_slash_command(f"/image {png_path}")
    cli.handle_slash_command("/image clear")
    assert cli._REPL_STATE["pending_images"] == []


def test_image_command_resolves_relative_to_workspace(png_path: Path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(png_path.parent))
    cli.handle_slash_command(f"/image {png_path.name}")
    assert cli._REPL_STATE["pending_images"] == [str(png_path)]


def test_image_command_no_arg_shows_usage(capsys):
    cli.handle_slash_command("/image")
    assert "Usage" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# /paste-image
# ---------------------------------------------------------------------------


def test_paste_image_stages_when_clipboard_has_image(tmp_path: Path):
    fake_path = tmp_path / "paste.png"
    fake_path.write_bytes(_PNG_BYTES)
    with patch("wells.vision.paste_clipboard_image", return_value=fake_path):
        cli.handle_slash_command("/paste-image")
    assert cli._REPL_STATE["pending_images"] == [str(fake_path)]


def test_paste_image_no_clipboard_image_stages_nothing(capsys):
    with patch("wells.vision.paste_clipboard_image", return_value=None):
        cli.handle_slash_command("/paste-image")
    assert cli._REPL_STATE["pending_images"] == []
    assert "No image found" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _run_task consumes and clears staged images
# ---------------------------------------------------------------------------


def test_run_task_consumes_and_clears_pending_images(png_path: Path, monkeypatch):
    cli._REPL_STATE["pending_images"] = [str(png_path)]
    captured_state = {}

    class _FakeApp:
        def stream(self, state, config=None, **kw):
            captured_state.update(state)
            return iter([])  # no graph events; _run_task just needs `images` observed

    with (
        patch("wells.sessions.new_session_id", return_value="x"),
        patch.object(cli, "_save_undo_checkpoint"),
        patch.object(cli, "console"),
    ):
        cli._run_task("do it", {}, _FakeApp(), [])
    # The agent_state passed to app.stream() must carry the staged image...
    assert captured_state.get("images") == [str(png_path)]
    # ...and it's a one-shot attachment: cleared so it doesn't leak into
    # every subsequent task for the rest of the session.
    assert cli._REPL_STATE["pending_images"] == []


# ---------------------------------------------------------------------------
# main.py --image flag
# ---------------------------------------------------------------------------


def test_main_image_flag_reaches_run_goal(monkeypatch, png_path: Path):
    monkeypatch.setattr(sys, "argv", ["wells", "--image", str(png_path), "look at this"])
    captured = {}

    def _fake_run_goal(goal, **kw):
        captured["goal"] = goal
        captured["image_paths"] = kw.get("image_paths")

    with (
        patch("wells.setup.first_run_setup"),
        patch.object(main, "_run_goal", side_effect=_fake_run_goal),
    ):
        main.main()
    assert captured == {"goal": "look at this", "image_paths": [str(png_path)]}


def test_main_image_flag_repeatable(monkeypatch, png_path: Path, tmp_path: Path):
    second = tmp_path / "second.png"
    second.write_bytes(_PNG_BYTES)
    monkeypatch.setattr(
        sys, "argv",
        ["wells", "--image", str(png_path), "--image", str(second), "compare"],
    )
    captured = {}

    def _fake_run_goal(goal, **kw):
        captured["image_paths"] = kw.get("image_paths")

    with (
        patch("wells.setup.first_run_setup"),
        patch.object(main, "_run_goal", side_effect=_fake_run_goal),
    ):
        main.main()
    assert captured["image_paths"] == [str(png_path), str(second)]


def test_run_goal_rejects_bad_image_before_starting(tmp_path: Path, capsys):
    with patch.object(main, "_ensure_model_configured", return_value=True):
        with pytest.raises(SystemExit) as ei:
            main._run_goal(
                "x", output_format="json", image_paths=[str(tmp_path / "nope.png")]
            )
    assert ei.value.code == 2
    assert "not found" in capsys.readouterr().out
