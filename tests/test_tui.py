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


# ---------------------------------------------------------------------------
# ChoicePickScreen — the arrow+Enter picker for choice-constrained settings
# (HARNESS_SAFETY, PLAN_MODE, ...), replacing free-text typing for them.
# ---------------------------------------------------------------------------


def test_choice_pick_screen_lists_all_choices_and_marks_current():
    from wells.tui import ChoicePickScreen, WellsApp

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = ChoicePickScreen(
                    "HARNESS_SAFETY", ("auto", "approve", "dryrun", "sandbox"), "auto"
                )
                app.push_screen(screen)
                await pilot.pause()

                lst = screen.query_one("#choice-list")
                labels = [
                    str(lst.get_option_at_index(i).prompt)
                    for i in range(lst.option_count)
                ]
                assert len(labels) == 4
                assert any("auto" in lbl and "current" in lbl for lbl in labels)
                assert any("sandbox" in lbl and "current" not in lbl for lbl in labels)

    _run(body)


def test_choice_pick_screen_dismisses_with_picked_value():
    from wells.tui import ChoicePickScreen, WellsApp

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                picked: list[str | None] = []
                screen = ChoicePickScreen(
                    "HARNESS_SAFETY", ("auto", "approve", "dryrun", "sandbox"), "auto"
                )
                app.push_screen(screen, picked.append)
                await pilot.pause()

                # Simulate selecting "sandbox" the way OptionList.OptionSelected
                # would arrive from a real Enter keypress on that row.
                from textual.widgets import OptionList as _OptionList

                lst = screen.query_one("#choice-list")
                event = _OptionList.OptionSelected(
                    lst, lst.get_option("sandbox"), lst.get_option_index("sandbox")
                )
                screen.on_option_list_option_selected(event)
                await pilot.pause()

                assert picked == ["sandbox"]

    _run(body)


def test_settings_screen_opens_picker_for_choice_settings(monkeypatch):
    """Selecting a choices-constrained row (HARNESS_SAFETY) must push
    ChoicePickScreen, not the free-text Input box."""
    from wells.tui import SettingsScreen, WellsApp
    from wells import settings as settings_mod

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = SettingsScreen()
                app.push_screen(screen)
                await pilot.pause()

                pushed: list = []
                monkeypatch.setattr(
                    app, "push_screen",
                    lambda scr, cb=None: pushed.append(scr),
                )

                s = settings_mod.SETTINGS_BY_KEY["HARNESS_SAFETY"]
                assert s.choices  # sanity: this setting IS choice-constrained
                from textual.widgets import OptionList as _OptionList

                lst = screen.query_one("#settings-list")
                event = _OptionList.OptionSelected(
                    lst,
                    lst.get_option("HARNESS_SAFETY"),
                    lst.get_option_index("HARNESS_SAFETY"),
                )
                screen.on_option_list_option_selected(event)

                assert len(pushed) == 1
                from wells.tui import ChoicePickScreen
                assert isinstance(pushed[0], ChoicePickScreen)
                # The free-text input must stay hidden for choice settings.
                assert screen.query_one("#settings-input").display is False

    _run(body)


# ---------------------------------------------------------------------------
# AgentsScreen / AgentEditScreen — the /agents subagent-persona modal manager
# ---------------------------------------------------------------------------


def test_agents_screen_shows_empty_state(tmp_path, monkeypatch):
    from wells import config as config_mod
    from wells.tui import AgentsScreen, WellsApp

    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(tmp_path))

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = AgentsScreen()
                app.push_screen(screen)
                await pilot.pause()

                lst = screen.query_one("#agents-list")
                assert lst.option_count == 1
                assert "add one" in str(lst.get_option_at_index(0).prompt)

    _run(body)


