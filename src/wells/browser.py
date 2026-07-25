"""Browser: let the agent drive a real, JS-rendered browser session.

``fetch_url`` only ever sees the initial static HTML — most real web apps
(SPAs, dashboards, a local dev server's frontend) render their actual content
with JavaScript, so ``fetch_url`` against them returns an empty shell. This
module gives the agent a genuine browser (Playwright + Chromium) it can
navigate, click, type into, read the rendered text of, and screenshot — the
same category of capability OpenHands' browsing agent provides, but opt-in
and lazily imported so it costs nothing until actually used.

Guardrails:
  * On by default, same as the other agent capabilities (``WELLS_BROWSER=0``
    opts out). Playwright itself still needs a separate one-time install
    (``pip install 'wells[browser]'`` + ``playwright install chromium`` —
    not part of the base dependency set, so a fresh `wells` install never
    pays for a Chromium download it doesn't use) — a call made before that
    step returns a clear, actionable error instead of the tool silently not
    existing, the same pattern the ``anthropic``/``ollama``/``google``
    provider profiles already use for their optional packages.
  * ``browser_navigate`` / ``browser_read`` / ``browser_screenshot`` are
    read-only (no safety gate, mirroring ``fetch_url``). ``browser_click`` /
    ``browser_type`` can have real side effects (submit a form, trigger a
    purchase, click "delete") and go through the same plan/approve/dryrun
    gate as every other mutating tool.
  * One browser session per process, launched lazily on first use and closed
    at process exit (``atexit``) — cookies/login state persist across calls
    within a session, the same way a human keeps one tab open.
  * ``browser_screenshot`` routes the PNG through the harness's own
    vision-profile routing (:mod:`wells.vision` + ``config.vision_profile_name``)
    to get a text description back, so even a non-vision active model can
    effectively "see" the page.
"""

from __future__ import annotations

import atexit
import os
import tempfile
import time
from pathlib import Path

from wells import safety
from wells.tools import ToolContext, ToolDef, ToolResult

_MAX_TEXT_CHARS = 8000
_DEFAULT_TIMEOUT_MS = 15000
_SETTLE_MS = 300  # brief pause after nav/click/type so SPA JS can react

_playwright = None
_browser = None
_page = None


def enabled() -> bool:
    """Whether the browser_* tools are registered (on by default; ``WELLS_BROWSER=0`` opts out)."""
    return os.environ.get("WELLS_BROWSER", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _ensure_page():
    """Lazily launch a headless Chromium session; reused across calls/tools."""
    global _playwright, _browser, _page
    if _page is not None:
        return _page
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: uv pip install playwright && "
            "uv run playwright install chromium"
        ) from exc
    _playwright = sync_playwright().start()
    headless = os.environ.get("WELLS_BROWSER_HEADLESS", "1").strip().lower() not in (
        "0", "false", "no",
    )
    _browser = _playwright.chromium.launch(headless=headless)
    _page = _browser.new_page()
    _page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    return _page


@atexit.register
def _close_all() -> None:
    global _playwright, _browser, _page
    try:
        if _browser is not None:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright is not None:
            _playwright.stop()
    except Exception:
        pass
    _playwright = _browser = _page = None


def _truncate(text: str, limit: int = _MAX_TEXT_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated, {len(text) - limit} more chars)"


def _visible_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def _locate(page, target: str):
    """Best-effort element lookup: CSS selector first, then visible text."""
    try:
        loc = page.locator(target)
        if loc.count() > 0:
            return loc.first
    except Exception:
        pass
    return page.get_by_text(target, exact=False).first


def browser_navigate(ctx: ToolContext, url: str) -> ToolResult:
    """Navigate the browser session to ``url``; return title + rendered text."""
    if not url or not url.strip():
        return ToolResult(False, "", "url is required")
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        page = _ensure_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(_SETTLE_MS)
    except Exception as e:
        return ToolResult(
            False, "", f"browser_navigate failed for {url!r}: {type(e).__name__}: {e}"
        )
    text = _truncate(_visible_text(page))
    return ToolResult(True, f"[{page.url} — {page.title()!r}]\n\n{text}", "")


def browser_click(ctx: ToolContext, target: str) -> ToolResult:
    """Click an element identified by a CSS selector or its visible text."""
    if not target or not target.strip():
        return ToolResult(False, "", "target is required")
    if _page is None:
        return ToolResult(False, "", "No active browser session — call browser_navigate first.")
    detail = f"{target!r} on {_page.url}"
    if ctx.plan_mode:
        return ToolResult(True, f"[plan] would browser_click {detail}", simulated=True)
    decision = safety.gate("browser_click", detail, safety=ctx.safety, approver=ctx.approver)
    if not decision.allowed:
        return ToolResult(True, decision.reason, simulated=decision.simulated)
    try:
        _locate(_page, target).click(timeout=_DEFAULT_TIMEOUT_MS)
        _page.wait_for_timeout(_SETTLE_MS)
    except Exception as e:
        return ToolResult(False, "", f"browser_click failed for {target!r}: {type(e).__name__}: {e}")
    return ToolResult(True, f"Clicked {target!r}. Now at {_page.url} ({_page.title()!r}).", "")


