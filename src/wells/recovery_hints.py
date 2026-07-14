"""Deterministic recovery hints for common tool-use failures.

Small/local models often abandon a productive approach after a single
failure they don't recognize, even when the fix is mechanical and needs no
real reasoning — see the 2026-07-14 stuck-loop investigation (Wells
tools.py's `pip install` → "command not found" → the model gave up on the
shell instead of trying `pip3`). Instead of leaving the model to interpret
raw stderr from scratch every time, match known failure signatures here and
attach an actionable hint directly to the observation.

Add new patterns as they're discovered in the wild — one entry per known
gotcha, the cheapest correct fix. This is the "list of common failures" to
grow over time rather than re-deriving fixes ad hoc in tools.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RecoveryHint:
    name: str  # short id, for logs/tests
    match: Callable[[str, int | None], bool]
    message: str


def _contains(*needles: str) -> Callable[[str, int | None], bool]:
    def _m(text: str, code: int | None) -> bool:
        low = text.lower()
        return any(n in low for n in needles)

    return _m


HINTS: list[RecoveryHint] = [
    RecoveryHint(
        name="command_not_found",
        match=lambda text, code: code == 127
        or _contains("command not found", "is not recognized")(text, code),
        message=(
            "the shell could not find that executable on PATH. Common fixes: "
            "use the versioned form (pip3/python3 instead of pip/python), "
            "invoke it as a module of an interpreter that IS on PATH (e.g. "
            "`python3 -m pip install ...`), or run `command -v <name>` / "
            "`which <name>` (POSIX) or `where <name>` (Windows) to check "
            "what's actually available before assuming it isn't installed "
            "at all."
        ),
    ),
    RecoveryHint(
        name="pip_externally_managed",
        match=_contains("externally-managed-environment"),
        message=(
            "this Python install refuses system-wide pip installs (PEP "
            "668). Either create/use a venv (`python3 -m venv .venv && "
            "source .venv/bin/activate`) or, only if a venv genuinely isn't "
            "an option, add `--break-system-packages` to the pip command."
        ),
    ),
    RecoveryHint(
        name="module_not_found",
        match=_contains("modulenotfounderror", "no module named"),
        message=(
            "a Python import failed because the package isn't installed in "
            "the interpreter actually being used. Install it (pip/uv add) "
            "into the SAME interpreter that will run the script — check "
            "with `python3 -m pip show <package>` if unsure which one that "
            "is."
        ),
    ),
    RecoveryHint(
        name="npm_missing_modules",
        match=_contains("cannot find module", "err_module_not_found"),
        message=(
            "a Node import failed — dependencies are likely not installed. "
            "Run `npm install` (or `yarn`/`pnpm install` if that's the "
            "project's lockfile) before retrying."
        ),
    ),
    RecoveryHint(
        name="port_in_use",
        match=_contains("address already in use", "eaddrinuse"),
        message=(
            "the port is already bound by another process (often a "
            "previous run of this same server that never exited). Find and "
            "stop it first, or pick a different port, rather than retrying "
            "the identical command."
        ),
    ),
    RecoveryHint(
        name="not_a_git_repo",
        match=_contains("not a git repository"),
        message=(
            "this directory isn't a git repo (or you're above/outside it). "
            "Check the working directory (`pwd`) matches where the repo "
            "actually lives before retrying — running `git init` is almost "
            "never the right fix here."
        ),
    ),
    RecoveryHint(
        name="permission_denied",
        match=_contains("permission denied", "eacces"),
        message=(
            "the OS refused this operation for permission reasons. On a "
            "workspace file this usually means a wrong path (e.g. writing "
            "outside the workspace) rather than something to fix with sudo "
            "— double-check the path before retrying with elevated "
            "privileges."
        ),
    ),
]


def hint_for(output: str, returncode: int | None = None) -> str | None:
    """Return the first matching recovery hint's message, or None."""
    for h in HINTS:
        if h.match(output, returncode):
            return h.message
    return None
