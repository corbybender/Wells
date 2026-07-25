"""Tests for the browser_* tools: registration, gating, guardrails.

Live-browser tests are skipped unless Playwright + a Chromium build are
actually installed (an optional extra, not part of the base dependency set --
see pyproject.toml's ``browser`` group).
"""

from __future__ import annotations

import pytest

from wells import browser, tools


# ---------------------------------------------------------------------------
# Registration + gating (no browser session required)
# ---------------------------------------------------------------------------


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("WELLS_BROWSER", raising=False)
    assert browser.enabled()


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("WELLS_BROWSER", "0")
    assert not browser.enabled()


def test_registered_by_default(monkeypatch):
    monkeypatch.delenv("WELLS_BROWSER", raising=False)
    tools._optional_registered = False
    tools._register_optional_tools()
    names = [t.name for t in tools.ALL_TOOLS]
    for n in (
        "browser_navigate", "browser_click", "browser_type",
        "browser_read", "browser_screenshot",
    ):
        assert n in names


def test_not_registered_when_disabled(monkeypatch):
    # ALL_TOOLS is a shared, additive-only module-level registry -- once a
    # tool is added in-process it's never removed, so an earlier test in
    # this same session may have already registered browser_* tools. Reset
    # to a browser-free baseline here so this test verifies the actual guard
    # condition instead of depending on test ordering.
    monkeypatch.setenv("WELLS_BROWSER", "0")
    fresh = [t for t in tools.ALL_TOOLS if not t.name.startswith("browser_")]
    monkeypatch.setattr(tools, "ALL_TOOLS", fresh)
    tools._optional_registered = False
    tools._register_optional_tools()
    names = [t.name for t in tools.ALL_TOOLS]
    assert "browser_navigate" not in names


def test_click_without_session_errors():
    ctx = tools.ToolContext(workspace=".")
    r = browser.browser_click(ctx, "a")
    assert not r.ok
    assert "browser_navigate" in r.error


def test_type_without_session_errors():
    ctx = tools.ToolContext(workspace=".")
    r = browser.browser_type(ctx, "input", "hi")
    assert not r.ok
    assert "browser_navigate" in r.error


def test_read_without_session_errors():
    ctx = tools.ToolContext(workspace=".")
    r = browser.browser_read(ctx)
    assert not r.ok
    assert "browser_navigate" in r.error


def test_navigate_requires_url():
    ctx = tools.ToolContext(workspace=".")
    r = browser.browser_navigate(ctx, "")
    assert not r.ok


def test_click_requires_target():
    ctx = tools.ToolContext(workspace=".")
    r = browser.browser_click(ctx, "")
    assert not r.ok


def test_type_requires_target():
    ctx = tools.ToolContext(workspace=".")
    r = browser.browser_type(ctx, "", "hi")
    assert not r.ok


# ---------------------------------------------------------------------------
# Live browser tests
# ---------------------------------------------------------------------------

def _chromium_available() -> bool:
    try:
        import os
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except ImportError:
        return False
    except Exception:
        return False


# A single skipif (not module-level importorskip) so only the live-browser
# tests below are skipped when Playwright/Chromium aren't installed --
# importorskip at module scope would skip the registration/gating tests
# above too, which don't need a real browser at all.
requires_chromium = pytest.mark.skipif(
    not _chromium_available(),
    reason="playwright/chromium not installed (optional 'browser' extra: "
    "pip install 'wells[browser]' && playwright install chromium)",
)


@pytest.fixture(autouse=True, scope="module")
def _close_browser_after_module():
    yield
    browser._close_all()


@requires_chromium
def test_navigate_and_read_local_html(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        "<html><body><h1>Hi</h1><a href='#'>Click me</a><input name='q'></body></html>",
        encoding="utf-8",
    )
    ctx = tools.ToolContext(workspace=str(tmp_path))
    r = browser.browser_navigate(ctx, html_file.as_uri())
    assert r.ok
    assert "Hi" in r.output
    assert "Click me" in r.output


@requires_chromium
def test_click_by_css_selector(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        "<html><body><button id='btn' "
        "onclick=\"document.title='clicked'\">Go</button></body></html>",
        encoding="utf-8",
    )
    ctx = tools.ToolContext(workspace=str(tmp_path))
    browser.browser_navigate(ctx, html_file.as_uri())
    r = browser.browser_click(ctx, "#btn")
    assert r.ok


@requires_chromium
def test_type_into_input(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        "<html><body><input id='box' value=''></body></html>", encoding="utf-8"
    )
    ctx = tools.ToolContext(workspace=str(tmp_path))
    browser.browser_navigate(ctx, html_file.as_uri())
    r = browser.browser_type(ctx, "#box", "hello world")
    assert r.ok


@requires_chromium
def test_click_respects_plan_mode(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        "<html><body><button id='btn'>Go</button></body></html>", encoding="utf-8"
    )
    ctx = tools.ToolContext(workspace=str(tmp_path), plan_mode=True)
    browser.browser_navigate(ctx, html_file.as_uri())
    r = browser.browser_click(ctx, "#btn")
    assert r.simulated
    assert "plan" in r.output.lower()


@requires_chromium
def test_click_respects_dryrun(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        "<html><body><button id='btn'>Go</button></body></html>", encoding="utf-8"
    )
    ctx = tools.ToolContext(workspace=str(tmp_path), safety="dryrun")
    browser.browser_navigate(ctx, html_file.as_uri())
    r = browser.browser_click(ctx, "#btn")
    assert r.simulated
    assert "dry-run" in r.output.lower()


@requires_chromium
def test_screenshot_without_describe(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text("<html><body><h1>Hi</h1></body></html>", encoding="utf-8")
    ctx = tools.ToolContext(workspace=str(tmp_path))
    browser.browser_navigate(ctx, html_file.as_uri())
    r = browser.browser_screenshot(ctx, describe=False)
    assert r.ok
    assert ".png" in r.output
