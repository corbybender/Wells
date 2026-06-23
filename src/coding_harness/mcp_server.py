"""MCP server interface for the coding-agent harness.

Exposes the harness capabilities as Model Context Protocol tools so external
agent clients (Claude Code, OpenCode, Codex-style CLIs, Gemini CLI, etc.) can
call into the harness via stdio transport.

Start the server:

    coding-harness-mcp          # console script
    python -m coding_harness.mcp_server

Or (via a subcommand added to the main CLI):

    coding-harness mcp serve    # if enabled in a future iteration
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import mcp.server
import mcp.server.models
import mcp.server.stdio
import mcp.types as types

from coding_harness.agents.architect import architect
from coding_harness.agents.planner import planner
from coding_harness.agents.reviewer import reviewer  # for review_code
from coding_harness.compress import compress_output
from coding_harness.config import (
    MAX_ITERATIONS,
    ZAI_API_KEY,
    ZAI_ENDPOINT,
    ZAI_MODEL,
    ZAI_MODEL_CHEAP,
)
from coding_harness.graph import build_graph
from coding_harness.tokens import LEDGER

server = mcp.server.Server("coding-harness")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="run_agent_task",
            description="Run the full coding-agent harness with a software development goal. "
                        "Executes the planner -> architect -> coder -> tester -> reviewer loop "
                        "(up to max_iterations). Returns the full plan, architecture, "
                        "implementation steps, test plan, review, and a final summary.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Your software development goal",
                    },
                    "max_iterations": {
                        "type": "integer",
                        "description": "Max coder<->reviewer loops (default 3)",
                        "default": 3,
                    },
                },
                "required": ["goal"],
            },
        ),
        types.Tool(
            name="plan_task",
            description="Run only the planner and architect stages. Fast — produces a "
                        "development plan and architecture proposal without executing the "
                        "full coder/tester/reviewer loop.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Your software development goal",
                    },
                },
                "required": ["goal"],
            },
        ),
        types.Tool(
            name="review_code",
            description="Run the reviewer on provided implementation context. "
                        "Returns a DECISION (COMPLETE/INCOMPLETE) and detailed review.",
            inputSchema={
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "Original development goal",
                    },
                    "plan": {
                        "type": "string",
                        "description": "Development plan",
                    },
                    "architecture": {
                        "type": "string",
                        "description": "Architecture proposal",
                    },
                    "implementation_steps": {
                        "type": "string",
                        "description": "Implementation steps / pseudo-code",
                    },
                    "test_plan": {
                        "type": "string",
                        "description": "Test plan",
                    },
                },
                "required": ["goal", "implementation_steps"],
            },
        ),
        types.Tool(
            name="compress_logs",
            description="Compress a shell/test/build log blob for compact display. "
                        "Strips ANSI codes, deduplicates lines, collapses blank runs, "
                        "preserves tracebacks and error lines.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Raw log / output text to compress",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "Max lines to keep (default 160)",
                        "default": 160,
                    },
                },
                "required": ["text"],
            },
        ),
        types.Tool(
            name="get_harness_info",
            description="Return information about the harness configuration, including "
                        "the active model, endpoint, iteration cap, and token-optimisation "
                        "settings.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _format_output(**fields: Any) -> str:
    """Return a compact, structured text blob (JSON-like) for the client."""
    return json.dumps(fields, indent=2, default=str)


def _text_content(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        if name == "run_agent_task":
            return await _run_agent_task(arguments)
        if name == "plan_task":
            return await _plan_task(arguments)
        if name == "review_code":
            return await _review_code(arguments)
        if name == "compress_logs":
            return _compress_logs(arguments)
        if name == "get_harness_info":
            return _get_harness_info(arguments)
        return _text_content(_format_output(
            error=f"Unknown tool: {name}"
        ))
    except Exception as exc:
        return _text_content(_format_output(error=f"{type(exc).__name__}: {exc}"))


# -- run_agent_task ---------------------------------------------------------

def _run_graph_sync(goal: str, max_iters: int) -> dict:
    """Build + invoke the harness graph synchronously (runs in a thread)."""
    LEDGER.reset()
    app = build_graph()
    initial = {
        "goal": goal,
        "iteration": 0,
        "max_iterations": max_iters,
        "messages": [],
    }
    return app.invoke(initial)


async def _run_agent_task(args: dict) -> list[types.TextContent]:
    goal = args["goal"]
    max_iters = args.get("max_iterations", MAX_ITERATIONS)

    final_state = await asyncio.to_thread(_run_graph_sync, goal, max_iters)

    status = "COMPLETE" if final_state.get("review_complete") else "INCOMPLETE"
    extra = {}
    if final_state.get("development_plan"):
        extra["development_plan"] = final_state["development_plan"]
    if final_state.get("architecture"):
        extra["architecture"] = final_state["architecture"]
    if final_state.get("implementation_steps"):
        extra["implementation_steps"] = final_state["implementation_steps"]
    if final_state.get("test_plan"):
        extra["test_plan"] = final_state["test_plan"]
    if final_state.get("review_result"):
        extra["review_result"] = final_state["review_result"]
    extra["iterations_used"] = final_state.get("iteration", 0)
    extra["max_iterations"] = final_state.get("max_iterations", max_iters)

    return _text_content(_format_output(status=status, **extra))


# -- plan_task --------------------------------------------------------------

async def _plan_task(args: dict) -> list[types.TextContent]:
    goal = args["goal"]

    state: dict = {"goal": goal, "iteration": 0, "max_iterations": 1, "messages": []}

    def _run() -> dict:
        state.update(planner(state))
        if state.get("development_plan"):
            state.update(architect(state))
        return state

    final = await asyncio.to_thread(_run)
    return _text_content(_format_output(
        development_plan=final.get("development_plan", ""),
        architecture=final.get("architecture", ""),
    ))


# -- review_code ------------------------------------------------------------

async def _review_code(args: dict) -> list[types.TextContent]:
    goal = args["goal"]
    plan = args.get("plan", "")
    architecture = args.get("architecture", "")
    implementation_steps = args["implementation_steps"]
    test_plan = args.get("test_plan", "")

    state: dict = {
        "goal": goal,
        "development_plan": plan,
        "architecture": architecture,
        "implementation_steps": implementation_steps,
        "test_plan": test_plan,
        "iteration": 1,
        "max_iterations": 1,
        "messages": [],
    }

    def _run() -> dict:
        state.update(reviewer(state))
        return state

    final = await asyncio.to_thread(_run)
    return _text_content(_format_output(
        review_result=final.get("review_result", ""),
        review_complete=final.get("review_complete", False),
    ))


# -- compress_logs ----------------------------------------------------------

def _compress_logs(args: dict) -> list[types.TextContent]:
    text = args["text"]
    tail_lines = args.get("tail_lines", 160)
    result = compress_output(text, tail_lines=tail_lines)
    return _text_content(result)


# -- get_harness_info -------------------------------------------------------

def _get_harness_info(args: dict) -> list[types.TextContent]:
    info = {
        "package": "coding-harness",
        "version": "0.1.0",
        "model": ZAI_MODEL,
        "endpoint": ZAI_ENDPOINT,
        "cheap_model": ZAI_MODEL_CHEAP or "(same as main model)",
        "max_iterations": MAX_ITERATIONS,
        "api_key_configured": bool(ZAI_API_KEY),
        "transport": "stdio",
        # TODO: expose token-opt settings once they land in config module-level consts.
    }
    return _text_content(_format_output(**info))


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

async def start_server() -> None:
    """Run the MCP server over stdio transport."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            mcp.server.models.InitializationOptions(
                server_name="coding-harness",
                server_version="0.1.0",
                capabilities=types.ServerCapabilities(),
            ),
        )


def main() -> None:
    """Console-entrypoint and ``python -m`` entry point."""
    asyncio.run(start_server())


if __name__ == "__main__":
    main()
