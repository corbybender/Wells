"""Custom subagent personas: discoverable, user-authored specialist roles.

A *persona* is a small ``PERSONA.md`` file (YAML front-matter + markdown
body) that packages a subagent identity: a system prompt establishing its
expertise/voice/constraints, plus which toolset tier it gets. Analogous to
Claude Code's ``.claude/agents/*.md`` custom subagent types — but unlike
skills (whose body is progressively loaded into the *parent's* context via
``load_skill``), a persona's body never touches the parent's context at
all: only its name + one-line description show up in the parent's system
prompt, and the full system prompt is handed to the *subagent* run itself
when ``bg_start(persona=name, ...)`` picks it. Even cheaper than skills.

A ``PERSONA.md`` is YAML-front-matter + markdown body, e.g.::

    ---
    name: security-reviewer
    description: Reviews a diff for injection/auth/secrets issues before merge.
    tools: readonly
    ---

    You are a senior application-security reviewer. For every file you're
    shown, look specifically for: injection (SQL/command/template), broken
    auth/session handling, hardcoded secrets, and unsafe deserialization.
    Report findings as `file:line — issue — why it's exploitable`. Say
    "no issues found" plainly if there aren't any — don't invent findings
    to seem thorough.

Front-matter fields:
  * ``name``        — the identifier passed as ``bg_start(persona=...)``.
                       Defaults to the folder name.
  * ``description`` — one line, always visible in the parent's system
                       prompt index (so the parent knows when to use it).
  * ``tools``        — ``readonly`` | ``exec`` | ``full`` (default ``full``).
                       Still capped by the chosen ``role``: ``role=research``
                       is always read-only regardless of what a persona
                       requests — the role controls mutation/isolation
                       mechanics, the persona controls voice/expertise/tool
                       *ceiling* within that.

Resolution mirrors :mod:`wells.skills` exactly: personas are discovered from
the workspace ``agents/`` directory and any extra paths in
``WELLS_AGENTS_PATHS`` (a path-list). Name collisions resolve first-wins in
that order. ``WELLS_AGENTS=0`` disables discovery entirely (``bg_start``'s
``persona`` argument then always reports "unknown").
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from wells import safety

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_FRONT_MATTER_RE = re.compile(
    r"\A\s*---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)\Z", re.DOTALL
)
_KV_RE = re.compile(r"^\s*([A-Za-z_][\w\-]*)\s*:\s*(.*?)\s*$", re.M)
_VALID_TOOLSETS = ("readonly", "exec", "full")
# Max chars for the up-front persona index injected into the parent's prompt.
_MAX_INDEX_CHARS = 1200


@dataclass(frozen=True)
class Persona:
    """One discoverable subagent persona."""

    name: str
    description: str
    system_prompt: str
    toolset: str
    path: Path

    def index_line(self) -> str:
        """One-line summary for the always-on index: ``name — description``."""
        desc = self.description.strip().replace("\n", " ")
        if len(desc) > 110:
            desc = desc[:107] + "…"
        return f"- {self.name} (tools={self.toolset}): {desc}"


@dataclass
class PersonaIndex:
    """The set of discovered personas for a workspace."""

    personas: list[Persona] = field(default_factory=list)
    roots: list[Path] = field(default_factory=list)

    def by_name(self, name: str) -> Persona | None:
        target = name.strip().lower()
        for p in self.personas:
            if p.name.lower() == target:
                return p
        return None

    def is_empty(self) -> bool:
        return not self.personas


# ---------------------------------------------------------------------------
# Discovery + parsing
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """Whether the persona provider is on (``WELLS_AGENTS`` != 0)."""
    return os.environ.get("WELLS_AGENTS", "1").strip().lower() not in ("0", "false", "no", "off")


def _agent_paths(workspace: str | None = None) -> list[Path]:
    """Roots to search for ``agents/<name>/PERSONA.md`` (or loose ``PERSONA.md``).

      1. ``<workspace>/agents/`` (the conventional location — mirrors ``skills/``)
      2. Any extra dir in ``WELLS_AGENTS_PATHS`` (os.pathsep-separated)

    Non-existent dirs are silently skipped. Duplicates removed.
    """
    roots: list[Path] = []
    try:
        ws = safety.workspace_root(workspace)
        roots.append(ws / "agents")
    except Exception:
        pass
    extra = os.environ.get("WELLS_AGENTS_PATHS", "").strip()
    if extra:
        for piece in re.split(rf"[{re.escape(os.pathsep)}]", extra):
            p = piece.strip()
            if p:
                roots.append(Path(p))
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        try:
            rp = r.expanduser().resolve()
        except Exception:
            continue
        if not rp.exists() or str(rp) in seen:
            continue
        seen.add(str(rp))
        out.append(rp)
    return out


def _parse(content: str, path: Path) -> Persona | None:
    """Parse a PERSONA.md file into a :class:`Persona`. None on failure."""
    m = _FRONT_MATTER_RE.match(content)
    fm: dict[str, str] = {}
    body = content
    if m:
        for k, v in _KV_RE.findall(m.group("fm")):
            fm[k.lower()] = v.strip().strip("\"'")
        body = m.group("body").strip()
    name = (fm.get("name") or path.parent.name).strip()
    if not name:
        return None
    desc = fm.get("description") or ""
    toolset = (fm.get("tools") or "full").strip().lower()
    if toolset not in _VALID_TOOLSETS:
        toolset = "full"
    return Persona(name=name, description=desc, system_prompt=body, toolset=toolset, path=path)


def _discover_in(root: Path) -> list[Persona]:
    """Find all personas under ``root``: ``<root>/<name>/PERSONA.md`` or a
    single loose ``<root>/PERSONA.md``."""
    out: list[Persona] = []
    if not root.is_dir():
        return out
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except Exception:
        return out
    for child in children:
        if child.is_dir():
            pf = child / "PERSONA.md"
            if pf.is_file():
                try:
                    persona = _parse(pf.read_text(encoding="utf-8", errors="replace"), pf)
                except Exception:
                    persona = None
                if persona:
                    out.append(persona)
        elif child.is_file() and child.name.upper() == "PERSONA.MD":
            try:
                persona = _parse(child.read_text(encoding="utf-8", errors="replace"), child)
            except Exception:
                persona = None
            if persona:
                out.append(persona)
    return out


@lru_cache(maxsize=16)
def _index_cached(key: str) -> PersonaIndex:
    """Cache a PersonaIndex by a stable key (joined roots + their mtimes)."""
    roots: list[Path] = []
    for entry in key.split("\n") if key else []:
        path_str = entry.split("||", 1)[0] if "||" in entry else entry
        if path_str:
            roots.append(Path(path_str))
    personas_list: list[Persona] = []
    for r in roots:
        personas_list.extend(_discover_in(r))
    seen: set[str] = set()
    dedup: list[Persona] = []
    for p in personas_list:
        if p.name.lower() in seen:
            continue
        seen.add(p.name.lower())
        dedup.append(p)
    return PersonaIndex(personas=dedup, roots=roots)


def _roots_key(roots: list[Path]) -> str:
    """Cache key embedding mtimes so edits invalidate the cache (``||`` never
    appears in a path, so it splits back out unambiguously on every OS)."""
    parts: list[str] = []
    for r in roots:
        try:
            stamp = str(int(r.stat().st_mtime))
        except Exception:
            stamp = "?"
        parts.append(f"{r}||{stamp}")
    return "\n".join(parts)


def personas_for(workspace: str | None = None) -> PersonaIndex:
    """Return the discovered :class:`PersonaIndex` for ``workspace``."""
    if not enabled():
        return PersonaIndex()
    roots = _agent_paths(workspace)
    return _index_cached(_roots_key(roots))


def clear_cache() -> None:
    """Drop cached indexes (after a persona is added/edited at runtime)."""
    _index_cached.cache_clear()


# ---------------------------------------------------------------------------
# Prompt injection (index only — the full system_prompt never enters the
# parent's context, only the subagent's, when bg_start(persona=...) picks it)
# ---------------------------------------------------------------------------


def persona_index_block(workspace: str | None = None) -> str:
    """The always-on index: one line per available persona, for the PARENT's
    system prompt. Returns "" when no personas are configured."""
    idx = personas_for(workspace)
    if idx.is_empty():
        return ""
    lines = [p.index_line() for p in idx.personas]
    block = "\n".join(lines)
    if len(block) > _MAX_INDEX_CHARS:
        block = block[:_MAX_INDEX_CHARS] + "\n… (persona list truncated)"
    return (
        "=== AVAILABLE SUBAGENT PERSONAS (bg_start persona=<name>) ===\n"
        f"{block}\n"
        "Pass persona=<name> to bg_start for a specialized subagent identity "
        "(its full instructions load into that subagent only, not here). "
        "Omit persona for the default research/fix behavior.\n"
        "=== END PERSONAS ===\n"
    )


def inject_into_prompt(prompt: str, workspace: str | None = None) -> str:
    """Append the persona index to ``prompt`` (no-op when no personas exist)."""
    block = persona_index_block(workspace)
    if not block:
        return prompt
    return f"{prompt}\n\n{block}"


# ---------------------------------------------------------------------------
# Mutation operations (create / read-raw / update / delete)
# ---------------------------------------------------------------------------
# Used by the /agents menu (CLI + TUI modal) — same shape as skills.py's.

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_name(name: str) -> str | None:
    """Return an error message if ``name`` is invalid, else None."""
    n = (name or "").strip().lower()
    if not n:
        return "Persona name is required."
    if not _NAME_RE.match(n):
        return (
            "Persona name must be lowercase letters, digits, and hyphens "
            "(e.g. 'security-reviewer')."
        )
    if n.startswith("-") or n.endswith("-") or "--" in n:
        return "Persona name must not start/end with a hyphen or contain consecutive hyphens."
    if len(n) > 64:
        return "Persona name must be 64 characters or fewer."
    return None


def validate_toolset(toolset: str) -> str | None:
    """Return an error message if ``toolset`` isn't a valid tier, else None."""
    if toolset.strip().lower() not in _VALID_TOOLSETS:
        return f"tools must be one of: {', '.join(_VALID_TOOLSETS)}"
    return None


