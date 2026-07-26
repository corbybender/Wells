"""Global, cross-project user memory: durable preferences that follow you
to every workspace, not just the one you're standing in.

``AGENTS.md`` (:mod:`wells.memory`) already accumulates facts about *one
repo* — conventions, gotchas, key files — and lives in that repo. This
module is the complementary, orthogonal piece: facts about *you* — how you
like to work, corrections you've given more than once, standing
preferences — that should apply no matter which project you point Wells
at. The same category of thing Claude Code's own auto-memory keeps across
conversations.

  * ``AGENTS.md``    — accumulated facts about THIS repo; per-workspace.
  * User memory      — accumulated facts about YOU; global (``~/.wells/memory/``).

A memory entry is a small ``<name>.md`` file (YAML front-matter + a short
body) directly under ``~/.wells/memory/`` (flat — no name/description/body
split loaded on demand the way skills are; these are meant to be short
enough to inject directly, like AGENTS.md), e.g.::

    ---
    name: terse-commit-messages
    type: feedback
    description: Prefers short, why-focused commit messages over detailed ones.
    ---

    Keep commit subject lines under 50 chars. Only add a body when the
    "why" isn't obvious from the diff — never restate what changed.

Front-matter fields:
  * ``name``        — the file's identifier (also its filename, sans ``.md``).
                       Defaults to the filename if omitted.
  * ``description`` — one line; shown in the index alongside the body.
  * ``type``         — free-form tag for your own organization (conventional
                       values: ``user``, ``feedback``, ``reference`` — but
                       not enforced; this is categorization, not a toggle
                       that changes behavior the way a persona's ``tools:``
                       does).

Unlike skills/personas, entries are injected **directly and in full** into
every system prompt (not progressively disclosed) — the whole point is
that these are small, standing facts the agent should always have, the
same way AGENTS.md is always-on. A total-size budget
(``_MAX_INJECT_CHARS``) caps what actually gets injected if the store
grows large, keeping the oldest-by-mtime entries and truncating with a
notice rather than silently dropping something.

``WELLS_USER_MEMORY=0`` disables the whole feature.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_FRONT_MATTER_RE = re.compile(
    r"\A\s*---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)\Z", re.DOTALL
)
_KV_RE = re.compile(r"^\s*([A-Za-z_][\w\-]*)\s*:\s*(.*?)\s*$", re.M)
_MAX_INJECT_CHARS = 3000
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _memory_dir() -> Path:
    """``~/.wells/memory/`` — overridable via ``WELLS_USER_MEMORY_DIR`` (tests)."""
    override = os.environ.get("WELLS_USER_MEMORY_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".wells" / "memory"


def enabled() -> bool:
    """Whether global user memory is on (``WELLS_USER_MEMORY`` != 0)."""
    return os.environ.get("WELLS_USER_MEMORY", "1").strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class MemoryEntry:
    """One discoverable global memory entry."""

    name: str
    description: str
    type: str
    body: str
    path: Path
    mtime: float = 0.0

    def index_line(self) -> str:
        desc = self.description.strip().replace("\n", " ")
        if len(desc) > 110:
            desc = desc[:107] + "…"
        tag = f"[{self.type}] " if self.type else ""
        return f"- {tag}{self.name}: {desc}"

    def as_block(self) -> str:
        header = f"### {self.name}" + (f" ({self.type})" if self.type else "")
        return f"{header}\n{self.body.strip()}"


@dataclass
class MemoryIndex:
    entries: list[MemoryEntry] = field(default_factory=list)

    def by_name(self, name: str) -> MemoryEntry | None:
        target = name.strip().lower()
        for e in self.entries:
            if e.name.lower() == target:
                return e
        return None

    def is_empty(self) -> bool:
        return not self.entries


# ---------------------------------------------------------------------------
# Discovery + parsing (no caching -- this is a small, rarely-changing local
# directory read on every prompt build; a stat-based cache isn't worth the
# staleness risk right after a /memory edit)
# ---------------------------------------------------------------------------


def _parse(content: str, path: Path) -> MemoryEntry | None:
    m = _FRONT_MATTER_RE.match(content)
    fm: dict[str, str] = {}
    body = content
    if m:
        for k, v in _KV_RE.findall(m.group("fm")):
            fm[k.lower()] = v.strip().strip("\"'")
        body = m.group("body").strip()
    name = (fm.get("name") or path.stem).strip()
    if not name:
        return None
    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = 0.0
    return MemoryEntry(
        name=name,
        description=fm.get("description") or "",
        type=fm.get("type") or "",
        body=body,
        path=path,
        mtime=mtime,
    )


def memories() -> MemoryIndex:
    """All discovered global memory entries, oldest-first by mtime."""
    if not enabled():
        return MemoryIndex()
    root = _memory_dir()
    if not root.is_dir():
        return MemoryIndex()
    entries: list[MemoryEntry] = []
    try:
        files = sorted(root.glob("*.md"))
    except Exception:
        return MemoryIndex()
    for f in files:
        if f.name.upper() == "MEMORY.md".upper():
            continue  # reserved: a future plain-index file, not an entry
        try:
            entry = _parse(f.read_text(encoding="utf-8", errors="replace"), f)
        except Exception:
            entry = None
        if entry:
            entries.append(entry)
    entries.sort(key=lambda e: e.mtime)
    return MemoryIndex(entries=entries)


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def memory_block() -> str:
    """Full injected block: every entry's body, oldest-first, budget-capped.

    Unlike skills/personas' name-only index, bodies are included directly —
    these are meant to be short standing facts, not on-demand procedures.
    """
    idx = memories()
    if idx.is_empty():
        return ""
    parts: list[str] = []
    used = 0
    truncated = False
    for e in idx.entries:
        block = e.as_block()
        if used + len(block) > _MAX_INJECT_CHARS and parts:
            truncated = True
            break
        parts.append(block)
        used += len(block)
    body = "\n\n".join(parts)
    if truncated:
        body += "\n\n… (older memory entries omitted; see /memory list)"
    return f"=== USER MEMORY (global, applies to every project) ===\n{body}\n=== END USER MEMORY ===\n"


def inject_into_prompt(prompt: str) -> str:
    """Prepend the user-memory block to ``prompt`` (no-op when empty)."""
    block = memory_block()
    if not block:
        return prompt
    return f"{block}\n\n{prompt}"


# ---------------------------------------------------------------------------
# Mutation operations (create / read-raw / update / delete)
# ---------------------------------------------------------------------------
# Used by the /memory menu (CLI + TUI modal) -- same shape as skills.py's,
# minus the safety gate: this is a global, per-user file outside any
# workspace, so plan/approve/dryrun (which govern actions *inside* a
# workspace) don't apply to it.


def validate_name(name: str) -> str | None:
    n = (name or "").strip().lower()
    if not n:
        return "Memory entry name is required."
    if not _NAME_RE.match(n):
        return (
            "Memory entry name must be lowercase letters, digits, and hyphens "
            "(e.g. 'terse-commit-messages')."
        )
    if n.startswith("-") or n.endswith("-") or "--" in n:
        return "Memory entry name must not start/end with a hyphen or contain consecutive hyphens."
    if len(n) > 64:
        return "Memory entry name must be 64 characters or fewer."
    return None


def entry_file_path(name: str) -> Path | None:
    entry = memories().by_name(name)
    return entry.path if entry else None


def read_memory_raw(name: str) -> tuple[bool, str]:
    path = entry_file_path(name)
    if path is None:
        return False, f"Unknown memory entry {name!r}."
    try:
        return True, path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, f"Could not read {path}: {e}"


def create_memory(name: str, description: str, entry_type: str, body: str) -> tuple[bool, str]:
    """Create a new global memory entry at ``~/.wells/memory/<name>.md``."""
    err = validate_name(name)
    if err:
        return False, err
    name = name.strip().lower()
    if memories().by_name(name) is not None:
        return False, f"A memory entry named {name!r} already exists. Use /memory edit to change it."
    root = _memory_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(_format_memory_file(name, description, entry_type, body), encoding="utf-8")
    return True, f"Created memory entry {name!r} at {path}"


def update_memory(
    name: str,
    *,
    description: str | None = None,
    entry_type: str | None = None,
    body: str | None = None,
) -> tuple[bool, str]:
    """Update an existing entry's description/type/body. Unpassed fields are kept."""
    name = (name or "").strip().lower()
    entry = memories().by_name(name)
    if entry is None:
        return False, f"Unknown memory entry {name!r}."
    new_desc = description.strip() if description is not None else entry.description
    new_type = entry_type.strip() if entry_type is not None else entry.type
    new_body = body if body is not None else entry.body
    entry.path.write_text(_format_memory_file(name, new_desc, new_type, new_body), encoding="utf-8")
    return True, f"Updated memory entry {name!r}."


def delete_memory(name: str) -> tuple[bool, str]:
    name = (name or "").strip().lower()
    entry = memories().by_name(name)
    if entry is None:
        return False, f"Unknown memory entry {name!r}."
    try:
        entry.path.unlink()
    except Exception as e:
        return False, f"Could not delete {entry.path}: {e}"
    return True, f"Deleted memory entry {name!r}."


def _format_memory_file(name: str, description: str, entry_type: str, body: str) -> str:
    desc = (description or "").strip().replace("\n", " ")
    etype = (entry_type or "").strip()
    body_text = (body or "").strip()
    fm = f"---\nname: {name}\ndescription: {desc}\ntype: {etype}\n---\n\n"
    return fm + body_text + ("\n" if body_text and not body_text.endswith("\n") else "")
