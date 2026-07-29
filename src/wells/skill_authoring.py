"""Auto-skill authoring: package verified runs into reusable skills (self-improvement #2).

A skill is only ever as good as the procedure it encodes. The most reliable
procedures are the ones a run has *already proven* — a clean, verified execution
path through this exact repo. This module turns those paths into ``SKILL.md``
proposals automatically, behind an explicit human approval gate.

Flow:

  1. **Qualify** — after a run, :func:`run_is_clean` confirms the run was
     verified (reviewer COMPLETE + suite green/no tests) and actually produced
     a non-trivial change. Dry-run/plan runs and infra failures never qualify.
  2. **Propose** — :func:`propose_from_state` derives a deterministic name,
     description, and body from the planner's (verified) plan + the coder's
     change summary, and stages the result as a *proposal* under
     ``<workspace>/.wells/skill-proposals/<name>.md``. Proposals are NOT scanned
     by :mod:`wells.skills`, so they cost zero context and cannot be loaded by
     the agent until promoted.
  3. **Approve** — the user reviews proposals (``/skills proposals``) and either
     accepts or rejects each one. Accepting moves the file to
     ``<workspace>/.wells/skills/<name>/SKILL.md`` (a discoverable root) and
     clears the skill cache; from the next run on, the agent can ``load_skill``
     it like any hand-authored skill.

Name/description/body synthesis is deterministic by default (no extra LLM
call). ``WELLS_AUTOSKILL_LLM=1`` opts into a cheap-model-polished name and
summary. ``WELLS_AUTOSKILL=0`` disables the whole feature.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from wells import safety, skills

PROPOSALS_REL = Path(".wells") / "skill-proposals"
AUTOSKILLS_REL = Path(".wells") / "skills"


@dataclass
class Proposal:
    """One staged skill proposal."""

    name: str
    description: str
    body: str
    source_goal: str
    proposed_at: str
    path: Path


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return os.environ.get("WELLS_AUTOSKILL", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _min_steps() -> int:
    try:
        return max(1, int(os.environ.get("WELLS_AUTOSKILL_MIN_STEPS", "2")))
    except ValueError:
        return 2


def _use_llm() -> bool:
    return os.environ.get("WELLS_AUTOSKILL_LLM", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _proposals_dir(workspace: str | None) -> Path:
    return safety.workspace_root(workspace) / PROPOSALS_REL


def _autoskills_dir(workspace: str | None) -> Path:
    return safety.workspace_root(workspace) / AUTOSKILLS_REL


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------


def run_is_clean(state: dict, *, dry: bool = False) -> bool:
    """True when a run was genuinely verified and worth packaging.

    A clean run = the reviewer marked it COMPLETE, the deterministic test gate
    did not go red (green or no runnable test setup), and it wasn't a simulated
    (dry-run/plan) run. Infra failures (``review_error``) disqualify regardless.
    """
    if dry:
        return False
    if not state.get("review_complete"):
        return False
    if state.get("tests_passed") is False:
        return False
    if state.get("review_error"):
        return False
    steps = (state.get("implementation_steps") or "").strip()
    if not steps:
        return False
    # A run that aborted partway is not a complete, proven procedure.
    if re.search(r"\b(aborted|did not complete)\b", steps, re.I):
        return False
    return True


# ---------------------------------------------------------------------------
# Plan parsing (local copy so this module stays decoupled from the coder)
# ---------------------------------------------------------------------------


def _section(plan: str, heading: str) -> str:
    m = re.search(
        rf"^##\s*{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", plan or "",
        re.M | re.S | re.I,
    )
    return m.group(1).strip() if m else ""


_STEP_LINE_RE = re.compile(r"^\s*\d+[.)]\s+")


def _plan_steps(plan: str) -> list[str]:
    """Numbered entries under the plan's 'Implementation steps' heading."""
    body = _section(plan, "Implementation steps")
    if not body:
        return []
    steps: list[str] = []
    cur: list[str] = []
    for line in body.splitlines():
        if _STEP_LINE_RE.match(line):
            if cur:
                steps.append("\n".join(cur).strip())
            cur = [line.strip()]
        elif cur and line.strip():
            cur.append(line.strip())
    if cur:
        steps.append("\n".join(cur).strip())
    return steps


def _extract_files(text: str, *, max_n: int = 12) -> list[str]:
    """Best-effort file-path extraction from the coder's change summary."""
    if not text:
        return []
    pat = re.compile(
        r"([\w./\-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|md|toml|yaml|yml|json|sh))"
    )
    seen: list[str] = []
    for m in pat.finditer(text):
        f = m.group(1)
        if f not in seen:
            seen.append(f)
        if len(seen) >= max_n:
            break
    return seen


