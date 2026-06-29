"""Conversational mode: routes plain chat past the agentic loop.

The Wells REPL sends every message through the planner->architect->coder->
tester->reviewer graph by default. That is the right thing for a real
development task ("add a login page", "fix the bug in parser.py"), but it is
absurdly expensive for a quick question ("did you actually make that change?",
"what does this file do?", "explain your last run").

This module provides:

* :func:`classify_intent` — decides whether a message is a conversational
  question (``"chat"``) or a real development task (``"task"``). A fast
  heuristic layer handles the obvious cases for free; ambiguous inputs fall
  back to a tiny classifier call against the cheap model.

* :func:`conversational_reply` — streams a direct LLM reply to a question,
  with full conversation history + a summary of the most recent agent run so
  follow-up questions like "did it work?" have context.

* :class:`ConversationMemory` — a bounded, role-tagged history of the chat so
  the conversation is coherent across turns.

The router is intentionally conservative: when in doubt, it routes to the
agentic loop. A wrong "chat" decision wastes the user's time; a wrong "task"
decision only costs a little extra compute.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from coding_harness.config import (
    _invoke_with_retry,
    get_llm_for_task,
    model_name_for_task,
)
from coding_harness.tokens import LEDGER, calibrate, estimate_tokens

Intent = Literal["chat", "task"]


# ---------------------------------------------------------------------------
# Heuristic intent classification (free, instant, no model call)
# ---------------------------------------------------------------------------

# Strongly conversational openings. A message starting with one of these is
# almost always a question aimed at the assistant, not a build instruction.
_QUESTION_STARTERS = (
    "what ",
    "why ",
    "how ",
    "when ",
    "where ",
    "who ",
    "did you",
    "do you",
    "are you",
    "is the",
    "is it",
    "is there",
    "can you",
    "could you",
    "would you",
    "will you",
    "should i",
    "explain",
    "tell me",
    "describe",
    "summarize",
    "what's",
    "whats",
    "how's",
    "hows",
    "where's",
    "show me",
    "help me understand",
    "what did",
    "what does",
    "what is",
    "what was",
    "what were",
    "which ",
    "whose ",
    "whom ",
)

# Greetings / acknowledgements / filler that is never a dev task.
_CHITCHAT = (
    "hi",
    "hello",
    "hey",
    "yo",
    "sup",
    "howdy",
    "thanks",
    "thank you",
    "thx",
    "ty",
    "cool",
    "nice",
    "got it",
    "ok",
    "okay",
    "k",
    "lol",
    "haha",
    "sure",
    "right",
    "yes",
    "no",
    "agreed",
    "makes sense",
    "understood",
    "sounds good",
)

# Verbs that signal a request to *change the codebase* — a real task.
# If any appear, lean toward "task" even when the message is phrased as a
# question ("can you fix the login bug?").
_TASK_SIGNALS = (
    "implement",
    "create",
    "build",
    "add",
    "write",
    "generate",
    "make",
    "fix",
    "repair",
    "patch",
    "resolve",
    "debug",
    "refactor",
    "rename",
    "reorganize",
    "restructure",
    "move",
    "delete",
    "remove",
    "drop",
    "clean up",
    "cleanup",
    "update",
    "upgrade",
    "modify",
    "change",
    "edit",
    "replace",
    "install",
    "deploy",
    "migrate",
    "port",
    "convert",
    "optimize",
    "speed up",
    "improve",
    "enhance",
    "set up",
    "setup",
    "configure",
)

# Reference to prior work — strong signal this is a follow-up question, not a
# brand-new task.
_FOLLOWUP_SIGNALS = (
    "previous",
    "earlier",
    "last",
    "just",
    "your output",
    "your run",
    "the fix",
    "the change",
    "the patch",
    "the result",
    "the summary",
    "did you",
    "have you",
    "you said",
    "you mentioned",
    "you wrote",
    "that ",
    "this ",
    "it ",
    "above",
    "below",
)

# Patterns that make a "task" word actually a question ("how do i fix X" is a
# question, not an instruction to fix X).
_QUESTION_HINTS_RE = re.compile(
    r"\b(how (do|does|to|can|could)|why (do|does|is|are|was|were|can't|cannot)"
    r"|what (is|are|do|does|was|were|does the)|where (is|are|do i|does)"
    r"|can (i|you) (use|run|call|do)|is (it|there|this|that) (a |an |the )?"
    r"|explain |tell me |describe )\b",
    re.IGNORECASE,
)


def _word_count(text: str) -> int:
    return len(text.split())


def _heuristic_classify(text: str) -> Intent | None:
    """Return ``"chat"``/``"task"`` for obvious cases, ``None`` if ambiguous."""
    stripped = text.strip()
    if not stripped:
        return "chat"
    lower = " " + stripped.lower() + " "
    words = _word_count(stripped)

    # Very short chitchat / greetings → chat.
    if words <= 4:
        bare = stripped.lower().strip("?!.,")
        first = bare.split(None, 1)[0] if bare else ""
        if bare in _CHITCHAT or first in _CHITCHAT:
            return "chat"

    has_task_signal = any(sig in lower for sig in _TASK_SIGNALS)
    has_followup = any(sig in lower for sig in _FOLLOWUP_SIGNALS)
    has_question_mark = "?" in stripped
    starts_with_question = lower[1:].lstrip().startswith(_QUESTION_STARTERS)
    looks_like_question = bool(_QUESTION_HINTS_RE.search(stripped))

    # A question about prior work ("did you make the fix?") → chat.
    if has_followup and (
        has_question_mark or starts_with_question or looks_like_question
    ):
        return "chat"

    # "how do I fix X" / "why does X fail" — asking, not instructing.
    if looks_like_question and not _imperative(stripped):
        return "chat"

    # Pure question with no task verb → chat.
    if starts_with_question and not has_task_signal:
        return "chat"

    # Imperative task verb present → task.
    if has_task_signal and not starts_with_question and not looks_like_question:
        return "task"

    # Task verb phrased as a question ("can you fix the bug?") → task, because
    # the user clearly wants the work done.
    if (
        has_task_signal
        and (starts_with_question or has_question_mark)
        and not has_followup
    ):
        if _imperative_after_please(stripped):
            return "task"

    return None  # ambiguous


def _imperative(text: str) -> bool:
    """True if the sentence reads as a command (starts with a task verb)."""
    first = text.strip().split(None, 1)[0].lower().rstrip(",.:!")
    return first in _TASK_SIGNALS


def _imperative_after_please(text: str) -> bool:
    """True for 'please <task-verb> ...' / 'can you <task-verb> ...'."""
    lower = text.lower().lstrip()
    for lead in ("please ", "can you ", "could you ", "would you ", "will you "):
        if lower.startswith(lead):
            rest = lower[len(lead) :].lstrip()
            first = rest.split(None, 1)[0].rstrip(",.:!") if rest else ""
            if first in _TASK_SIGNALS:
                return True
    return False


# ---------------------------------------------------------------------------
# LLM-based intent classification (fallback for ambiguous inputs)
# ---------------------------------------------------------------------------

_CLASSIFIER_SYSTEM = (
    "You are a fast intent router for a coding assistant REPL. Decide whether "
    "the user's message is (a) a CONVERSATIONAL question that deserves a direct "
    "answer, or (b) a TASK that requires reading/editing files and running "
    "commands in the workspace.\n\n"
    "Reply with exactly one word: CHAT or TASK.\n\n"
    "Guidelines:\n"
    "- 'explain what you did', 'did the fix work?', 'what does X do?' -> CHAT\n"
    "- 'what files did you change?' -> CHAT\n"
    "- 'add a login page', 'fix the bug in parser.py', 'refactor X' -> TASK\n"
    "- 'can you fix the bug?' (wants work done) -> TASK\n"
    "- 'how do I fix X?' (asking to learn) -> CHAT\n"
    "- when unsure, prefer TASK.\n"
)

_CLASSIFIER_CACHE: dict[str, Intent] = {}


def _llm_classify(text: str) -> Intent:
    """Classify via the cheap model. Cached + fault-tolerant (defaults to task)."""
    key = text.strip().lower()[:200]
    if key in _CLASSIFIER_CACHE:
        return _CLASSIFIER_CACHE[key]
    try:
        llm = get_llm_for_task("classification")
        resp = _invoke_with_retry(
            llm, [SystemMessage(content=_CLASSIFIER_SYSTEM), HumanMessage(content=text)]
        )
        answer = (resp.content or "").strip().upper()
        intent: Intent = "chat" if answer.startswith("CHAT") else "task"
    except Exception:
        intent = "task"  # conservative fallback: run the agent
    _CLASSIFIER_CACHE[key] = intent
    return intent


def classify_intent(text: str, *, use_llm_fallback: bool = True) -> Intent:
    """Decide whether ``text`` is a conversational question or a dev task.

    A fast heuristic handles the obvious cases for free. Ambiguous inputs fall
    back to a one-word classifier call against the cheap model (unless
    ``use_llm_fallback`` is False, in which case ambiguous -> task).
    """
    direct = _heuristic_classify(text)
    if direct is not None:
        return direct
    if use_llm_fallback:
        return _llm_classify(text)
    return "task"


def clear_classifier_cache() -> None:
    _CLASSIFIER_CACHE.clear()


# ---------------------------------------------------------------------------
# Conversation memory
# ---------------------------------------------------------------------------


@dataclass
class ConversationMemory:
    """Bounded chat history + the most recent agent-run summary.

    ``last_run_summary`` is set by the REPL after each agentic run so that
    follow-up questions ("did it work?", "what did you change?") have context
    without re-running the whole graph.
    """

    max_turns: int = 12
    turns: Deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=12))
    last_run_summary: str = ""

    def __post_init__(self) -> None:
        # Respect max_turns if passed to the constructor.
        self.turns = deque(self.turns, maxlen=self.max_turns)

    def add(self, role: str, content: str) -> None:
        self.turns.append((role, content))

    def clear(self) -> None:
        self.turns.clear()
        self.last_run_summary = ""

    def set_run_summary(self, summary: str) -> None:
        self.last_run_summary = (summary or "").strip()

    def as_messages(self, system: str) -> list:
        """Build a LangChain message list: system + run-summary + history."""
        msgs: list = [SystemMessage(content=system)]
        if self.last_run_summary:
            msgs.append(
                SystemMessage(
                    content=(
                        "Context — the most recent agent run in this session:\n"
                        + self.last_run_summary
                    )
                )
            )
        for role, content in self.turns:
            if role == "user":
                msgs.append(HumanMessage(content=content))
            else:
                msgs.append(AIMessage(content=content))
        return msgs


# ---------------------------------------------------------------------------
# Conversational reply
# ---------------------------------------------------------------------------

_CHAT_SYSTEM = (
    "You are Wells, a concise, helpful coding assistant chatting with the user "
    "in an interactive REPL. Answer the user's question directly and briefly. "
    "You are NOT in agentic mode — do not claim to run tools or edit files. "
    "If the user is actually asking you to perform a coding task (create, fix, "
    "refactor, build something), tell them to phrase it as a task or use the "
    "/task command so the full agent loop can run. Be honest about what the "
    "last agent run did or did not accomplish based on the provided context."
)


def conversational_reply(
    text: str,
    memory: ConversationMemory,
    *,
    on_token=None,
) -> str:
    """Stream a direct conversational reply to ``text``.

    Records the exchange in ``memory`` and accounts tokens to the global
    :data:`LEDGER` under the ``chat`` step. ``on_token(token)`` is called for
    each streamed token (if the model streams).
    """
    messages = memory.as_messages(_CHAT_SYSTEM)
    messages.append(HumanMessage(content=text))
    llm = get_llm_for_task("chat")
    model = model_name_for_task("chat")
    full_text = _CHAT_SYSTEM + "\n" + text

    try:
        # Stream if a callback was supplied.
        if on_token is not None:
            collected: list[str] = []
            for chunk in llm.stream(messages):
                piece = chunk.content or ""
                if piece:
                    collected.append(piece)
                    on_token(piece)
            content = "".join(collected).strip()
            # usage_metadata isn't reliably on the final stream chunk; estimate.
            input_tokens = estimate_tokens(full_text)
            output_tokens = estimate_tokens(content) if content else 0
            reasoning_tokens = 0
            cache_read_tokens = 0
        else:
            resp = _invoke_with_retry(llm, messages)
            content = (resp.content or "").strip()
            um = getattr(resp, "usage_metadata", None) or {}
            input_tokens = um.get("input_tokens") or estimate_tokens(full_text)
            output_tokens = um.get("output_tokens") or 0
            reasoning_tokens = (
                (um.get("output_token_details") or {}).get("reasoning")
            ) or 0
            cache_read_tokens = (
                (um.get("input_token_details") or {}).get("cache_read")
            ) or 0
        calibrate(full_text, input_tokens)
    except Exception as err:
        content = f"(chat call failed: {type(err).__name__}: {str(err)[:160]})"
        input_tokens = estimate_tokens(full_text)
        output_tokens = reasoning_tokens = cache_read_tokens = 0
        print(f"[chat] {content}")

    LEDGER.record(
        step="chat",
        task_type="chat",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        category_tokens={"chat": input_tokens},
        saved_by_trim=0,
        saved_by_summary=0,
    )

    memory.add("user", text)
    memory.add("assistant", content or "(no response)")
    return content
