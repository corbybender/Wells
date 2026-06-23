"""Tests for the MCP server tool registration and fast-path tool invocations.

Slow tools (``run_agent_task``, ``plan_task``, ``review_code``) that require
LLM calls are NOT tested here — they are verified in the manual smoke test and
the full-harness acceptance tests.
"""

from __future__ import annotations

import asyncio

import pytest

from coding_harness.config import ZAI_MODEL
from coding_harness.mcp_server import (
    _compress_logs,
    _get_harness_info,
    handle_call_tool,
    handle_list_tools,
    server,
)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_server_name() -> None:
    assert server.name == "coding-harness"


def test_tool_count() -> None:
    tools = asyncio.run(handle_list_tools())
    assert len(tools) == 5


@pytest.mark.parametrize(
    "name, props",
    [
        ("run_agent_task", {"goal"}),
        ("plan_task", {"goal"}),
        ("review_code", {"goal", "implementation_steps"}),
        ("compress_logs", {"text"}),
        ("get_harness_info", set()),
    ],
)
def test_tool_schema(name: str, props: set) -> None:
    tools = asyncio.run(handle_list_tools())
    tool = next(t for t in tools if t.name == name)
    assert tool.name == name
    assert props.issubset(set(tool.inputSchema.get("properties", {})))
    for required in props:
        assert required in tool.inputSchema.get("required", [])


# ---------------------------------------------------------------------------
# Fast-path tool invocations
# ---------------------------------------------------------------------------


def test_compress_logs() -> None:
    result = _compress_logs({"text": "hello\nworld\nworld\nworld\nerror\n\n\n"})
    assert len(result) == 1
    assert result[0].type == "text"
    text = result[0].text
    assert "hello" in text
    assert "error" in text
    assert "compressed" in text


def test_compress_logs_with_tail() -> None:
    text = "\n".join(f"line {i}" for i in range(200))
    result = _compress_logs({"text": text, "tail_lines": 10})
    lines = result[0].text.splitlines()
    assert 1 <= len(lines) <= 14


def test_get_harness_info() -> None:
    result = _get_harness_info({})
    text = result[0].text
    assert "coding-harness" in text
    assert ZAI_MODEL in text


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_unknown_tool_returns_error() -> None:
    result = asyncio.run(handle_call_tool("nonexistent_tool", {}))
    text = result[0].text
    assert "Unknown tool" in text or "error" in text.lower()


@pytest.mark.parametrize(
    "name, bad_args",
    [
        ("compress_logs", {}),
        ("run_agent_task", {}),
        ("plan_task", {}),
    ],
)
def test_missing_required_arg(name: str, bad_args: dict) -> None:
    result = asyncio.run(handle_call_tool(name, bad_args))
    text = result[0].text
    assert "error" in text.lower()
