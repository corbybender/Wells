"""Web tools: web_search and fetch_url.

Design choice — zero-config by default, no 3rd-party service required:
Wells is a standalone tool, so ``web_search`` must work the moment it's
installed, with no Docker container, no signup, and no API key. The default
backend scrapes DuckDuckGo's plain HTML results page
(``html.duckduckgo.com/html/``) — no official API, but no key/quota either,
and it's the same approach several other no-config agent CLIs use for
exactly this reason. It's inherently more fragile than a real API (DuckDuckGo
can change its markup, or rate-limit a burst of queries), which is the
tradeoff for "just works, nothing to set up."

For anyone who already runs (or wants to run) a self-hosted SearXNG instance
(https://docs.searxng.org — a metasearch engine aggregating 70+ backends
behind a clean JSON API), setting ``WELLS_SEARXNG_URL`` switches to that
instead: more reliable, no scraping fragility, and no external network call
to DuckDuckGo at all if privacy matters. This is opt-in, never required.

Both tools are read-only (no workspace mutation) and gated by
``WELLS_WEB_TOOLS``.
"""

from __future__ import annotations

import html
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

from wells.tools import ToolContext, ToolDef, ToolResult

_DEFAULT_TIMEOUT = 15.0
_MAX_RESULTS = 10
_DEFAULT_RESULTS = 5
_MAX_FETCH_CHARS = int(os.environ.get("WELLS_FETCH_MAX_CHARS", "8000"))
_USER_AGENT = "Mozilla/5.0 (compatible; Wells-agentic-harness/1.0)"


def enabled() -> bool:
    return os.environ.get("WELLS_WEB_TOOLS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _searxng_url() -> str:
    return os.environ.get("WELLS_SEARXNG_URL", "").strip().rstrip("/")


# ---------------------------------------------------------------------------
# web_search — dispatches to SearXNG (opt-in) or DuckDuckGo (zero-config default)
# ---------------------------------------------------------------------------


def web_search(ctx: ToolContext, query: str, *, count: int = _DEFAULT_RESULTS) -> ToolResult:
    """Search the web. Uses a self-hosted SearXNG instance when
    WELLS_SEARXNG_URL is set, else DuckDuckGo's HTML results page — no
    setup required for the default path."""
    if not query or not query.strip():
        return ToolResult(False, "", "query is required")
    n = max(1, min(int(count or _DEFAULT_RESULTS), _MAX_RESULTS))
    base = _searxng_url()
    if base:
        return _web_search_searxng(base, query, n)
    return _web_search_duckduckgo(query, n)


def _web_search_searxng(base: str, query: str, n: int) -> ToolResult:
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


_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
# Each result: a result__a title link, then (possibly with other markup
# between) a result__snippet link. Non-greedy + DOTALL spans the gap.
_DDG_RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>',
    re.S,
)


def _extract_ddg_url(href: str) -> str:
    """DuckDuckGo's HTML wraps result links in a redirect
    (//duckduckgo.com/l/?uddg=<url-encoded-real-url>&...) — unwrap it."""
    full = href if href.startswith("http") else f"https:{href}"
    if "duckduckgo.com/l/" in full:
        qs = parse_qs(urlparse(full).query)
        real = qs.get("uddg", [""])[0]
        if real:
            return unquote(real)
    return full


def _strip_result_markup(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _web_search_duckduckgo(query: str, n: int) -> ToolResult:
    try:
        import httpx
        resp = httpx.get(
            _DDG_ENDPOINT, params={"q": query},
            headers={"User-Agent": _USER_AGENT},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:
        return ToolResult(
            False, "", f"web_search (DuckDuckGo) failed: {type(e).__name__}: {e}",
        )

    matches = _DDG_RESULT_RE.findall(resp.text)
    if not matches:
        return ToolResult(True, f"No results for {query!r}.", "")

    lines = [f"Search results for {query!r} (via DuckDuckGo):"]
    for i, (href, title_html, snippet_html) in enumerate(matches[:n], 1):
        title = _strip_result_markup(title_html) or "(no title)"
        snippet = _strip_result_markup(snippet_html)[:220]
        url = _extract_ddg_url(href)
        lines.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return ToolResult(True, "\n".join(lines), "")


WEB_SEARCH_TOOL = ToolDef(
    name="web_search",
    description=(
        "Search the web (DuckDuckGo by default — no setup required; uses a "
        "self-hosted SearXNG instance instead when WELLS_SEARXNG_URL is set). "
        "Use this to look up current library APIs, error messages, or anything "
        "not in the codebase/training data."
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
            headers={"User-Agent": _USER_AGENT},
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
