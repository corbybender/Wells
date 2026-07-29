"""Reflexion: persistent, queryable failure critiques (self-improvement #1).

The reviewer and tester already produce rich, human-readable critiques when a
run fails (a red suite's exit code + output tail; an INCOMPLETE review with
specific feedback). Until now those critiques were *ephemeral* — they steered
the coder's next iteration within the same run, then disappeared. The same
failure mode could repeat verbatim on the next run, against the same repo,
with no memory of having hit it before.

This module closes that loop. After a run, :func:`capture_from_state` inspects
the final state for a genuine failure (deterministic test-gate red, or a real
reviewer rejection — not an infra hiccup) and appends a structured, timestamped
critique to ``<workspace>/.wells/reflections.md``. Before a run, the planner
calls :func:`inject_into_prompt`, which retrieves the top-K reflections most
relevant to the current goal and prepends them, so the planner starts already
warned about the project's known traps.

Design (mirrors :mod:`wells.memory`):
  * Storage is a single human-readable/editable markdown file under ``.wells/``,
    version-controlled with the repo. Reads never block: a missing/corrupt file
    is treated as empty.
  * Capture is deterministic by default — the critique is synthesized from the
    state's existing failure text, no extra LLM call. ``WELLS_REFLECTIONS_LLM=1``
    opts into a cheap-model-distilled sharper critique.
  * Retrieval ranks by keyword overlap (deterministic, free).
    ``WELLS_REFLECTIONS_EMBED=1`` opts into an embedding re-rank using the
    local ONNX embedder already wired up in :mod:`wells.embeddings`.
  * Each block carries a ``<!-- sig: ... -->`` fingerprint so the *same* failure
    (same type + goal keywords + evidence head) is recorded once, not every
    iteration of the same loop.
  * A size cap compacts the oldest entries, the same strategy AGENTS.md uses.

``WELLS_REFLECTIONS=0`` disables capture and injection entirely.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from wells import safety

REFLECTIONS_REL = Path(".wells") / "reflections.md"
MAX_FILE_BYTES = 16_000  # ~4k tokens; keep reflections a small slice of context

# Failure categories captured. ``infra`` (reviewer could not run) is deliberately
# NOT captured — an expired API key is not a lesson about this repo.
TYPE_TEST_FAILURE = "test_failure"
TYPE_REVIEW_REJECTION = "review_rejection"
TYPE_CODER_STALLED = "coder_stalled"


@dataclass
class Reflection:
    """One captured failure critique."""

    timestamp: str
    goal: str
    failure_type: str
    critique: str
    evidence: str
    signature: str


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """Whether capture + injection are on (``WELLS_REFLECTIONS`` != 0)."""
    import os

    return os.environ.get("WELLS_REFLECTIONS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _top_k() -> int:
    import os

    try:
        return max(1, int(os.environ.get("WELLS_REFLECTIONS_K", "3")))
    except ValueError:
        return 3


def _use_embed() -> bool:
    import os

    return os.environ.get("WELLS_REFLECTIONS_EMBED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _reflections_path(workspace: str | None) -> Path:
    root = safety.workspace_root(workspace)
    return root / REFLECTIONS_REL


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


_BLOCK_HEADER_RE = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2}[^\n]*)$", re.M)
_SIG_RE = re.compile(r"<!--\s*sig:\s*([0-9a-f]+)\s*-->", re.I)
_TYPE_RE = re.compile(r"^\s*-\s*type:\s*(\S+)\s*$", re.M)
_CRITIQUE_RE = re.compile(r"^\s*Critique:\s*(.*)$", re.M)
_GOAL_RE = re.compile(r"^###\s+\d{4}-\d{2}-\d{2}[^\n]*\s*—\s*(.*)$", re.M)


def load(workspace: str | None = None) -> list[Reflection]:
    """Parse ``reflections.md`` into structured reflections (empty on miss)."""
    try:
        path = _reflections_path(workspace)
    except Exception:
        return []
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return _parse(text)


def _parse(text: str) -> list[Reflection]:
    out: list[Reflection] = []
    # Split into blocks starting at each `### <date>` header.
    starts = [m.start() for m in _BLOCK_HEADER_RE.finditer(text)]
    if not starts:
        return []
    starts.append(len(text))
    for i in range(len(starts) - 1):
        block = text[starts[i]:starts[i + 1]]
        header = _BLOCK_HEADER_RE.search(block)
        if not header:
            continue
        timestamp = header.group(1).strip()
        goal_m = _GOAL_RE.search(block)
        goal = goal_m.group(1).strip() if goal_m else ""
        type_m = _TYPE_RE.search(block)
        failure_type = type_m.group(1).strip() if type_m else "unknown"
        sig_m = _SIG_RE.search(block)
        signature = sig_m.group(1).strip() if sig_m else ""
        crit_m = _CRITIQUE_RE.search(block)
        critique = crit_m.group(1).strip() if crit_m else ""
        # Evidence is everything after an "Evidence:" line.
        evidence = ""
        ev_m = re.search(r"^\s*Evidence:\s*\n?(.*)$", block, re.S | re.M)
        if ev_m:
            evidence = ev_m.group(1).strip()
        if not critique and not evidence:
            continue  # not a real reflection block
        out.append(
            Reflection(
                timestamp=timestamp,
                goal=goal,
                failure_type=failure_type,
                critique=critique,
                evidence=evidence,
                signature=signature,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Capture (write-back)
# ---------------------------------------------------------------------------


def _detect_failure(state: dict, dry: bool) -> tuple[str, str, str] | None:
    """Return ``(failure_type, critique, evidence)`` for a genuine failure, else None.

    Infra failures (``review_error``) are skipped unless the test gate also
    failed — a red suite is a real lesson regardless of whether the reviewer
    later crashed. Dry runs are never captured: a "failure" that never executed
    is not a lesson.
    """
    if dry:
        return None

    tests_passed = state.get("tests_passed")
    review_complete = bool(state.get("review_complete"))
    review_error = bool(state.get("review_error"))
    review_result = (state.get("review_result") or "").strip()
    test_results = (state.get("test_results") or "").strip()

    if tests_passed is False:
        ev = _tail(test_results or review_result, 1200)
        first = _first_nonempty_line(ev) or "(no output captured)"
        return (
            TYPE_TEST_FAILURE,
            f"Automated test gate failed for this goal. {first}",
            ev,
        )

    if not review_complete and not review_error:
        ev = _tail(review_result, 1200)
        critique = _first_nonempty_line(ev) or "Reviewer marked the work incomplete."
        # Drop the leading "DECISION:" line if present — it's noise in a critique.
        critique = re.sub(r"^\s*DECISION:\s*INCOMPLETE\s*", "", critique, flags=re.I)
        return (TYPE_REVIEW_REJECTION, critique, ev)

    # Coder stalled without producing changes and without a clean review:
    # capture only when there's actual executor output to learn from.
    steps = (state.get("implementation_steps") or "").strip()
    if not review_complete and steps and re.search(
        r"\b(aborted|stuck_loop|did not complete|error)\b", steps, re.I
    ):
        return (
            TYPE_CODER_STALLED,
            "The coder could not complete this goal within its step budget.",
            _tail(steps, 1200),
        )

    return None


def capture_from_state(state: dict, workspace: str | None, *, dry: bool = False) -> Path | None:
    """Inspect ``state`` for a failure and append a reflection if one is found.

    Honours the safety gate (best-effort — a denied/dry-run write is a no-op).
    Returns the path written, or None when nothing was captured. Never raises.
    """
    if not enabled():
        return None
    detected = _detect_failure(state, dry)
    if detected is None:
        return None
    failure_type, critique, evidence = detected

    goal = (state.get("goal") or "").strip()
    signature = _signature(failure_type, goal, evidence)

    # Dedup: a reflection with the same fingerprint already on file means the
    # same failure loop is being re-recorded — skip rather than spam the file.
    try:
        existing = load(workspace)
    except Exception:
        existing = []
    if any(r.signature and r.signature == signature for r in existing):
        return None

    block = _format_block(
        timestamp=time.strftime("%Y-%m-%d %H:%M"),
        goal=goal,
        failure_type=failure_type,
        critique=critique,
        evidence=evidence,
        signature=signature,
    )

    try:
        path = _reflections_path(workspace)
    except Exception:
        return None

    decision = safety.gate("write_file", f"append reflection ({failure_type}) to {path.name}")
    if not decision.allowed:
        return None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        prev = path.read_text(encoding="utf-8") if path.is_file() else ""
        path.write_text(_merge_block(prev, block), encoding="utf-8")
    except Exception:
        return None
    return path


def _signature(failure_type: str, goal: str, evidence: str) -> str:
    """Stable fingerprint: type + normalized goal keywords + evidence head."""
    goal_tokens = " ".join(sorted(_tokens(goal)))
    ev_head = re.sub(r"\s+", " ", (evidence or "").strip())[:200].lower()
    raw = f"{failure_type}|{goal_tokens}|{ev_head}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _format_block(
    *, timestamp: str, goal: str, failure_type: str, critique: str, evidence: str, signature: str
) -> str:
    goal_line = goal.strip()[:140] or "(no goal)"
    critique_line = re.sub(r"\s+", " ", critique.strip())[:400]
    ev = (evidence or "").strip()
    parts = [
        f"### {timestamp} — {goal_line}",
        f"- type: {failure_type}",
        f"<!-- sig: {signature} -->",
        f"Critique: {critique_line}",
    ]
    if ev:
        parts.append("Evidence:")
        parts.append(ev[:2000])
    return "\n".join(parts).strip()


def _merge_block(existing: str, block: str) -> str:
    if not existing.strip():
        return (
            "# Reflections — failure critiques captured by the Wells harness\n\n"
            "This file records what went wrong on past runs so the planner can\n"
            "avoid repeating the same failure. Edit or prune freely; it is\n"
            "version-controlled with the code.\n\n"
            "## Reflections\n\n"
            + block
            + "\n"
        )

    if re.search(r"^##\s+Reflections\s*$", existing, re.M):
        def _push(m: re.Match) -> str:
            return m.group(0) + "\n" + block + "\n"

        updated = re.sub(r"^##\s+Reflections\s*$", _push, existing, count=1, flags=re.M)
    else:
        updated = existing.rstrip() + "\n\n## Reflections\n\n" + block + "\n"

    if len(updated.encode("utf-8")) > MAX_FILE_BYTES:
        updated = _compact_oldest(updated)
    return updated


def _compact_oldest(text: str) -> str:
    """Fold older `### timestamp` blocks into one summary line (mirrors memory)."""
    blocks = re.split(r"(?=^### \d{4}-\d{2})", text, flags=re.M)
    if len(blocks) <= 3:
        return text
    head, rest = blocks[0], blocks[1:]
    keep_n = max(1, len(rest) // 2)
    old, recent = rest[:-keep_n], rest[-keep_n:]
    n_old = len([b for b in old if b.strip()])
    return (
        head.rstrip()
        + f"\n\n_(compacted {n_old} older reflections to stay within budget)_\n\n"
        + "".join(recent)
    )


# ---------------------------------------------------------------------------
# Retrieval + prompt injection
# ---------------------------------------------------------------------------


_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "have", "will", "your",
    "into", "using", "use", "need", "when", "where", "which", "what", "make",
    "adds", "add", "fix", "update", "code", "file", "files", "function",
    "should", "must", "does", "test", "tests",
}


def _tokens(text: str) -> set[str]:
    """Lowercase significant word tokens (len>=3, stop words dropped)."""
    out: set[str] = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_+-]{2,}", text or ""):
        w = m.group(0).lower()
        if w in _STOPWORDS:
            continue
        out.add(w)
    return out


def _keyword_score(goal_tokens: set[str], reflection: Reflection) -> float:
    if not goal_tokens:
        return 0.0
    doc = _tokens(f"{reflection.goal} {reflection.critique} {reflection.evidence}")
    if not doc:
        return 0.0
    overlap = goal_tokens & doc
    if not overlap:
        return 0.0
    # Normalized overlap (Jaccard-like) — rewards precision of the match.
    return len(overlap) / math.sqrt(len(goal_tokens) * len(doc))


def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed via the local ONNX model; None when embeddings are unavailable."""
    try:
        from wells import embeddings
    except Exception:
        return None
    if not embeddings.EMBED_AVAILABLE:
        return None
    out: list[list[float]] = []
    for t in texts:
        v = embeddings.embed_query(t)
        if v is None:
            return None
        out.append(v)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def retrieve(
    workspace: str | None, goal: str, *, k: int | None = None
) -> list[Reflection]:
    """Top-K reflections relevant to ``goal``, highest-scoring first.

    Keyword overlap is always computed. When ``WELLS_REFLECTIONS_EMBED=1`` and
    the embedder is available, an embedding similarity is blended in.
    """
    try:
        reflections = load(workspace)
    except Exception:
        reflections = []
    if not reflections:
        return []
    if k is None:
        k = _top_k()

    goal_tokens = _tokens(goal)
    scored = [(r, _keyword_score(goal_tokens, r)) for r in reflections]

    if _use_embed():
        try:
            goal_vec = _embed_texts([_doc_text_for_embed(goal)])
            doc_vecs = _embed_texts([_doc_text_for_embed(_reflection_doc(r)) for r in reflections])
            if goal_vec and doc_vecs and len(doc_vecs) == len(reflections):
                gv = goal_vec[0]
                blended: list[tuple[Reflection, float]] = []
                for (r, kw), dv in zip(scored, doc_vecs):
                    cos = _cosine(gv, dv)
                    blended.append((r, 0.4 * kw + 0.6 * max(0.0, cos)))
                scored = blended
        except Exception:
            pass  # embed path is best-effort; keyword ranking stands.

    scored.sort(key=lambda t: -t[1])
    # Drop zero-score reflections (no relevance) before applying the cap.
    return [r for r, s in scored if s > 0.0][:k]


