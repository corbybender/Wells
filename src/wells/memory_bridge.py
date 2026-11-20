"""BigBrain memory bridge: durable, transaction-log project memory for Wells.

Wells already has three memory layers — the injected ``AGENT.md`` principles,
per-project ``AGENTS.md`` lessons (written directly by :mod:`wells.memory`),
and Reflexion failure critiques. What none of them have is a **transaction
log**: Wells writes memory files in place, so a crash mid-write loses the
lesson and concurrent agents (fleet mode, background subagents) can clobber
each other.

`BigBrain <https://github.com/corbybender/bigbrain-memory>`_ adds exactly
that lifecycle: entries are appended to a locked ``log.md`` under
``.bigbrain/`` in the project, then reconciled ("compiled") into curated
Markdown memory files — deterministically by default. This module embeds
BigBrain **as a library only** (no CLI installs, no MCP server, no git-hook
mutation) and adapts it to Wells:

  * :func:`log_lesson` — finisher write-back: a run summary becomes a
    LEARNING entry (+ INVARIANT entries for reviewer gotchas).
  * :func:`log_reflection` — Reflexion critiques become LEARNING entries so
    future planners can recall them.
  * :func:`compile` — deterministic reconciliation into ``.bigbrain/memory``.
    Wells deliberately does NOT use BigBrain's LLM-assisted compile
    (``use_llm=True`` shells out to an external CLI); Wells's own model does
    reconciliation when needed, and the deterministic path is the default.
  * :func:`recall` — keyword recall for the planner/coder, exposed to the
    model as the read-only ``memory_search`` tool (registered by
    :func:`wells.tools._register_optional_tools`).

Retrieval note: Wells's fastembed semantic search stays the retrieval story
for *code*; BigBrain search covers *decisions/learnings* only.

Everything here is best-effort and feature-gated (``WELLS_BIGBRAIN=0``
disables; default is on when the ``bigbrain`` package is importable). A
missing BigBrain install degrades silently — Wells's own AGENTS.md lessons
still get written; only the durable log layer is skipped.
"""

from __future__ import annotations

import os
from pathlib import Path

# Entry types BigBrain understands (core.append_log validates these).
_DECISION = "DECISION"
_LEARNING = "LEARNING"
_INVARIANT = "INVARIANT"

try:  # pragma: no cover - trivial import guard
    from bigbrain import core as _bb

    BIGBRAIN_AVAILABLE = True
except Exception:  # pragma: no cover
    _bb = None
    BIGBRAIN_AVAILABLE = False


def enabled() -> bool:
    """Whether the bridge is active (installed AND not disabled by env)."""
    if os.environ.get("WELLS_BIGBRAIN", "1").strip().lower() in (
        "0", "false", "no", "off",
    ):
        return False
    return BIGBRAIN_AVAILABLE


def _ensure(root: str | Path) -> Path:
    """Init ``.bigbrain/`` scaffolding if needed; return the project Path."""
    p = Path(root)
    if not p.is_dir():
        raise NotADirectoryError(f"workspace does not exist: {p}")
    _bb.init_engine(p)
    return p


def log_lesson(
    workspace: str | Path,
    *,
    goal: str,
    summary: str,
    key_files: list[str] | None = None,
    gotchas: list[str] | None = None,
) -> str | None:
    """Append the finisher's run lesson to the BigBrain transaction log.

    Returns the log line written, or None when the bridge is disabled.
    The entry lands in ``log.md``; :func:`compile` reconciles it into the
    curated memory files (the finisher calls both at session end).
    """
    if not enabled():
        return None
    root = _ensure(workspace)
    details: list[str] = []
    if key_files:
        details.append("files: " + ", ".join(key_files[:15]))
    line = _bb.append_log(
        root,
        _LEARNING,
        "tasks",
        (summary or goal)[:400],
        "\n".join(details),
    )
    for gotcha in (gotchas or [])[:6]:
        _bb.append_log(root, _INVARIANT, "invariants", gotcha[:400], "")
    return line


def log_reflection(workspace: str | Path, *, critique: str, failed: bool) -> str | None:
    """Persist a Reflexion critique as a LEARNING entry (best-effort)."""
    if not enabled() or not critique or not critique.strip():
        return None
    root = _ensure(workspace)
    kind = "failure critique" if failed else "post-run note"
    return _bb.append_log(root, _LEARNING, "reflexion", critique[:400], kind)


def compile(workspace: str | Path, *, reason: str = "wells session end") -> dict:
    """Deterministically reconcile pending log entries into memory files.

    Uses BigBrain's no-LLM merge path — data loss is impossible by design
    (entries are appended under a dated heading, never rewritten away).
    """
    if not enabled():
        return {"skipped": "bigbrain disabled or not installed"}
    root = _ensure(workspace)
    return _bb.compile_memory(root, reason=reason, use_llm=False)


def recall(workspace: str | Path, query: str, *, top_n: int = 6) -> str:
    """Keyword recall over compiled memory — backs the memory_search tool."""
    if not enabled():
        return "[bigbrain] not available (install bigbrain-memory or set WELLS_BIGBRAIN=1)"
    root = _ensure(workspace)
    hits = _bb.search(root, query, top_n)
    if not hits:
        return f"(no project memory matches {query!r})"
    lines: list[str] = []
    for h in hits[:top_n]:
        # BigBrain returns dicts with file/score/snippet-ish keys; render
        # defensively since the exact shape varies by version.
        if isinstance(h, dict):
            where = str(h.get("file") or h.get("target") or "?")
            snippet = str(
                h.get("summary") or h.get("snippet") or h.get("line") or ""
            ).strip()
            score = h.get("score")
            suffix = f" (score {score})" if isinstance(score, (int, float)) else ""
            lines.append(f"- [{where}{suffix}] {snippet}"[:300])
        else:
            lines.append(f"- {h}"[:300])
    return "Project memory matches:\n" + "\n".join(lines)


def status(workspace: str | Path) -> dict:
    """BigBrain engine status for /doctor-style checks."""
    if not enabled():
        return {"enabled": False}
    try:
        return {"enabled": True, **_bb.status(Path(workspace))}
    except Exception as e:
        return {"enabled": True, "error": str(e)}


# ---------------------------------------------------------------------------
# Tool descriptor (read-only recall for planner/coder/subagents)
# ---------------------------------------------------------------------------

from wells.tools import ToolContext, ToolDef, ToolResult  # noqa: E402


def _memory_search(ctx: ToolContext, query: str, top_n: int = 6) -> ToolResult:
    try:
        return ToolResult(True, recall(ctx.workspace, query, top_n=int(top_n)), "")
    except Exception as e:
        return ToolResult(False, "", f"memory_search failed: {e}")


MEMORY_SEARCH_TOOL = ToolDef(
    name="memory_search",
    description=(
        "Search this project's durable long-term memory (past decisions, "
        "learnings, and invariants recorded by earlier Wells runs via "
        "BigBrain). Use it BEFORE planning a task to recall how similar work "
        "went before. Keyword-based; returns curated memory snippets."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to recall"},
            "top_n": {"type": "integer", "default": 6},
        },
        "required": ["query"],
    },
    handler=_memory_search,
    mutating=False,
)
