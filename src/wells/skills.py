"""Agent skills: discoverable know-how loaded on demand.

A *skill* is a small ``SKILL.md`` file (with optional reference docs / scripts)
that packages a chunk of know-how: how to value a position, how to add a new
provider profile to this repo, how to run the release pipeline, etc. The agent
sees only each skill's **name and one-line description** up front (injected
into the system prompt), and **progressively loads** a skill's full body only
when a request matches it — by calling the ``load_skill`` tool.

This solves the "stuff every instruction into the system prompt" anti-pattern:
context stays small and focused, and domain how-to scales without bloating
every call. It is the natural complement to ``AGENTS.md`` memory:

  * ``AGENTS.md``  — *accumulated facts* about a repo; always-on (small).
  * Skills         — *how-to procedures*; load-on-demand (potentially large).

A ``SKILL.md`` is YAML-front-matter + markdown body, e.g.::

    ---
    name: release-index
    description: How to cut and publish a new wells-index release.
    ---

    1. Bump version in wells-index/Cargo.toml
    2. Tag index-vX.Y.Z
    3. ...

Resolution: skills are discovered from the workspace ``skills/`` directory,
any extra paths in ``WELLS_SKILLS_PATHS`` (a path-list), and finally the
built-in skills shipped with the Wells package itself (``builtin_skills/``,
alongside this module — general, repo-agnostic know-how like "check CI
config before guessing a test command", useful out of the box with zero
per-repo setup). Name collisions resolve first-wins in that same order, so
a workspace's own ``skills/verify-external-api/`` transparently shadows the
built-in of the same name — a repo can override or disable an individual
built-in just by defining its own skill with the same name.

``WELLS_SKILLS=0`` disables the provider entirely; ``WELLS_BUILTIN_SKILLS=0``
disables only the shipped defaults, leaving workspace/WELLS_SKILLS_PATHS
skills unaffected.
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
# Max body chars loaded into context by load_skill (keeps a runaway skill small).
_MAX_BODY_CHARS = 8000
# Max chars for the up-front skill index injected into every prompt.
_MAX_INDEX_CHARS = 1200


@dataclass(frozen=True)
class Skill:
    """One discoverable skill."""

    name: str
    description: str
    body: str
    path: Path

    def index_line(self) -> str:
        """One-line summary for the always-on index: ``name — description``."""
        desc = self.description.strip().replace("\n", " ")
        if len(desc) > 110:
            desc = desc[:107] + "…"
        return f"- {self.name}: {desc}"


@dataclass
class SkillIndex:
    """The set of discovered skills for a workspace."""

    skills: list[Skill] = field(default_factory=list)
    roots: list[Path] = field(default_factory=list)

    def by_name(self, name: str) -> Skill | None:
        target = name.strip().lower()
        for s in self.skills:
            if s.name.lower() == target:
                return s
        return None

    def is_empty(self) -> bool:
        return not self.skills


# ---------------------------------------------------------------------------
# Discovery + parsing
# ---------------------------------------------------------------------------


# Skills shipped with the Wells package itself — general, repo-agnostic
# know-how (see the module docstring). Always LAST in the roots list so a
# workspace's own skill of the same name shadows it (dedup in
# _index_cached is first-wins by roots order).
_BUILTIN_SKILLS_ROOT = Path(__file__).parent / "builtin_skills"


def builtin_skills_enabled() -> bool:
    return os.environ.get("WELLS_BUILTIN_SKILLS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _skill_paths(workspace: str | None = None) -> list[Path]:
    """Roots to search for ``skills/<name>/SKILL.md`` (or loose ``SKILL.md``).

      1. ``<workspace>/skills/`` (the conventional location)
      2. Any extra dir in ``WELLS_SKILLS_PATHS`` (os.pathsep-separated)
      3. The package's built-in skills (``WELLS_BUILTIN_SKILLS=0`` to disable)

    Non-existent dirs are silently skipped. Duplicates removed.
    """
    roots: list[Path] = []
    try:
        ws = safety.workspace_root(workspace)
        roots.append(ws / "skills")
    except Exception:
        pass
    extra = os.environ.get("WELLS_SKILLS_PATHS", "").strip()
    if extra:
        for piece in re.split(rf"[{re.escape(os.pathsep)}]", extra):
            p = piece.strip()
            if p:
                roots.append(Path(p))
    if builtin_skills_enabled():
        roots.append(_BUILTIN_SKILLS_ROOT)
    # De-dup by resolved path, drop non-existent, keep order.
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


def _parse(content: str, path: Path) -> Skill | None:
    """Parse a SKILL.md file into a :class:`Skill`. None on failure."""
    m = _FRONT_MATTER_RE.match(content)
    fm: dict[str, str] = {}
    body = content
    if m:
        for k, v in _KV_RE.findall(m.group("fm")):
            fm[k.lower()] = v.strip().strip("\"'")
        body = m.group("body").strip()
    name = fm.get("name") or path.parent.name
    desc = fm.get("description") or ""
    name = name.strip()
    if not name:
        return None
    return Skill(name=name, description=desc, body=body, path=path)


def _discover_in(root: Path) -> list[Skill]:
    """Find all skills under ``root``.

    Supports two layouts:
      * ``<root>/<name>/SKILL.md``  (a skill folder; recommended)
      * ``<root>/SKILL.md``         (a single skill at the root)
    """
    out: list[Skill] = []
    if not root.is_dir():
        return out
    # Skill folders first.
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except Exception:
        return out
    for child in children:
        if child.is_dir():
            sf = child / "SKILL.md"
            if sf.is_file():
                try:
                    skill = _parse(sf.read_text(encoding="utf-8", errors="replace"), sf)
                except Exception:
                    skill = None
                if skill:
                    out.append(skill)
        elif child.is_file() and child.name.upper() == "SKILL.MD":
            try:
                skill = _parse(child.read_text(encoding="utf-8", errors="replace"), child)
            except Exception:
                skill = None
            if skill:
                out.append(skill)
    return out


@lru_cache(maxsize=16)
def _index_cached(key: str) -> SkillIndex:
    """Cache a SkillIndex by a stable key (joined roots + their mtimes).

    The key is a newline-joined list of ``path||mtime`` entries. Paths are
    split back out here; the mtime just invalidates the cache when a skill
    file is edited.
    """
    roots: list[Path] = []
    for entry in key.split("\n") if key else []:
        if "||" in entry:
            path_str = entry.split("||", 1)[0]
        else:
            path_str = entry
        if path_str:
            roots.append(Path(path_str))
    skills_list: list[Skill] = []
    for r in roots:
        skills_list.extend(_discover_in(r))
    # De-dup by name (first wins) so an override dir can shadow defaults.
    seen: set[str] = set()
    dedup: list[Skill] = []
    for s in skills_list:
        if s.name.lower() in seen:
            continue
        seen.add(s.name.lower())
        dedup.append(s)
    return SkillIndex(skills=dedup, roots=roots)


def _roots_key(roots: list[Path]) -> str:
    """Build a cache key embedding mtimes so edits invalidate the cache.

    Uses ``||`` between path and mtime (never a path separator) so the path
    can be split back out unambiguously on every OS.
    """
    parts: list[str] = []
    for r in roots:
        try:
            stamp = str(int(r.stat().st_mtime))
        except Exception:
            stamp = "?"
        parts.append(f"{r}||{stamp}")
    return "\n".join(parts)


def skills_for(workspace: str | None = None) -> SkillIndex:
    """Return the discovered :class:`SkillIndex` for ``workspace``."""
    if not enabled():
        return SkillIndex()
    roots = _skill_paths(workspace)
    return _index_cached(_roots_key(roots))


def enabled() -> bool:
    """Whether the skills provider is on (``WELLS_SKILLS`` != 0)."""
    return os.environ.get("WELLS_SKILLS", "1").strip().lower() not in ("0", "false", "no", "off")


def clear_cache() -> None:
    """Drop cached indexes (after a skill is added/edited at runtime)."""
    _index_cached.cache_clear()


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def skill_index_block(workspace: str | None = None) -> str:
    """The always-on index: one line per available skill.

    Injected into the system prompt so the model knows what skills exist and
    can ask to load one. Returns "" when no skills are configured.
    """
    idx = skills_for(workspace)
    if idx.is_empty():
        return ""
    lines = [s.index_line() for s in idx.skills]
    block = "\n".join(lines)
    if len(block) > _MAX_INDEX_CHARS:
        block = block[:_MAX_INDEX_CHARS] + "\n… (skill list truncated)"
    return (
        "=== AVAILABLE SKILLS (load with the load_skill tool) ===\n"
        f"{block}\n"
        "Call load_skill(name) to load a skill's full instructions when a "
        "request matches one. Do not load a skill unless it is relevant.\n"
        "=== END SKILLS ===\n"
    )


def inject_into_prompt(prompt: str, workspace: str | None = None) -> str:
    """Append the skill index to ``prompt`` (no-op when no skills exist)."""
    block = skill_index_block(workspace)
    if not block:
        return prompt
    return f"{prompt}\n\n{block}"


def load_skill_body(name: str, workspace: str | None = None) -> tuple[bool, str]:
    """Resolve ``name`` to a skill and return ``(ok, body_or_error)``.

    Truncates large bodies so a load never blows the budget. Used by the
    ``load_skill`` tool in :mod:`wells.tools`.
    """
    idx = skills_for(workspace)
    if idx.is_empty():
        return False, "No skills are configured in this workspace."
    skill = idx.by_name(name)
    if skill is None:
        avail = ", ".join(s.name for s in idx.skills)
        return False, f"Unknown skill {name!r}. Available: {avail}"
    body = skill.body.strip()
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "\n… (skill body truncated)"
    header = f"# Skill: {skill.name}\n"
    return True, header + body


# ---------------------------------------------------------------------------
# Mutation operations (create / read-raw / update / delete)
# ---------------------------------------------------------------------------
# Used by the /skills menu (CLI + TUI modal). Each goes through the safety
# gate so plan/approve/dryrun apply, validates the skill name (no path
# traversal), and clears the discovery cache so the next read sees the change.

# Safe skill-name charset: lowercase letters, digits, hyphens. Matches the
# folder-name convention and blocks any path tricks.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def validate_name(name: str) -> str | None:
    """Return an error message if ``name`` is invalid, else None."""
    n = (name or "").strip().lower()
    if not n:
        return "Skill name is required."
    if not _NAME_RE.match(n):
        return (
            "Skill name must be lowercase letters, digits, and hyphens "
            "(e.g. 'release-checklist')."
        )
    if n.startswith("-") or n.endswith("-") or "--" in n:
        return "Skill name must not start/end with a hyphen or contain consecutive hyphens."
    if len(n) > 64:
        return "Skill name must be 64 characters or fewer."
    return None


def _skills_dir(workspace: str | None = None) -> Path:
    """The primary skills directory: ``<workspace>/skills/``."""
    root = safety.workspace_root(workspace)
    return root / "skills"


def skill_file_path(name: str, workspace: str | None = None) -> Path | None:
    """Return the ``SKILL.md`` path for ``name``, or None if not found."""
    skill = skills_for(workspace).by_name(name)
    return skill.path if skill else None


def read_skill_raw(name: str, workspace: str | None = None) -> tuple[bool, str]:
    """Return ``(ok, raw_text_or_error)`` — the full SKILL.md file content.

    Unlike :func:`load_skill_body` (which returns only the parsed body, capped),
    this returns the *entire* raw file (front-matter + body) for the editor.
    """
    path = skill_file_path(name, workspace)
    if path is None:
        return False, f"Unknown skill {name!r}."
    try:
        return True, path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, f"Could not read {path}: {e}"


def create_skill(
    name: str,
    description: str,
    body: str,
    workspace: str | None = None,
) -> tuple[bool, str]:
    """Create a new skill under ``<workspace>/skills/<name>/SKILL.md``.

    Returns ``(ok, message)``. Honours the safety gate; fails if the skill
    already exists (use :func:`update_skill` to change it).
    """
    err = validate_name(name)
    if err:
        return False, err
    name = name.strip().lower()

    if skills_for(workspace).by_name(name) is not None:
        return False, f"A skill named {name!r} already exists. Use /skills edit to change it."

    detail = f"create skill {name!r}"
    decision = safety.gate("write_file", detail)
    if not decision.allowed:
        return False, decision.reason

    skill_dir = _skills_dir(workspace) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(_format_skill_file(name, description, body), encoding="utf-8")
    clear_cache()
    return True, f"Created skill {name!r} at {path}"


def update_skill(
    name: str,
    workspace: str | None = None,
    *,
    description: str | None = None,
    body: str | None = None,
) -> tuple[bool, str]:
    """Update an existing skill's description and/or body.

    Only the fields you pass are changed; the rest is preserved. Returns
    ``(ok, message)``. Honours the safety gate.
    """
    name = (name or "").strip().lower()
    skill = skills_for(workspace).by_name(name)
    if skill is None:
        return False, f"Unknown skill {name!r}."

    detail = f"update skill {name!r}"
    decision = safety.gate("write_file", detail)
    if not decision.allowed:
        return False, decision.reason

    # Reconstruct from the current file: keep whichever fields weren't passed.
    cur_desc = skill.description
    cur_body = skill.body
    new_desc = description.strip() if description is not None else cur_desc
    new_body = body if body is not None else cur_body

    path = skill.path
    path.write_text(_format_skill_file(name, new_desc, new_body), encoding="utf-8")
    clear_cache()
    return True, f"Updated skill {name!r}."


def delete_skill(name: str, workspace: str | None = None) -> tuple[bool, str]:
    """Delete a skill (its folder + SKILL.md). Returns ``(ok, message)``.

    Honours the safety gate. Refuses to delete skills outside the workspace
    ``skills/`` tree (e.g. ones loaded from ``WELLS_SKILLS_PATHS``).
    """
    name = (name or "").strip().lower()
    skill = skills_for(workspace).by_name(name)
    if skill is None:
        return False, f"Unknown skill {name!r}."

    # Confinement: only delete skills that live under the workspace skills dir.
    ws_skills = _skills_dir(workspace).resolve()
    try:
        skill_dir = skill.path.parent.resolve()
        skill_dir.relative_to(ws_skills)
    except (ValueError, OSError):
        return (
            False,
            f"Skill {name!r} is not under {ws_skills} — it may be loaded from "
            "WELLS_SKILLS_PATHS and can't be deleted from here. Remove its "
            "folder manually.",
        )

    detail = f"delete skill {name!r} ({skill_dir})"
    decision = safety.gate("write_file", detail)
    if not decision.allowed:
        return False, decision.reason

    try:
        import shutil

        shutil.rmtree(skill_dir)
    except Exception as e:
        return False, f"Could not delete {skill_dir}: {e}"
    clear_cache()
    return True, f"Deleted skill {name!r}."


def _format_skill_file(name: str, description: str, body: str) -> str:
    """Render the SKILL.md file text from components."""
    desc = (description or "").strip().replace("\n", " ")
    body_text = (body or "").strip()
    fm = f"---\nname: {name}\ndescription: {desc}\n---\n\n"
    return fm + body_text + ("\n" if body_text and not body_text.endswith("\n") else "")
