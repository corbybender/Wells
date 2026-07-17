"""User-scriptable hooks: run shell commands around tool calls and task
lifecycle events, defined in ``.wells/hooks.yaml``.

This is a different layer from :mod:`wells.rules`: rules are Wells' own
declarative, deterministic policy (block/confirm/warn/liability patterns
matched against tool calls). Hooks are arbitrary user shell scripts —
"run our internal linter after every edit", "notify Slack when a run
finishes", "block any goal mentioning production data" — the same role
Claude Code's PreToolUse/PostToolUse/UserPromptSubmit/Stop hooks play.

Events:
  * ``PreToolUse``  — before a tool dispatches. A nonzero exit BLOCKS the
    call; its output becomes the refusal reason shown to the model.
  * ``PostToolUse`` — after a tool dispatches (success or failure). Output
    (any exit code) is appended as a note to the model's next observation;
    never blocks (the call already happened).
  * ``UserPromptSubmit`` — before a submitted goal starts a run. A nonzero
    exit BLOCKS the run entirely; output becomes the refusal shown to the
    user. Wired at the top-level goal entry points (one-shot CLI, TUI
    orchestrate) — not every internal executor call (subagents, stepwise
    coder steps) re-fires it, since those aren't a *new* user prompt.
  * ``Stop`` — after a top-level run finishes. Observational only (the run
    is already over) — for notifications, logging, cleanup.

Each hook's command receives a JSON payload on stdin and a couple of
common fields as environment variables (``WELLS_HOOK_EVENT``,
``WELLS_HOOK_WORKSPACE``) for scripts that don't want to parse JSON.
Failures to load/run hooks are never fatal to the run they're observing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

HOOKS_FILE = Path(".wells") / "hooks.yaml"
_DEFAULT_TIMEOUT = 30.0
_MAX_OUTPUT_CHARS = 2000

_VALID_EVENTS = {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"}


@dataclass(frozen=True)
class HookDef:
    event: str
    command: str
    matcher: re.Pattern | None = None  # compiled regex on tool name; None = all
    timeout: float = _DEFAULT_TIMEOUT


def enabled() -> bool:
    return os.environ.get("WELLS_HOOKS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _hooks_path(workspace: str) -> Path:
    return Path(workspace) / HOOKS_FILE


def load_hooks(workspace: str) -> list[HookDef]:
    """Parse .wells/hooks.yaml; [] when absent, invalid, or disabled.

    Malformed entries are skipped individually rather than discarding the
    whole file — one typo shouldn't silence every other hook.
    """
    if not enabled():
        return []
    path = _hooks_path(workspace)
    if not path.is_file():
        return []
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    entries = raw.get("hooks") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []

    out: list[HookDef] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        event = str(e.get("event", "")).strip()
        command = str(e.get("command", "")).strip()
        if event not in _VALID_EVENTS or not command:
            continue
        matcher_raw = e.get("matcher")
        matcher = None
        if matcher_raw:
            try:
                matcher = re.compile(str(matcher_raw))
            except re.error:
                continue
        try:
            timeout = float(e.get("timeout", _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        out.append(HookDef(event=event, command=command, matcher=matcher, timeout=timeout))
    return out


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class HookOutcome:
    ok: bool          # False only for PreToolUse/UserPromptSubmit blocks
    output: str        # combined stdout+stderr, truncated


def _run_hook(hook: HookDef, payload: dict, workspace: str) -> HookOutcome | None:
    """Run one hook's command; None on any execution failure (never raises)."""
    env = dict(os.environ)
    env["WELLS_HOOK_EVENT"] = payload.get("event", "")
    env["WELLS_HOOK_WORKSPACE"] = workspace
    tool_name = payload.get("tool_name")
    if tool_name:
        env["WELLS_HOOK_TOOL"] = tool_name
    try:
        proc = subprocess.run(
            hook.command,
            shell=True,
            cwd=workspace,
            input=json.dumps(payload, default=str),
            capture_output=True,
            text=True,
            timeout=hook.timeout,
            env=env,
        )
    except Exception:
        return None
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if len(out) > _MAX_OUTPUT_CHARS:
        out = out[:_MAX_OUTPUT_CHARS] + f"\n… (truncated, {len(out) - _MAX_OUTPUT_CHARS} more chars)"
    return HookOutcome(ok=proc.returncode == 0, output=out)


def _matching(hooks: list[HookDef], event: str, tool_name: str | None) -> list[HookDef]:
    return [
        h for h in hooks
        if h.event == event and (h.matcher is None or (tool_name and h.matcher.search(tool_name)))
    ]


# ---------------------------------------------------------------------------
# Per-event entry points
# ---------------------------------------------------------------------------


def fire_pre_tool_use(
    workspace: str, tool_name: str, tool_args: dict
) -> tuple[bool, str]:
    """Returns (allowed, reason). reason is non-empty only when blocked."""
    hooks = _matching(load_hooks(workspace), "PreToolUse", tool_name)
    for h in hooks:
        outcome = _run_hook(
            h, {"event": "PreToolUse", "tool_name": tool_name, "tool_args": tool_args},
            workspace,
        )
        if outcome is None:
            continue  # a broken hook must not silently block real work
        if not outcome.ok:
            reason = outcome.output or f"blocked by hook: {h.command}"
            return False, reason
    return True, ""


def fire_post_tool_use(
    workspace: str, tool_name: str, tool_args: dict, *, ok: bool, output_preview: str
) -> list[str]:
    """Returns note strings to append to the model's next observation."""
    hooks = _matching(load_hooks(workspace), "PostToolUse", tool_name)
    notes: list[str] = []
    for h in hooks:
        outcome = _run_hook(
            h, {
                "event": "PostToolUse", "tool_name": tool_name, "tool_args": tool_args,
                "ok": ok, "output_preview": output_preview[:500],
            },
            workspace,
        )
        if outcome and outcome.output:
            notes.append(f"[HOOK {tool_name}]: {outcome.output}")
    return notes


def fire_user_prompt_submit(workspace: str, prompt: str) -> tuple[bool, str]:
    """Returns (allowed, reason). reason is non-empty only when blocked."""
    hooks = _matching(load_hooks(workspace), "UserPromptSubmit", None)
    for h in hooks:
        outcome = _run_hook(h, {"event": "UserPromptSubmit", "prompt": prompt}, workspace)
        if outcome is None:
            continue
        if not outcome.ok:
            reason = outcome.output or f"blocked by hook: {h.command}"
            return False, reason
    return True, ""


def fire_stop(workspace: str, *, stopped_reason: str, summary: str) -> None:
    """Fire-and-forget: the run already ended, nothing left to block."""
    hooks = _matching(load_hooks(workspace), "Stop", None)
    for h in hooks:
        _run_hook(
            h, {"event": "Stop", "stopped_reason": stopped_reason, "summary": summary[:2000]},
            workspace,
        )