def test_agents_screen_add_creates_persona_on_disk(tmp_path, monkeypatch):
    from wells import config as config_mod
    from wells import personas as pz
    from wells.tui import AgentsScreen, WellsApp

    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(tmp_path))

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = AgentsScreen()
                app.push_screen(screen)
                await pilot.pause()

                # Simulate AgentEditScreen dismissing with a filled-in form,
                # the same shape action_add's _done callback expects.
                screen.action_add()
                await pilot.pause()

                edit_screen = app.screen
                from wells.tui import AgentEditScreen
                assert isinstance(edit_screen, AgentEditScreen)
                edit_screen.dismiss({
                    "name": "reviewer",
                    "description": "Reviews things.",
                    "tools": "readonly",
                    "system_prompt": "You are a reviewer.",
                })
                await pilot.pause()

                idx = pz.personas_for(str(tmp_path))
                p = idx.by_name("reviewer")
                assert p is not None
                assert p.toolset == "readonly"
                assert p.system_prompt == "You are a reviewer."

                lst = screen.query_one("#agents-list")
                labels = [str(lst.get_option_at_index(i).prompt) for i in range(lst.option_count)]
                assert any("reviewer" in lbl for lbl in labels)

    _run(body)


def test_agent_edit_screen_rejects_bad_toolset():
    from wells.tui import AgentEditScreen, WellsApp

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = AgentEditScreen(existing=None)
                app.push_screen(screen)
                await pilot.pause()

                screen.query_one("#agent-name").value = "ok-name"
                screen.query_one("#agent-tools").value = "bogus"
                screen.action_save()
                await pilot.pause()

                # Bad toolset must block the save -- the screen stays open
                # (not dismissed) rather than silently accepting garbage.
                assert app.screen is screen

    _run(body)


# ---------------------------------------------------------------------------
# MemoryScreen / MemoryEditScreen — the /memory global-user-memory modal
# ---------------------------------------------------------------------------


def test_memory_screen_shows_empty_state(tmp_path, monkeypatch):
    from wells.tui import MemoryScreen, WellsApp

    monkeypatch.setenv("WELLS_USER_MEMORY_DIR", str(tmp_path / "memory"))

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = MemoryScreen()
                app.push_screen(screen)
                await pilot.pause()

                lst = screen.query_one("#memory-list")
                assert lst.option_count == 1
                assert "add one" in str(lst.get_option_at_index(0).prompt)

    _run(body)


def test_memory_screen_add_creates_entry_on_disk(tmp_path, monkeypatch):
    from wells import user_memory as um
    from wells.tui import MemoryScreen, WellsApp

    monkeypatch.setenv("WELLS_USER_MEMORY_DIR", str(tmp_path / "memory"))

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = MemoryScreen()
                app.push_screen(screen)
                await pilot.pause()

                screen.action_add()
                await pilot.pause()

                from wells.tui import MemoryEditScreen
                edit_screen = app.screen
                assert isinstance(edit_screen, MemoryEditScreen)
                edit_screen.dismiss({
                    "name": "terse-commits",
                    "description": "Prefers terse commits.",
                    "type": "feedback",
                    "body": "Keep it short.",
                })
                await pilot.pause()

                e = um.memories().by_name("terse-commits")
                assert e is not None
                assert e.type == "feedback"
                assert e.body == "Keep it short."

                lst = screen.query_one("#memory-list")
                labels = [str(lst.get_option_at_index(i).prompt) for i in range(lst.option_count)]
                assert any("terse-commits" in lbl for lbl in labels)

    _run(body)


def test_memory_edit_screen_rejects_bad_name_on_create():
    from wells.tui import MemoryEditScreen, WellsApp

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = MemoryEditScreen(existing=None)
                app.push_screen(screen)
                await pilot.pause()

                screen.query_one("#memory-name").value = "Bad Name"
                screen.action_save()
                await pilot.pause()

                # Bad name must block the save -- the screen stays open.
                assert app.screen is screen

    _run(body)


# ---------------------------------------------------------------------------
# RuleAddScreen — the /rules add permission-allowlist form
# ---------------------------------------------------------------------------