# ---------------------------------------------------------------------------
# Name / description / body synthesis
# ---------------------------------------------------------------------------


_SLUG_KEEP = re.compile(r"[a-z0-9]+")


def _slugify(text: str, *, limit: int = 40) -> str:
    """Derive a lowercase hyphen slug from ``text``."""
    words = _SLUG_KEEP.findall((text or "").lower())
    slug = "-".join(words)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:limit] or "task"


def _first_sentence(text: str, *, limit: int = 110) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    m = re.search(r"^(.+?[.!?])\s/", t)
    head = m.group(1) if m else t
    return head[:limit].strip()


def _synthesize_name_desc(goal: str) -> tuple[str, str]:
    name = _slugify(goal)
    description = _first_sentence(goal) or (f"Auto-authored procedure for: {goal[:80]}")
    if len(description) > 110:
        description = description[:107] + "…"
    return name, description


def _build_body(state: dict, name: str, goal: str) -> str:
    plan = state.get("development_plan") or ""
    steps = _plan_steps(plan)
    verification = _section(plan, "Verification")
    files = _extract_files(state.get("implementation_steps") or "")

    # If the plan has no numbered steps, fall back to the coder's summary lines.
    if not steps:
        for ln in (state.get("implementation_steps") or "").splitlines():
            s = ln.strip()
            if s and not s.startswith("Step "):
                steps.append(s)
        steps = steps[:8]

    lines = [
        f"<!-- Auto-authored by the Wells harness from a verified run on "
        f"{time.strftime('%Y-%m-%d')}. -->",
        f"<!-- Source goal: {goal.strip()[:200]} -->",
        "",
        "This skill was proposed automatically from a run that passed review "
        "and the test suite. Adjust it to fit how you want the agent to repeat "
        "this kind of task.",
        "",
        "## Steps",
    ]
    if steps:
        for i, step in enumerate(steps, 1):
            oneline = " ".join(step.split())[:240]
            lines.append(f"{i}. {oneline}")
    else:
        lines.append("(No discrete steps were extracted — describe the procedure here.)")

    if verification:
        lines.append("")
        lines.append("## Verification")
        lines.append(verification[:800])

    if files:
        lines.append("")
        lines.append("## Key files")
        lines.append(", ".join(files))

    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Proposals: write / list / accept / reject
# ---------------------------------------------------------------------------


def _proposal_path(name: str, workspace: str | None) -> Path:
    return _proposals_dir(workspace) / f"{name}.md"


def _ensure_unique(name: str, workspace: str | None) -> str | None:
    """Return ``name`` if no proposal/skill with that name exists, else None."""
    if _proposal_path(name, workspace).is_file():
        return None
    try:
        if skills.skills_for(workspace).by_name(name) is not None:
            return None
    except Exception:
        pass
    return name


def _parse_proposal_file(path: Path) -> Proposal | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    fm, body = _split_front_matter(raw)
    name = (fm.get("name") or path.stem).strip().lower()
    description = (fm.get("description") or "").strip()
    source_goal = (fm.get("source_goal") or _extract_meta(raw, "Source goal") or "").strip()
    proposed_at = (fm.get("proposed_at") or _extract_meta(raw, "verified run on") or "").strip()
    if not description:
        description = _first_sentence(source_goal) or "(auto-authored skill)"
    return Proposal(
        name=name,
        description=description,
        body=body,
        source_goal=source_goal,
        proposed_at=proposed_at,
        path=path,
    )


def _split_front_matter(raw: str) -> tuple[dict[str, str], str]:
    m = re.match(r"\A\s*---\s*\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)\Z", raw, re.DOTALL)
    if not m:
        return {}, raw
    fm: dict[str, str] = {}
    for k, v in re.findall(r"^\s*([A-Za-z_][\w\-]*)\s*:\s*(.*?)\s*$", m.group("fm"), re.M):
        fm[k.lower()] = v.strip().strip("\"'")
    return fm, m.group("body").strip()


def _extract_meta(raw: str, label: str) -> str:
    m = re.search(rf"<!--\s*{re.escape(label)}:\s*(.*?)\s*-->", raw)
    return m.group(1) if m else ""


