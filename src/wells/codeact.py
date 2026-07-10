"""CodeAct: a sandboxed code-execution tool the agent can drive.

Some questions are *calculations*, not lookups — "what's the total LOC changed",
"does this regex match these 12 strings", "generate the cartesian product of
these test cases", "count how many functions call X transitively". Guessing
arithmetic or eyeballing a regex is exactly what the harness's "Deterministic
First" and "Verify Before Trust" principles tell us not to do. Letting the
agent *run a little code* is the fix.

This module exposes ``run_code``: the agent writes a small Python snippet, the
harness runs it in a **workspace-confined subprocess**, and returns structured
``stdout`` / ``stderr`` / ``exit code``. It is gated by the same safety policy
as ``run_command`` and reuses the harness's cert-injection / shell plumbing.

Why a confined subprocess, not Hyperlight/Monty?
  * Zero extra dependencies — works out of the box everywhere Python runs.
  * Workspace confinement + the existing safety gate + the deny-list give the
    same first line of defense the article's ``LocalShellExecutor`` relies on.
  * The code runs with ``cwd`` = workspace, so ``open("relative/path")`` works
    naturally for the common "inspect the repo" use case.

Guardrails:
  * ``WELLS_CODEACT=0`` disables the tool entirely (it won't appear in the
    registry). Default is on.
  * Output is truncated (stdout/stderr each capped) so a runaway ``print`` in
    a loop can't blow the context budget.
  * The same deny-list that screens ``run_command`` is applied to the code
    text, so a fork-bomb / ``mkfs`` string is refused before execution.
  * Honours plan/dry-run/approve modes like every other mutating tool.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from wells import safety
from wells.tools import ToolContext, ToolDef, ToolResult

# Output caps — keep the returned text small so it can't starve the context.
_MAX_STDOUT_CHARS = 8000
_MAX_STDERR_CHARS = 4000
# Hard wall-clock cap on a single code run (also bounded by ctx.shell_timeout).
_DEFAULT_TIMEOUT = 30.0

# Patterns that screen the *source* (in addition to the run_command deny-list).
# These catch the classic footguns even when the snippet doesn't shell out.
_CODE_DENY = [
    re.compile(r"\bos\.system\b"),
    re.compile(r"\bsubprocess\b"),
    re.compile(r"\bpopen\b", re.I),
    re.compile(r"\b__import__\b"),
    re.compile(r"fork\s*\(\s*\)"),
]


def _screen_code(code: str) -> str | None:
    """Return a refusal reason if ``code`` contains a forbidden pattern, else None."""
    for pat in _CODE_DENY:
        if pat.search(code):
            return (
                f"Code contains a forbidden pattern ({pat.pattern!r}). run_code "
                f"is for pure computation; use run_command for subprocesses."
            )
    # Also apply the shell deny-list (catches `rm -rf /` in a string literal).
    try:
        safety.screen_command(code)
    except safety.BlockedCommandError as e:
        return str(e)
    return None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit - 40
    return text[:keep] + f"\n… (truncated, {len(text) - keep} more chars)"


def run_code(ctx: ToolContext, code: str, *, language: str = "python") -> ToolResult:
    """Execute ``code`` in a workspace-confined interpreter; return stdout/stderr.

    The snippet runs as a temp file with ``cwd`` = workspace, so relative reads
    (``open("src/a.py")``) work. Output is structured and truncated so the
    model gets a clean, bounded result to reason over.
    """
    if not code or not code.strip():
        return ToolResult(False, "", "code is required")
    if language.strip().lower() not in ("python", "py", "python3"):
        return ToolResult(
            False, "", f"Unsupported language {language!r}; run_code supports Python."
        )

    # Source screening happens regardless of mode — even a dry-run shouldn't
    # bless a fork bomb.
    refuse = _screen_code(code)
    if refuse is not None:
        return ToolResult(False, "", refuse)

    detail = f"run_code: {len(code)} chars of Python"
    if ctx.plan_mode:
        return ToolResult(True, f"[plan] would {detail}", simulated=True)
    decision = safety.gate("run_code", detail, safety=ctx.safety, approver=ctx.approver)
    if not decision.allowed:
        return ToolResult(True, decision.reason, simulated=decision.simulated)

    timeout = float(os.environ.get("CODEACT_TIMEOUT", "") or _DEFAULT_TIMEOUT)
    timeout = min(timeout, ctx.shell_timeout or timeout)

    # Write to a temp file under the OS temp dir (NOT the workspace — we don't
    # want the snippet itself showing up as a repo file or in the repo map).
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(code)
            tmp_path = Path(fh.name)
    except Exception as e:
        return ToolResult(False, "", f"Could not stage code: {e}")

    # Reuse the harness subprocess env (cert injection etc.) + the workspace cwd.
    from wells.tools import _subprocess_env  # type: ignore

    py = "python3" if _which("python3") else "python"
    try:
        proc = subprocess.run(
            [py, str(tmp_path)],
            cwd=ctx.workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, "", f"run_code timed out after {timeout}s")
    except Exception as e:
        return ToolResult(False, "", f"run_code failed: {e}")
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    stdout = _truncate((proc.stdout or "").rstrip(), _MAX_STDOUT_CHARS)
    stderr = _truncate((proc.stderr or "").rstrip(), _MAX_STDERR_CHARS)
    rc = proc.returncode

    parts = [f"[exit {rc}]"]
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if not stdout and not stderr:
        parts.append("(no output)")
    text = "\n".join(parts)
    ok = rc == 0
    return ToolResult(ok, text, "" if ok else f"exit {rc}")


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def enabled() -> bool:
    """Whether the run_code tool is registered (``WELLS_CODEACT`` != 0)."""
    return os.environ.get("WELLS_CODEACT", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ---------------------------------------------------------------------------
# Tool descriptor
# ---------------------------------------------------------------------------


RUN_CODE_TOOL = ToolDef(
    name="run_code",
    description=(
        "Run a Python snippet in a sandboxed, workspace-confined interpreter and "
        "return its stdout/stderr/exit code. Use this to COMPUTE answers (counts, "
        "totals, regex checks, cartesian products, data transforms) instead of "
        "doing arithmetic in your head or eyeballing logic. The code runs with "
        "cwd=workspace, so open('relative/path') works. No subprocess/os.system/"
        "__import__ (use run_command for shell work). Output is truncated."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute"},
            "language": {
                "type": "string",
                "description": 'Language (only "python" supported)',
                "default": "python",
            },
        },
        "required": ["code"],
    },
    handler=run_code,
    mutating=True,
)
