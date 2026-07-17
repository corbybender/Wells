"""Tests for the web tools: web_search (SearXNG) and fetch_url."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from wells import tools, web
from wells.tools import ToolContext


@pytest.fixture
def ctx(tmp_path) -> ToolContext:
    return ToolContext(workspace=str(tmp_path), safety="auto")


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------


def test_web_search_reports_not_configured_without_searxng_url(ctx, monkeypatch):
    monkeypatch.delenv("WELLS_SEARXNG_URL", raising=False)
    result = web.web_search(ctx, "how does asyncio work")
    assert result.ok is False
    assert "WELLS_SEARXNG_URL" in result.error


def test_web_search_requires_a_query(ctx, monkeypatch):
    monkeypatch.setenv("WELLS_SEARXNG_URL", "http://localhost:8080")
    result = web.web_search(ctx, "")
    assert result.ok is False


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_web_search_formats_results(ctx, monkeypatch):
    monkeypatch.setenv("WELLS_SEARXNG_URL", "http://localhost:8080")
    payload = {
        "results": [
            {"title": "asyncio docs", "url": "https://docs.python.org/asyncio",
             "content": "Asynchronous I/O framework."},
            {"title": "Real Python asyncio", "url": "https://realpython.com/asyncio",
             "content": "A guide to asyncio."},
        ]
    }
    with patch("httpx.get", return_value=_FakeResp(payload)) as mock_get:
        result = web.web_search(ctx, "asyncio", count=2)
    assert result.ok is True
    assert "asyncio docs" in result.output
    assert "docs.python.org/asyncio" in result.output
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["params"]["format"] == "json"
    assert call_kwargs["params"]["q"] == "asyncio"


def test_web_search_strips_trailing_slash_from_base_url(ctx, monkeypatch):
    monkeypatch.setenv("WELLS_SEARXNG_URL", "http://localhost:8080/")
    with patch("httpx.get", return_value=_FakeResp({"results": []})) as mock_get:
        web.web_search(ctx, "x")
    called_url = mock_get.call_args.args[0]
    assert called_url == "http://localhost:8080/search"  # no double slash


def test_web_search_no_results(ctx, monkeypatch):
    monkeypatch.setenv("WELLS_SEARXNG_URL", "http://localhost:8080")
    with patch("httpx.get", return_value=_FakeResp({"results": []})):
        result = web.web_search(ctx, "zzzqqqnonexistent")
    assert result.ok is True
    assert "No results" in result.output


def test_web_search_network_failure_reported_not_raised(ctx, monkeypatch):
    monkeypatch.setenv("WELLS_SEARXNG_URL", "http://localhost:8080")
    with patch("httpx.get", side_effect=ConnectionError("refused")):
        result = web.web_search(ctx, "x")  # must not raise
    assert result.ok is False
    assert "failed" in result.error


def test_web_search_count_clamped_to_max(ctx, monkeypatch):
    monkeypatch.setenv("WELLS_SEARXNG_URL", "http://localhost:8080")
    payload = {"results": [
        {"title": f"r{i}", "url": f"https://x.com/{i}", "content": "c"} for i in range(20)
    ]}
    with patch("httpx.get", return_value=_FakeResp(payload)):
        result = web.web_search(ctx, "x", count=999)
    assert result.output.count("https://x.com/") == web._MAX_RESULTS


# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------


def test_fetch_url_requires_url(ctx):
    result = web.fetch_url(ctx, "")
    assert result.ok is False


def test_fetch_url_rejects_non_http_schemes(ctx):
    result = web.fetch_url(ctx, "file:///etc/passwd")
    assert result.ok is False
    assert "http" in result.error.lower()


class _FakeHttpResp:
    def __init__(self, text, content_type="text/html", status=200):
        self.text = text
        self.headers = {"content-type": content_type}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_fetch_url_strips_html_tags(ctx):
    html_body = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><script>evil()</script><h1>Title</h1>"
        "<p>Hello &amp; welcome.</p></body></html>"
    )
    with patch("httpx.get", return_value=_FakeHttpResp(html_body)):
        result = web.fetch_url(ctx, "https://example.com/page")
    assert result.ok is True
    assert "Title" in result.output
    assert "Hello & welcome." in result.output
    assert "evil()" not in result.output
    assert "color:red" not in result.output
    assert "<h1>" not in result.output


def test_fetch_url_plain_text_passthrough(ctx):
    with patch("httpx.get", return_value=_FakeHttpResp("raw plain text", "text/plain")):
        result = web.fetch_url(ctx, "https://example.com/readme.txt")
    assert "raw plain text" in result.output


def test_fetch_url_truncates_long_content(ctx):
    long_text = "x" * 20000
    with patch("httpx.get", return_value=_FakeHttpResp(long_text, "text/plain")):
        result = web.fetch_url(ctx, "https://example.com/big", max_chars=100)
    assert "truncated" in result.output
    assert len(result.output) < 20000


def test_fetch_url_network_failure_reported_not_raised(ctx):
    with patch("httpx.get", side_effect=TimeoutError("slow")):
        result = web.fetch_url(ctx, "https://example.com")  # must not raise
    assert result.ok is False


# ---------------------------------------------------------------------------
# Registration + gating
# ---------------------------------------------------------------------------


def test_web_tools_registered_in_all_and_read_tools():
    tools._ensure_optional_registered()
    all_names = {t.name for t in tools.ALL_TOOLS}
    read_names = {t.name for t in tools.READ_TOOLS}
    assert "web_search" in all_names and "fetch_url" in all_names
    assert "web_search" in read_names and "fetch_url" in read_names  # read-only


def test_web_tools_disabled_via_env(monkeypatch):
    monkeypatch.setenv("WELLS_WEB_TOOLS", "0")
    assert web.enabled() is False
