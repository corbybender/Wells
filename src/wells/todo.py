"""update_todos: a model-driven task breakdown, rendered live in the TUI.

The existing pipeline breadcrumb (``wells.control.RunControl.stages``) shows
*structural* position — which fixed graph node (indexer/planner/coder/...)
is running. It says nothing about what the coder actually decided to do
inside a long task. This tool is the complementary, dynamic piece: the
model declares its own subtask breakdown for the current task and checks
items off as it goes, the same category of transparency Claude Code's own
TodoWrite gives — "here's what I'm actually doing," not just "here's what
stage I'm in."

Design:
  * One tool, ``update_todos``, that replaces the whole list each call (the
    model resends the full list with updated statuses — no partial-update
    API to keep in sync).
  * State lives in :class:`wells.control.RunControl` (``set_todos``/
    ``todos``), the same shared channel the TUI already polls for activity/
    progress/stages — no new plumbing between the executor thread and the
    UI thread.
  * At most one item may be ``in_progress`` at a time (mirrors Claude Code's
    own constraint) — keeps "what's the model doing right now" unambiguous
    for the panel to highlight, rather than accepting an anything-goes list
    that can't be rendered as a single point of progress.
  * Cleared at the start of every run (``RunControl.reset()``) — a todo list
    belongs to one task, not the whole session.
"""

from __future__ import annotations

import os

from wells.control import CONTROL
from wells.tools import ToolContext, ToolDef, ToolResult

_VALID_STATUSES = ("pending", "in_progress", "completed")


def enabled() -> bool:
    """Whether the update_todos tool is registered (``WELLS_TODO`` != 0)."""
    return os.environ.get("WELLS_TODO", "1").strip().lower() not in ("0", "false", "no", "off")


def _validate(items: list) -> str | None:
    """Return an error message if ``items`` is malformed, else None."""
    if not isinstance(items, list):
        return "items must be a list"
    in_progress_count = 0
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return f"item {i} must be an object with 'content' and 'status'"
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            return f"item {i}: 'content' is required and must be non-empty text"
        status = item.get("status")
        if status not in _VALID_STATUSES:
            return f"item {i}: 'status' must be one of {_VALID_STATUSES}, got {status!r}"
        if status == "in_progress":
            in_progress_count += 1
    if in_progress_count > 1:
        return (
            f"only one item may be 'in_progress' at a time (got {in_progress_count}) -- "
            f"finish or defer one before starting another"
        )
    return None


def update_todos(ctx: ToolContext, items: list) -> ToolResult:
    """Replace the current todo list; used to render live progress in the TUI."""
    err = _validate(items or [])
    if err:
        return ToolResult(False, "", err)
    clean = [
        {"content": item["content"].strip(), "status": item["status"]}
        for item in items
    ]
    CONTROL.set_todos(clean)
    if not clean:
        return ToolResult(True, "Todo list cleared.")
    counts = {s: sum(1 for i in clean if i["status"] == s) for s in _VALID_STATUSES}
    summary = ", ".join(f"{counts[s]} {s}" for s in _VALID_STATUSES if counts[s])
    return ToolResult(True, f"Todo list updated: {len(clean)} item(s) ({summary}).")


UPDATE_TODOS_TOOL = ToolDef(
    name="update_todos",
    description=(
        "Declare or update your task breakdown for the CURRENT task — rendered "
        "live in the user's status panel so they can see what you're actually "
        "doing, not just which stage you're in. Resend the FULL list each call "
        "(this replaces the whole thing, not a partial patch). Use for any "
        "task with 3+ distinct steps; skip it for trivial one-shot requests. "
        "Exactly one item may be 'in_progress' at a time -- mark it 'completed' "
        "before starting the next. Call with an empty list to clear when done."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "One subtask, in your own words"},
                        "status": {
                            "type": "string",
                            "enum": list(_VALID_STATUSES),
                        },
                    },
                    "required": ["content", "status"],
                },
                "description": "The full task list, in order. Empty list clears it.",
            },
        },
        "required": ["items"],
    },
    handler=update_todos,
    mutating=False,
)