def test_rule_add_screen_creates_rule_on_disk(tmp_path, monkeypatch):
    from wells import config as config_mod
    from wells import rules as rules_mod
    from wells.tui import RuleAddScreen, WellsApp

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(ws))
    monkeypatch.setattr(rules_mod, "_GLOBAL_RULES", tmp_path / "no-global.yaml")
    rules_mod._ENGINES.clear()

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = RuleAddScreen()
                app.push_screen(screen)
                await pilot.pause()

                screen.query_one("#rule-id").value = "allow-ls"
                screen.query_one("#rule-severity").value = "allow"
                screen.query_one("#rule-tool").value = "run_command"
                screen.query_one("#rule-pattern").value = r"^ls\b"
                screen.query_one("#rule-message").value = "listing is safe"
                screen.action_save()
                await pilot.pause()

                eng = rules_mod.engine_for(str(ws))
                assert any(r.id == "allow-ls" for r in eng.rules)
                d = eng.check("run_command", {"command": "ls -la"})
                assert d.auto_approve

    _run(body)


def test_rule_add_screen_rejects_bad_severity(tmp_path, monkeypatch):
    from wells import config as config_mod
    from wells import rules as rules_mod
    from wells.tui import RuleAddScreen, WellsApp

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(ws))
    monkeypatch.setattr(rules_mod, "_GLOBAL_RULES", tmp_path / "no-global.yaml")
    rules_mod._ENGINES.clear()

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = RuleAddScreen()
                app.push_screen(screen)
                await pilot.pause()

                screen.query_one("#rule-id").value = "x"
                screen.query_one("#rule-severity").value = "bogus"
                screen.query_one("#rule-pattern").value = "y"
                screen.query_one("#rule-message").value = "z"
                screen.action_save()
                await pilot.pause()

                # Bad severity must block the save -- the screen stays open.
                assert app.screen is screen

    _run(body)


# ---------------------------------------------------------------------------
# ScheduleAddScreen — the /schedule add unattended-run form
# ---------------------------------------------------------------------------


def test_schedule_add_screen_creates_schedule(tmp_path, monkeypatch):
    from wells import config as config_mod
    from wells import schedule as sched_mod
    from wells.tui import ScheduleAddScreen, WellsApp

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(ws))
    monkeypatch.setattr(sched_mod, "_REGISTRY", tmp_path / "schedules.json")
    monkeypatch.setattr(sched_mod, "_SCRIPT_DIR", tmp_path / "schedule-scripts")
    monkeypatch.setattr(sched_mod, "_LOG_DIR", tmp_path / "schedule-logs")

    async def body():
        with (
            patch.object(WellsApp, "_ensure_repo_index", lambda self: None),
            patch.object(sched_mod, "_register_windows", return_value=(True, "mocked")),
            patch.object(sched_mod, "_register_cron", return_value=(True, "mocked")),
        ):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = ScheduleAddScreen()
                app.push_screen(screen)
                await pilot.pause()

                screen.query_one("#schedule-name").value = "nightly"
                screen.query_one("#schedule-interval").value = "every1h"
                screen.query_one("#schedule-goal").value = "run the linter"
                screen.action_save()
                await pilot.pause()

                entry = sched_mod.by_name("nightly")
                assert entry is not None
                assert entry["interval"] == "every1h"
                assert entry["goal"] == "run the linter"

    _run(body)


def test_schedule_add_screen_rejects_bad_interval(tmp_path, monkeypatch):
    from wells import config as config_mod
    from wells import schedule as sched_mod
    from wells.tui import ScheduleAddScreen, WellsApp

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(ws))
    monkeypatch.setattr(sched_mod, "_REGISTRY", tmp_path / "schedules.json")
    monkeypatch.setattr(sched_mod, "_SCRIPT_DIR", tmp_path / "schedule-scripts")
    monkeypatch.setattr(sched_mod, "_LOG_DIR", tmp_path / "schedule-logs")

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(120, 40)) as pilot:
                screen = ScheduleAddScreen()
                app.push_screen(screen)
                await pilot.pause()

                screen.query_one("#schedule-name").value = "x"
                screen.query_one("#schedule-interval").value = "bogus"
                screen.query_one("#schedule-goal").value = "y"
                screen.action_save()
                await pilot.pause()

                # Bad interval must block the save -- the screen stays open.
                assert app.screen is screen

    _run(body)


