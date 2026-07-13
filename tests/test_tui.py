"""Tests for the Textual TUI (wells.tui).

No pytest-asyncio dependency: Textual's ``app.run_test()`` is an async
context manager, so each test wraps its body in a small ``async def`` and
drives it with a plain ``asyncio.run()`` call from a synchronous test
function — avoids adding a new test-runner dependency for a handful of
TUI-level tests.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

from textual import events


def _run(coro_fn):
    """Run an async test body defined as a zero-arg coroutine function."""
    asyncio.run(coro_fn())


@asynccontextmanager
async def _mounted_input():
    """Yield (app, PromptInput, submitted) with a real WellsApp mounted.

    ``submitted`` collects PromptInput.Submitted values; those messages are
    swallowed (not forwarded) so a bug under test can't accidentally kick
    off a real agent run against a live model. The background repo-index
    build that on_mount normally starts is patched to a no-op — irrelevant
    here, and its thread can otherwise outlive the test's app context and
    print a stray "coroutine was never awaited" warning.
    """
    from wells.tui import WellsApp

    submitted: list[str] = []
    with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
        app = WellsApp()
        async with app.run_test(size=(120, 40)):
            inp = app._input
            inp.focus()
            orig_post = inp.post_message

            def spy_post(message):
                if isinstance(message, inp.Submitted):
                    submitted.append(message.value)
                    return None
                return orig_post(message)

            inp.post_message = spy_post
            yield app, inp, submitted


def _key_event(sender, key: str, char: str | None):
    ev = events.Key(key, char)
    ev.set_sender(sender)
    return ev


def test_paste_as_keystrokes_does_not_submit_mid_paste():
    """A raw-keystroke-fed multi-line paste must not submit partial lines.

    A genuine bracketed paste never reaches PromptInput._on_key at all —
    Textual delivers it as one ``events.Paste``, handled by TextArea's own
    ``_on_paste``, and inserts the full text (newlines included) as a single
    atomic edit. This test covers the FALLBACK path: a terminal/paste method
    that doesn't (or can't) use bracketed paste, so the pasted text arrives
    as an ordinary keystroke stream — each embedded newline is indistinguish-
    able from the user pressing Enter, and used to submit a truncated
    message per line instead of the whole multi-line paste.

    Textual's Pilot.press() is NOT used here — it awaits a full idle/animator
    cycle after every key (by design, for deterministic tests), which
    defeats a timing-based heuristic: those injected waits can exceed real
    inter-keystroke gaps regardless of the burst threshold. A real terminal
    delivers paste-as-keys back-to-back with no such wait, so this calls
    PromptInput._on_key() directly in a plain loop to match that.
    """

    async def body():
        async with _mounted_input() as (_app, inp, submitted):
            text = "line one\nline two\nline three"
            for ch in text:
                key = "enter" if ch == "\n" else ch
                char = None if key == "enter" else ch
                await inp._on_key(_key_event(inp, key, char))

            assert not submitted, f"submitted mid-paste: {submitted}"
            assert inp.text == text

    _run(body)


def test_normal_typing_and_enter_still_submits():
    """The paste-burst heuristic must not affect ordinary human-speed typing."""

    async def body():
        async with _mounted_input() as (_app, inp, submitted):
            for ch in "hello":
                await inp._on_key(_key_event(inp, ch, ch))
                await asyncio.sleep(0.08)  # real inter-keystroke gap
            await asyncio.sleep(0.3)  # deliberate pause before Enter
            await inp._on_key(_key_event(inp, "enter", None))

            assert submitted == ["hello"]

    _run(body)


def test_lone_fast_enter_with_no_prior_burst_still_submits():
    """A single fast Enter (e.g. key auto-repeat) with nothing typed before
    it is not a paste — the >=2-fast-keystrokes gate must let it submit."""

    async def body():
        async with _mounted_input() as (_app, inp, submitted):
            await inp._on_key(_key_event(inp, "enter", None))
            assert submitted == [""]

    _run(body)
