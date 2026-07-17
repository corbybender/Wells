"""Web tools: web_search (via a self-hosted SearXNG instance) and fetch_url.

Design choice — SearXNG over a commercial search API: SearXNG
(https://docs.searxng.org) is a free, self-hostable metasearch engine that
aggregates 70+ backends and exposes a clean JSON API (``?format=json``). No
per-query cost, no API key to provision, no vendor lock-in — a user runs
their own instance (a single Docker container) and points Wells at it. This
matches the harness's broader "works without a subscription" stance: a team
that's already self-hosting Ollama for the model can self-host SearXNG for
search with the same philosophy.

Both tools are read-only (no workspace mutation) and gated by
``WELLS_WEB_TOOLS``. ``web_search`` degrades gracefully — with no
``WELLS_SEARXNG_URL`` configured it reports exactly that instead of failing
opaquely, the same pattern used elsewhere for optional infra (Ollama warm-up,
structured outputs).
"""

from __future__ import annotations

import html
import os
import re
from urllib.parse import urlparse

from wells.tools import ToolContext, ToolDef, ToolResult

_DEFAULT_TIMEOUT = 15.0
_MAX_RESULTS = 10
_DEFAULT_RESULTS = 5
_MAX_FETCH_CHARS = int(os.environ.get("WELLS_FETCH_MAX_CHARS", "8000"))


def enabled() -> bool:
    return os.environ.get("WELLS_WEB_TOOLS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _searxng_url() -> str:
    return os.environ.get("WELLS_SEARXNG_URL", "").strip().rstrip("/")


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


def web_search(ctx: ToolContext, query: str, *, count: int = _DEFAULT_RESULTS) -> ToolResult:
    """Query a self-hosted SearXNG instance's JSON API and return top results."""
    if not query or not query.strip():
        return ToolResult(False, "", "query is required")
    base = _searxng_url()
    if not base:
        return ToolResult(
            False, "",
            "web_search is not configured. Set WELLS_SEARXNG_URL to a running "
            "SearXNG instance's base URL (self-hosted metasearch engine — see "
            "https://docs.searxng.org, a single Docker container is enough). "
            "The instance must have JSON output enabled "
            "(search.formats includes 'json' in searxng settings.yml).",
        )
    n = max(1, min(int(count or _DEFAULT_RESULTS), _MAX_RESULTS))

    try:
        import httpx
        resp = httpx.get(
            f"{base}/search",
            params={"q": query, "format": "json"},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return ToolResult(
            False, "",
            f"web_search request to {base} failed: {type(e).__name__}: {e}",
        )

    results = data.get("results") or []
    if not results:
        return ToolResult(True, f"No results for {query!r}.", "")

    lines = [f"Search results for {query!r} (via SearXNG):"]
    for i, r in enumerate(results[:n], 1):
        title = (r.get("title") or "(no title)").strip()
        url = (r.get("url") or "").strip()
        snippet = " ".join((r.get("content") or "").split())[:220]
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return ToolResult(True, "\n".join(lines), "")


WEB_SEARCH_TOOL = ToolDef(
    name="web_search",
    description=(
        "Search the web via a self-hosted SearXNG metasearch instance. Use this "
        "to look up current library APIs, error messages, or anything not in the "
        "codebase/training data. Requires WELLS_SEARXNG_URL to be configured — "
        "reports clearly if it isn't rather than failing opaquely."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {
                "type": "integer",
                "description": f"Number of results (default {_DEFAULT_RESULTS}, max {_MAX_RESULTS})",
                "default": _DEFAULT_RESULTS,
            },
        },
        "required": ["query"],
    },
    handler=web_search,
    mutating=False,
)


# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _html_to_text(raw: str) -> str:
    """Minimal HTML -> readable text: strip script/style, tags, unescape entities.

    Not a real renderer — no layout, no link extraction — but good enough for
    reading documentation/articles/error pages, and adds zero new dependencies.
    """
    no_scripts = _SCRIPT_STYLE_RE.sub(" ", raw)
    no_tags = _TAG_RE.sub("\n", no_scripts)
    text = html.unescape(no_tags)
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


def fetch_url(ctx: ToolContext, url: str, *, max_chars: int = 0) -> ToolResult:
    """Fetch ``url`` and return its readable text content, truncated."""
    if not url or not url.strip():
        return ToolResult(False, "", "url is required")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ToolResult(False, "", f"Refusing to fetch {url!r}: only http/https URLs are supported.")

    limit = max_chars if max_chars and max_chars > 0 else _MAX_FETCH_CHARS
    try:
        import httpx
        resp = httpx.get(
            url, timeout=_DEFAULT_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": "Wells-agentic-harness/1.0"},
        )
        resp.raise_for_status()
    except Exception as e:
        return ToolResult(False, "", f"fetch_url failed for {url!r}: {type(e).__name__}: {e}")

    ctype = resp.headers.get("content-type", "")
    body = resp.text
    text = _html_to_text(body) if "html" in ctype.lower() else body.strip()

    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + f"\n… (truncated, {len(text) - limit} more chars)"
    header = f"[{url} — {resp.status_code}, {ctype.split(';')[0] or 'unknown type'}]"
    return ToolResult(True, f"{header}\n\n{text}", "")


FETCH_URL_TOOL = ToolDef(
    name="fetch_url",
    description=(
        "Fetch a URL and return its readable text content (HTML tags stripped). "
        "Use this to read documentation pages, changelogs, or issue trackers "
        "when you already have a specific URL — for finding one, use web_search "
        "first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full http(s) URL to fetch"},
            "max_chars": {
                "type": "integer",
                "description": "Max characters of text to return (default: WELLS_FETCH_MAX_CHARS)",
                "default": 0,
            },
        },
        "required": ["url"],
    },
    handler=fetch_url,
    mutating=False,
)