# ---------------------------------------------------------------------------
# Proposal banner + review modal (self-improvement #2 surfaces)
# ---------------------------------------------------------------------------


def test_proposal_banner_shows_when_proposals_exist(tmp_path, monkeypatch):
    """The right-panel banner highlights when a proposal is staged."""
    from wells import config as config_mod
    from wells.tui import ProposalBanner, WellsApp
    from wells import skill_authoring as sa
    from wells import skills as sk

    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_SAFETY", "auto")
    monkeypatch.setenv("WELLS_BUILTIN_SKILLS", "0")
    sk.clear_cache()
    # Stage a proposal directly.
    pdir = tmp_path / ".wells" / "skill-proposals"
    pdir.mkdir(parents=True)
    (pdir / "my-task.md").write_text(
        "---\nname: my-task\ndescription: A test skill.\nsource_goal: x\n"
        "status: proposal\n---\nBody.\n", encoding="utf-8",
    )
    assert sa.list_proposals(str(tmp_path))  # sanity

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                banner = app._panel.query_one("#proposal-banner", ProposalBanner)
                # _refresh runs on a 1s interval; the banner should carry the
                # highlighted pseudo-class when proposals are staged.
                assert "-has-proposals" in banner.classes

    _run(body)


def test_proposal_banner_hidden_when_no_proposals(tmp_path, monkeypatch):
    from wells import config as config_mod
    from wells.tui import ProposalBanner, WellsApp

    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WELLS_BUILTIN_SKILLS", "0")

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                banner = app._panel.query_one("#proposal-banner", ProposalBanner)
                assert "-has-proposals" not in banner.classes

    _run(body)


def test_proposal_review_modal_accepts(tmp_path, monkeypatch):
    """The modal's Accept action promotes a proposal to a discoverable skill."""
    from wells import config as config_mod
    from wells.tui import ProposalReviewScreen, WellsApp
    from wells import skill_authoring as sa
    from wells import skills as sk

    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_SAFETY", "auto")
    monkeypatch.setenv("WELLS_BUILTIN_SKILLS", "0")
    sk.clear_cache()
    pdir = tmp_path / ".wells" / "skill-proposals"
    pdir.mkdir(parents=True)
    (pdir / "from-run.md").write_text(
        "---\nname: from-run\ndescription: d.\nsource_goal: g\n"
        "status: proposal\n---\nBody.\n", encoding="utf-8",
    )

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(140, 40)) as pilot:
                screen = ProposalReviewScreen()
                app.push_screen(screen)
                await pilot.pause()
                # Select the first (only) proposal and accept.
                screen.query_one("#proposal-list").highlighted = 0
                screen.action_accept()
                await pilot.pause()
                # Proposal consumed; skill now discoverable.
                assert sa.list_proposals(str(tmp_path)) == []
                assert sk.skills_for(str(tmp_path)).by_name("from-run") is not None

    _run(body)
    sk.clear_cache()


def test_banner_click_handler_opens_review_modal(tmp_path, monkeypatch):
    """The app's on_review_requested handler pushes ProposalReviewScreen."""
    from wells import config as config_mod
    from wells.tui import ProposalBanner, ProposalReviewScreen, WellsApp

    monkeypatch.setattr(config_mod, "WORKSPACE_ROOT", str(tmp_path))

    async def body():
        with patch.object(WellsApp, "_ensure_repo_index", lambda self: None):
            app = WellsApp()
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                # Fire the handler directly (the banner posts this message on
                # click; here we exercise the app's handler end-to-end).
                app.on_review_requested(ProposalBanner.ReviewRequested())
                await pilot.pause()
                assert isinstance(app.screen, ProposalReviewScreen)

    _run(body)
