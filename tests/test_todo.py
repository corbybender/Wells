"""Tests for the update_todos tool: validation, CONTROL state, registration,
and the TUI panel rendering it drives."""

from __future__ import annotations

import pytest

from wells import tools
from wells.control import CONTROL


@pytest.fixture(autouse=True)
def _reset_control():
    CONTROL.reset()
    yield
    CONTROL.reset()


@pytest.fixture
def ctx() -> tools.ToolContext:
    return tools.ToolContext(workspace=".")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_update_todos_is_registered():
    names = [t.name for t in tools.ALL_TOOLS]
    assert "update_todos" in names


def test_update_todos_is_read_only_registered_too():
    """Purely in-memory display state, no workspace mutation -- available to
    read-only investigations (planner, read-only subagents) too."""
    names = [t.name for t in tools.READ_TOOLS]
    assert "update_todos" in names


def test_disabled_via_env(monkeypatch):
    from wells import todo

    monkeypatch.setenv("WELLS_TODO", "0")
    assert not todo.enabled()


# ---------------------------------------------------------------------------
# Validation + CONTROL state
# ---------------------------------------------------------------------------


def test_update_sets_control_state(ctx):
    items = [
        {"content": "step one", "status": "completed"},
        {"content": "step two", "status": "in_progress"},
        {"content": "step three", "status": "pending"},
    ]
    r = tools.dispatch("update_todos", {"items": items}, ctx)
    assert r.ok, r.error
    assert CONTROL.todos() == items


def test_empty_list_clears(ctx):
    tools.dispatch("update_todos", {"items": [{"content": "x", "status": "pending"}]}, ctx)
    assert CONTROL.todos()
    r = tools.dispatch("update_todos", {"items": []}, ctx)
    assert r.ok
    assert CONTROL.todos() == []


def test_rejects_two_in_progress(ctx):
    items = [
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
    ]
    r = tools.dispatch("update_todos", {"items": items}, ctx)
    assert not r.ok
    assert "one item" in r.error
    # A rejected call must not mutate the existing state.
    assert CONTROL.todos() == []


def test_zero_in_progress_is_fine(ctx):
    items = [
        {"content": "a", "status": "pending"},
        {"content": "b", "status": "completed"},
    ]
    r = tools.dispatch("update_todos", {"items": items}, ctx)
    assert r.ok


def test_rejects_missing_content(ctx):
    r = tools.dispatch("update_todos", {"items": [{"status": "pending"}]}, ctx)
    assert not r.ok


def test_rejects_empty_content(ctx):
    r = tools.dispatch("update_todos", {"items": [{"content": "  ", "status": "pending"}]}, ctx)
    assert not r.ok


def test_rejects_bad_status(ctx):
    r = tools.dispatch(
        "update_todos", {"items": [{"content": "x", "status": "bogus"}]}, ctx
    )
    assert not r.ok


def test_content_is_stripped(ctx):
    tools.dispatch("update_todos", {"items": [{"content": "  x  ", "status": "pending"}]}, ctx)
    assert CONTROL.todos()[0]["content"] == "x"


def test_summary_counts_in_output(ctx):
    items = [
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "completed"},
        {"content": "c", "status": "in_progress"},
    ]
    r = tools.dispatch("update_todos", {"items": items}, ctx)
    assert "2 completed" in r.output
    assert "1 in_progress" in r.output


def test_reset_clears_todos(ctx):
    tools.dispatch("update_todos", {"items": [{"content": "x", "status": "pending"}]}, ctx)
    assert CONTROL.todos()
    CONTROL.reset()
    assert CONTROL.todos() == []


# ---------------------------------------------------------------------------
# TUI panel rendering
# ---------------------------------------------------------------------------


def test_info_panel_renders_todo_list():
    import asyncio
    from unittest.mock import patch

    from wells.tui import WellsApp

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)):
                CONTROL.set_todos([
                    {"content": "Read the config file", "status": "completed"},
                    {"content": "Fix the bug in parser", "status": "in_progress"},
                    {"content": "Run the tests", "status": "pending"},
                ])
                panel = app.query_one("InfoPanel")
                text = panel._build()
                assert "todo" in text
                assert "Read the config file" in text
                assert "Fix the bug in parser" in text
                assert "Run the tests" in text

    asyncio.run(body())


def test_info_panel_omits_todo_section_when_empty():
    import asyncio
    from unittest.mock import patch

    from wells.tui import WellsApp

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)):
                panel = app.query_one("InfoPanel")
                text = panel._build()
                assert "[bold]todo[/bold]" not in text

    asyncio.run(body())
