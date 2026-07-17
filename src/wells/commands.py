"""User-defined slash commands: ``.wells/commands/*.md`` become custom
``/<name>`` commands, the same convention Claude Code uses for
``.claude/commands/*.md``.

Any markdown file dropped in the directory becomes an invocable slash
command whose body is used as the task text, with ``$ARGUMENTS``
substituted for whatever follows the command name on the input line. This
lets a team codify its own repeated prompts ("/review-pr", "/add-tests",
"/release-notes") without touching Wells' source or its hardcoded
:data:`wells.cli.SLASH_COMMANDS` catalog.

A custom command can never shadow a builtin (``/help``, ``/undo``, ...) —
the caller always checks builtin names first via ``resolve()``'s
``builtin_names`` parameter.
"""

from __future__ import annotations

import re
from pathlib import Path

COMMANDS_SUBDIR = Path(".wells") / "commands"
_ARGS_RE = re.compile(r"\$ARGUMENTS\b")


def commands_dir(workspace: str) -> Path:
    return Path(workspace) / COMMANDS_SUBDIR


def discover(workspace: str) -> dict[str, Path]:
    """Map command name (lowercase, no leading slash) -> its .md file path."""
    d = commands_dir(workspace)
    if not d.is_dir():
        return {}
    out: dict[str, Path] = {}
    try:
        for p in sorted(d.glob("*.md")):
            if p.is_file():
                out[p.stem.lower()] = p
    except Exception:
        return {}
    return out


def expand(template: str, args: str) -> str:
    """Substitute ``$ARGUMENTS`` in ``template`` with ``args``.

    When the template has no ``$ARGUMENTS`` placeholder but the user typed
    trailing args anyway, they're appended rather than silently dropped —
    a command author who forgot the placeholder still gets useful behavior
    instead of the user's input vanishing with no explanation.
    """
    if _ARGS_RE.search(template):
        return _ARGS_RE.sub(lambda _m: args, template)
    if args:
        return f"{template.rstrip()}\n\n{args}"
    return template


def resolve(workspace: str, command_line: str, *, builtin_names: set[str]) -> str | None:
    """Return the expanded task text for ``command_line``, or ``None``.

    ``None`` means: not a slash command, names a builtin (never shadowed),
    or no matching file was found under ``.wells/commands/``.
    """
    parts = command_line.strip().split(None, 1)
    if not parts or not parts[0].startswith("/"):
        return None
    name = parts[0][1:].lower()
    if not name or name in builtin_names:
        return None
    path = discover(workspace).get(name)
    if path is None:
        return None
    try:
        template = path.read_text(encoding="utf-8")
    except Exception:
        return None
    args = parts[1].strip() if len(parts) > 1 else ""
    return expand(template, args)


def list_commands(workspace: str) -> list[tuple[str, str]]:
    """(name, first-line-description) pairs for /help and autocomplete.

    The description is the file's first non-blank line with any leading
    markdown heading marker stripped, truncated — a one-line summary
    without requiring authors to add frontmatter.
    """
    out: list[tuple[str, str]] = []
    for name, path in sorted(discover(workspace).items()):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            text = ""
        desc = ""
        for line in text.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                desc = stripped[:80]
                break
        out.append((f"/{name}", desc or "(custom command)"))
    return out