def _doc_text_for_embed(text: str) -> str:
    return text.strip() or "(empty)"


def _reflection_doc(r: Reflection) -> str:
    return f"{r.goal}. {r.critique} {r.evidence}"


def inject_into_prompt(
    prompt: str,
    workspace: str | None,
    goal: str,
    *,
    k: int | None = None,
) -> str:
    """Prepend the top-K relevant reflections to ``prompt`` (no-op when none)."""
    if not enabled():
        return prompt
    top = retrieve(workspace, goal, k=k)
    if not top:
        return prompt
    lines = [
        "PAST REFLECTIONS — failures this project hit before. "
        "Avoid repeating them:",
    ]
    for i, r in enumerate(top, 1):
        crit = re.sub(r"\s+", " ", r.critique.strip())[:240]
        lines.append(f"[{i}] ({r.failure_type}) {crit}")
        if r.goal.strip():
            lines.append(f"    goal: {r.goal.strip()[:120]}")
    block = "\n".join(lines)
    return f"{block}\n\n---\n\n{prompt}"


# ---------------------------------------------------------------------------
# Management (used by /reflections)
# ---------------------------------------------------------------------------


def clear(workspace: str | None) -> bool:
    """Delete the reflections file. Honours the safety gate. Returns success."""
    try:
        path = _reflections_path(workspace)
    except Exception:
        return False
    if not path.is_file():
        return True
    decision = safety.gate("write_file", f"delete {path.name}")
    if not decision.allowed:
        return False
    try:
        path.unlink()
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------


def _tail(text: str, n: int) -> str:
    """Last ``n`` characters of ``text`` (where runners put failure summaries)."""
    t = (text or "").strip()
    return t[-n:] if len(t) > n else t


def _first_nonempty_line(text: str) -> str:
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s:
            return s[:300]
    return ""


__all__ = [
    "Reflection",
    "enabled",
    "load",
    "capture_from_state",
    "retrieve",
    "inject_into_prompt",
    "clear",
    "TYPE_TEST_FAILURE",
    "TYPE_REVIEW_REJECTION",
    "TYPE_CODER_STALLED",
]