def browser_type(ctx: ToolContext, target: str, text: str, *, submit: bool = False) -> ToolResult:
    """Type ``text`` into an input identified by a CSS selector or visible label/placeholder."""
    if not target or not target.strip():
        return ToolResult(False, "", "target is required")
    if _page is None:
        return ToolResult(False, "", "No active browser session — call browser_navigate first.")
    detail = f"{len(text)} chars into {target!r} on {_page.url}"
    if ctx.plan_mode:
        return ToolResult(True, f"[plan] would browser_type {detail}", simulated=True)
    decision = safety.gate("browser_type", detail, safety=ctx.safety, approver=ctx.approver)
    if not decision.allowed:
        return ToolResult(True, decision.reason, simulated=decision.simulated)
    try:
        el = _locate(_page, target)
        el.fill(text, timeout=_DEFAULT_TIMEOUT_MS)
        if submit:
            el.press("Enter")
            _page.wait_for_timeout(_SETTLE_MS)
    except Exception as e:
        return ToolResult(False, "", f"browser_type failed for {target!r}: {type(e).__name__}: {e}")
    suffix = " and submitted" if submit else ""
    return ToolResult(True, f"Typed into {target!r}{suffix}. Now at {_page.url}.", "")


def browser_read(ctx: ToolContext) -> ToolResult:
    """Return the current page's rendered visible text (after JS runs)."""
    if _page is None:
        return ToolResult(False, "", "No active browser session — call browser_navigate first.")
    text = _truncate(_visible_text(_page))
    return ToolResult(True, f"[{_page.url} — {_page.title()!r}]\n\n{text}", "")


def browser_screenshot(ctx: ToolContext, *, describe: bool = True) -> ToolResult:
    """Screenshot the current page; describe it via the vision profile by default."""
    if _page is None:
        return ToolResult(False, "", "No active browser session — call browser_navigate first.")
    path = Path(tempfile.gettempdir()) / f"wells-browser-{int(time.time() * 1000)}.png"
    try:
        _page.screenshot(path=str(path))
    except Exception as e:
        return ToolResult(False, "", f"browser_screenshot failed: {type(e).__name__}: {e}")
    if not describe:
        return ToolResult(True, f"Screenshot saved to {path}", "")
    try:
        from langchain_core.messages import HumanMessage
        from wells import config, vision as _vision

        llm = config.providers.get_chat_model(config.vision_profile_name(), temperature=0.1)
        content = _vision.build_multimodal_content(
            "Describe this webpage screenshot: overall layout, all visible "
            "text, and every interactive element (buttons/links/inputs) with "
            "its visible label or placeholder — enough detail to decide what "
            "to click or type next without seeing the image directly.",
            [str(path)],
        )
        resp = llm.invoke([HumanMessage(content=content)])
        desc = getattr(resp, "content", "") or ""
        return ToolResult(True, f"[{_page.url}] Screenshot saved to {path}.\n\n{desc}", "")
    except Exception as e:
        return ToolResult(
            True,
            f"Screenshot saved to {path} (description unavailable: "
            f"{type(e).__name__}: {e}). Configure MODEL_PROFILE_VISION in "
            f"/config to enable descriptions.",
            "",
        )


# ---------------------------------------------------------------------------
# Tool descriptors
# ---------------------------------------------------------------------------

BROWSER_NAVIGATE_TOOL = ToolDef(
    name="browser_navigate",
    description=(
        "Navigate a real, JS-rendered browser session to a URL and return its "
        "title + rendered visible text. Use this instead of fetch_url for "
        "single-page apps, dashboards, or any site whose content is built by "
        "JavaScript (fetch_url only sees the initial static HTML)."
    ),
    input_schema={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "URL to open"}},
        "required": ["url"],
    },
    handler=browser_navigate,
    mutating=False,
)

BROWSER_CLICK_TOOL = ToolDef(
    name="browser_click",
    description=(
        "Click an element on the current browser page, identified by a CSS "
        "selector or its visible text (falls back to a text search if the "
        "selector doesn't match anything). Requires browser_navigate first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "CSS selector or visible text"},
        },
        "required": ["target"],
    },
    handler=browser_click,
    mutating=True,
)

BROWSER_TYPE_TOOL = ToolDef(
    name="browser_type",
    description=(
        "Type text into an input on the current browser page, identified by a "
        "CSS selector or visible label/placeholder. Set submit=true to press "
        "Enter afterward. Requires browser_navigate first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "CSS selector or visible label/placeholder",
            },
            "text": {"type": "string", "description": "Text to type"},
            "submit": {
                "type": "boolean",
                "description": "Press Enter after typing",
                "default": False,
            },
        },
        "required": ["target", "text"],
    },
    handler=browser_type,
    mutating=True,
)

BROWSER_READ_TOOL = ToolDef(
    name="browser_read",
    description=(
        "Return the current browser page's rendered visible text (after JS "
        "runs). Requires browser_navigate first."
    ),
    input_schema={"type": "object", "properties": {}},
    handler=browser_read,
    mutating=False,
)

BROWSER_SCREENSHOT_TOOL = ToolDef(
    name="browser_screenshot",
    description=(
        "Screenshot the current browser page and get back a text description "
        "of its layout and every interactive element — useful for visual "
        "layouts, canvases, or anything text extraction can't capture. Routes "
        "through the configured vision profile automatically (MODEL_PROFILE_VISION), "
        "so this works even when the active model isn't vision-capable. Set "
        "describe=false to just save the file without describing it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "describe": {
                "type": "boolean",
                "description": "Describe the screenshot via the vision profile",
                "default": True,
            },
        },
    },
    handler=browser_screenshot,
    mutating=False,
)