def _agents_dir(workspace: str | None = None) -> Path:
    """The primary personas directory: ``<workspace>/agents/``."""
    root = safety.workspace_root(workspace)
    return root / "agents"


def persona_file_path(name: str, workspace: str | None = None) -> Path | None:
    """Return the ``PERSONA.md`` path for ``name``, or None if not found."""
    persona = personas_for(workspace).by_name(name)
    return persona.path if persona else None


def read_persona_raw(name: str, workspace: str | None = None) -> tuple[bool, str]:
    """Return ``(ok, raw_text_or_error)`` — the full PERSONA.md file content."""
    path = persona_file_path(name, workspace)
    if path is None:
        return False, f"Unknown persona {name!r}."
    try:
        return True, path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, f"Could not read {path}: {e}"


def create_persona(
    name: str,
    description: str,
    system_prompt: str,
    toolset: str = "full",
    workspace: str | None = None,
) -> tuple[bool, str]:
    """Create a new persona under ``<workspace>/agents/<name>/PERSONA.md``.

    Returns ``(ok, message)``. Honours the safety gate; fails if the persona
    already exists (use :func:`update_persona` to change it).
    """
    err = validate_name(name)
    if err:
        return False, err
    name = name.strip().lower()
    toolset = (toolset or "full").strip().lower()
    err = validate_toolset(toolset)
    if err:
        return False, err

    if personas_for(workspace).by_name(name) is not None:
        return False, f"A persona named {name!r} already exists. Use /agents edit to change it."

    detail = f"create persona {name!r}"
    decision = safety.gate("write_file", detail)
    if not decision.allowed:
        return False, decision.reason

    persona_dir = _agents_dir(workspace) / name
    persona_dir.mkdir(parents=True, exist_ok=True)
    path = persona_dir / "PERSONA.md"
    path.write_text(_format_persona_file(name, description, toolset, system_prompt), encoding="utf-8")
    clear_cache()
    return True, f"Created persona {name!r} at {path}"