def propose_from_state(state: dict, workspace: str | None, *, dry: bool = False) -> Path | None:
    """Stage a skill proposal when ``state`` is a clean, qualifying run.

    Returns the proposal path, or None when skipped (disabled, not clean, too
    trivial, duplicate, or denied by the safety gate). Never raises.
    """
    if not enabled() or not run_is_clean(state, dry=dry):
        return None

    plan = state.get("development_plan") or ""
    steps = _plan_steps(plan)
    if len(steps) < _min_steps():
        return None

    goal = (state.get("goal") or "").strip()
    if not goal:
        return None

    name, description = _synthesize_name_desc(goal)
    err = skills.validate_name(name)
    if err:
        return None
    if _ensure_unique(name, workspace) is None:
        return None  # already proposed or already a skill

    body = _build_body(state, name, goal)
    content = _format_proposal_file(
        name=name,
        description=description,
        body=body,
        source_goal=goal,
        proposed_at=time.strftime("%Y-%m-%d %H:%M"),
    )

    try:
        path = _proposal_path(name, workspace)
    except Exception:
        return None

    decision = safety.gate("write_file", f"stage skill proposal {name!r}")
    if not decision.allowed:
        return None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception:
        return None
    return path


def list_proposals(workspace: str | None = None) -> list[Proposal]:
    """All staged proposals, sorted by name."""
    try:
        d = _proposals_dir(workspace)
    except Exception:
        return []
    if not d.is_dir():
        return []
    out: list[Proposal] = []
    for p in sorted(d.glob("*.md"), key=lambda x: x.name):
        prop = _parse_proposal_file(p)
        if prop:
            out.append(prop)
    return out


def accept_proposal(
    name: str,
    workspace: str | None = None,
    *,
    description: str | None = None,
    body: str | None = None,
) -> tuple[bool, str]:
    """Promote a staged proposal to a discoverable skill.

    Moves the proposal to ``.wells/skills/<name>/SKILL.md`` and clears the skill
    cache. Optional ``description`` / ``body`` overrides (from the edit-then-
    accept modal flow) replace the synthesized values. Honours the safety gate.
    """
    name = (name or "").strip().lower()
    if not name:
        return False, "Proposal name is required."
    src = _proposal_path(name, workspace)
    if not src.is_file():
        return False, f"No proposal named {name!r}. Use `/skills proposals` to list them."

    proposal = _parse_proposal_file(src)
    if proposal is None:
        return False, f"Proposal {name!r} is unreadable."

    err = skills.validate_name(name)
    if err:
        return False, err

    if skills.skills_for(workspace).by_name(name) is not None:
        return False, f"A skill named {name!r} already exists."

    decision = safety.gate("write_file", f"accept skill proposal {name!r}")
    if not decision.allowed:
        return False, decision.reason

    final_desc = (description.strip() if description is not None else proposal.description)
    final_body = (body if body is not None else proposal.body)

    try:
        skill_dir = _autoskills_dir(workspace) / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            skills._format_skill_file(name, final_desc, final_body),
            encoding="utf-8",
        )
        src.unlink(missing_ok=True)
    except Exception as e:
        return False, f"Could not promote {name!r}: {e}"

    skills.clear_cache()
    return True, f"Accepted skill {name!r} — now discoverable via load_skill."


def reject_proposal(name: str, workspace: str | None = None) -> tuple[bool, str]:
    """Delete a staged proposal. Honours the safety gate."""
    name = (name or "").strip().lower()
    if not name:
        return False, "Proposal name is required."
    src = _proposal_path(name, workspace)
    if not src.is_file():
        return False, f"No proposal named {name!r}."

    decision = safety.gate("write_file", f"reject skill proposal {name!r}")
    if not decision.allowed:
        return False, decision.reason

    try:
        src.unlink()
    except Exception as e:
        return False, f"Could not delete {name!r}: {e}"
    return True, f"Rejected skill proposal {name!r}."


def _format_proposal_file(
    *, name: str, description: str, body: str, source_goal: str, proposed_at: str
) -> str:
    desc = (description or "").strip().replace("\n", " ")
    fm = (
        "---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        f"source_goal: {source_goal.strip()[:200]}\n"
        f"proposed_at: {proposed_at}\n"
        f"status: proposal\n"
        "---\n\n"
    )
    return fm + body + ("\n" if body and not body.endswith("\n") else "")


def proposal_body(name: str, workspace: str | None = None) -> tuple[bool, str]:
    """Return the body of a staged proposal for editing (edit-then-accept)."""
    src = _proposal_path((name or "").strip().lower(), workspace)
    if not src.is_file():
        return False, f"No proposal named {name!r}."
    p = _parse_proposal_file(src)
    if p is None:
        return False, f"Proposal {name!r} is unreadable."
    return True, p.body


__all__ = [
    "Proposal",
    "enabled",
    "run_is_clean",
    "propose_from_state",
    "list_proposals",
    "accept_proposal",
    "reject_proposal",
    "proposal_body",
]
