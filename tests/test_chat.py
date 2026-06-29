"""Tests for the conversational intent router and chat memory.

These cover the heuristic classifier (the free, instant path) on a spread of
realistic REPL inputs, plus ConversationMemory bookkeeping. The LLM-fallback
classifier and the live conversational_reply are integration-tested manually.
"""

from __future__ import annotations

import pytest

from coding_harness import chat
from coding_harness.chat import ConversationMemory, classify_intent


# ---------------------------------------------------------------------------
# Heuristic classifier (no LLM calls)
# ---------------------------------------------------------------------------

CHAT_INPUTS = [
    "explain your previous output in a simple summary. Did you actually make the fix I asked for?",
    "Did you actually make the fix?",
    "What does this file do?",
    "hello",
    "thanks!",
    "why does the test fail?",
    "how do I fix the login bug?",
    "What did you change?",
    "explain what you just did",
    "can you tell me what happened?",
    "ok",
    "yes",
    "hi there",
    "what's the status of the last run?",
    "describe the architecture you proposed",
]

TASK_INPUTS = [
    "we need to modify the Page picking section of /admin",
    "fix the bug in parser.py",
    "add a login page",
    "refactor the auth module",
    "create a new component for the dashboard",
    "can you fix the bug?",  # wants work done
    "please implement the feature",
    "write a test for the parser",
    "delete the old config file",
    "update the README to include install steps",
    "migrate the database schema",
    "please add a button that loads the selected page",
]


@pytest.mark.parametrize("text", CHAT_INPUTS)
def test_classify_chat(text):
    assert classify_intent(text, use_llm_fallback=False) == "chat", text


@pytest.mark.parametrize("text", TASK_INPUTS)
def test_classify_task(text):
    assert classify_intent(text, use_llm_fallback=False) == "task", text


def test_empty_is_chat():
    assert classify_intent("", use_llm_fallback=False) == "chat"


def test_ambiguous_defaults_to_task_without_llm():
    # A single neutral noun like "dashboard" is ambiguous -> task (conservative).
    assert classify_intent("dashboard", use_llm_fallback=False) == "task"


def test_llm_fallback_disabled_returns_task_for_ambiguous():
    assert classify_intent("something", use_llm_fallback=False) == "task"


def test_classifier_cache_cleared():
    chat._CLASSIFIER_CACHE["stub"] = "chat"
    chat.clear_classifier_cache()
    assert chat._CLASSIFIER_CACHE == {}


# ---------------------------------------------------------------------------
# ConversationMemory
# ---------------------------------------------------------------------------


def test_memory_add_and_messages():
    mem = ConversationMemory(max_turns=4)
    mem.add("user", "hello")
    mem.add("assistant", "hi there")
    msgs = mem.as_messages("SYSTEM")
    assert msgs[0].content == "SYSTEM"
    assert msgs[1].content == "hello"
    assert msgs[2].content == "hi there"


def test_memory_run_summary_injected():
    mem = ConversationMemory()
    mem.set_run_summary("Goal: fix bug\nStatus: INCOMPLETE")
    msgs = mem.as_messages("SYSTEM")
    # system + run-summary-system + (no turns) = 2 messages
    assert len(msgs) == 2
    assert "fix bug" in msgs[1].content


def test_memory_bounded():
    mem = ConversationMemory(max_turns=2)
    mem.add("user", "a")
    mem.add("assistant", "b")
    mem.add("user", "c")  # should evict oldest
    msgs = mem.as_messages("S")
    # system + 2 turns
    assert len(msgs) == 3
    contents = [m.content for m in msgs]
    assert "a" not in contents  # evicted


def test_memory_clear():
    mem = ConversationMemory()
    mem.add("user", "x")
    mem.set_run_summary("summary")
    mem.clear()
    assert mem.turns == [] or len(mem.turns) == 0
    assert mem.last_run_summary == ""