def update_persona(
    name: str,
    workspace: str | None = None,
    *,
    description: str | None = None,
    toolset: str | None = None,
    system_prompt: str | None = None,
) -> tuple[bool, str]:
    """Update an existing persona's description/tools/system prompt.

    Only the fields you pass are changed; the rest is preserved. Returns
    ``(ok, message)``. Honours the safety gate.
    """
    name = (name or "").strip().lower()
    persona = personas_for(workspace).by_name(name)
    if persona is None:
        return False, f"Unknown persona {name!r}."

    if toolset is not None:
        err = validate_toolset(toolset)
        if err:
            return False, err

    detail = f"update persona {name!r}"
    decision = safety.gate("write_file", detail)
    if not decision.allowed:
        return False, decision.reason

    new_desc = description.strip() if description is not None else persona.description
    new_tools = toolset.strip().lower() if toolset is not None else persona.toolset
    new_prompt = system_prompt if system_prompt is not None else persona.system_prompt

    path = persona.path
    path.write_text(_format_persona_file(name, new_desc, new_tools, new_prompt), encoding="utf-8")
    clear_cache()
    return True, f"Updated persona {name!r}."


def delete_persona(name: str, workspace: str | None = None) -> tuple[bool, str]:
    """Delete a persona (its folder + PERSONA.md). Returns ``(ok, message)``.

    Honours the safety gate. Refuses to delete personas outside the
    workspace ``agents/`` tree (e.g. ones loaded from ``WELLS_AGENTS_PATHS``).
    """
    name = (name or "").strip().lower()
    persona = personas_for(workspace).by_name(name)
    if persona is None:
        return False, f"Unknown persona {name!r}."

    ws_agents = _agents_dir(workspace).resolve()
    try:
        persona_dir = persona.path.parent.resolve()
        persona_dir.relative_to(ws_agents)
    except (ValueError, OSError):
        return (
            False,
            f"Persona {name!r} is not under {ws_agents} — it may be loaded from "
            "WELLS_AGENTS_PATHS and can't be deleted from here. Remove its "
            "folder manually.",
        )

    detail = f"delete persona {name!r} ({persona_dir})"
    decision = safety.gate("write_file", detail)
    if not decision.allowed:
        return False, decision.reason

    try:
        import shutil

        shutil.rmtree(persona_dir)
    except Exception as e:
        return False, f"Could not delete {persona_dir}: {e}"
    clear_cache()
    return True, f"Deleted persona {name!r}."


def _format_persona_file(name: str, description: str, toolset: str, system_prompt: str) -> str:
    """Render the PERSONA.md file text from components."""
    desc = (description or "").strip().replace("\n", " ")
    tools = (toolset or "full").strip().lower()
    body_text = (system_prompt or "").strip()
    fm = f"---\nname: {name}\ndescription: {desc}\ntools: {tools}\n---\n\n"
    return fm + body_text + ("\n" if body_text and not body_text.endswith("\n") else "")
