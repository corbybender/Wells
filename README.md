# coding-harness

A local, LangGraph-based **agentic coding harness**. Give it a software
development goal and it runs an orchestration loop of
`planner → architect → coder → tester → reviewer` stages, looping the coder
back when the reviewer is not satisfied (up to a configurable number of times).

It talks to the **Z.ai (ZhipuAI) OpenAI-compatible API** through LangChain's
`ChatOpenAI` client (default model: `glm-5.1`).

> **Note (v1):** This harness currently *plans and reasons* about the work — it
> produces a development plan, architecture proposal, implementation steps, test
> plan, review result and a final summary. It does **not** edit files yet. See
> the `TODO` markers in the source for where real file editing, shell execution,
> OpenHands integration and GitHub PR creation will plug in later.

## Project structure

```
.
├── .env                         # your local credentials (not committed)
├── .env.example                 # template — copy this to .env
├── pyproject.toml               # uv project definition + dependencies
├── README.md
├── tests/
│   └── test_mcp_server.py       # MCP server tests
└── src/coding_harness/
    ├── __init__.py
    ├── __main__.py              # python -m coding_harness → MCP server
    ├── main.py                  # CLI entry point (coding-harness)
    ├── config.py                # env vars, LLM client, model router, budgets
    ├── state.py                 # TypedDict LangGraph state
    ├── graph.py                 # LangGraph workflow (includes summarizer)
    ├── tokens.py                # token estimation, accounting, usage report
    ├── context.py               # ContextManager: categorised + trimmed prompts
    ├── compress.py              # log/output compressor (ANSI, dup, tail)
    ├── summarize.py             # rolling task-state summarizer
    ├── runtime.py               # run_step(): LLM call + usage capture
    ├── mcp_server.py            # MCP server (run_agent_task, plan_task, …)
    └── agents/
        ├── planner.py
        ├── architect.py
        ├── coder.py
        ├── tester.py
        └── reviewer.py
```

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

```bash
uv sync                 # install dependencies
cp .env.example .env    # then edit .env and add your ZAI_API_KEY
```

Your `.env` should look like:

```
ZAI_API_KEY=<your key>
ZAI_ENDPOINT=https://api.z.ai/api/coding/paas/v4/
ZAI_MODEL=glm-5.1
MAX_ITERATIONS=3
```

## Run the harness

```bash
# Via the installed console script (anywhere in the venv):
coding-harness "Build a Payload CMS HTML-to-schema converter"

# Or via uv:
uv run coding-harness "Build a tiny CLI that counts words in a file"
```

The harness prints progress for each stage, a full report (plan, architecture,
implementation, test plan, review, final summary), and a **Token Usage Report**
showing actual input/output/reasoning tokens per step with estimated category
spend and tokens saved by trimming/summarization.

## Workflow

```
START → planner → architect → coder → tester → reviewer → decision
         ^                                            |
         |____ summarizer ← (if INCOMPLETE) __________|
```

- If the reviewer says `COMPLETE` the run ends.
- If `INCOMPLETE`, the **summarizer** condenses durable context (plan + architecture)
  and control returns to `coder`.
- The loop stops after `MAX_ITERATIONS` (default `3`) coder runs regardless of
  the reviewer's verdict.

## MCP server

The harness exposes its capabilities as a [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio, so external agent clients (Claude Code, OpenCode, Codex-style
CLIs, Gemini CLI, etc.) can invoke planning, review, and log-compression.

### Start the server

```bash
# Console script (recommended):
coding-harness-mcp

# Or via python -m:
python -m coding_harness.mcp_server

# Or via the package __main__:
python -m coding_harness
```

### Exposed tools

| Tool | Input | Description |
|------|-------|-------------|
| `run_agent_task` | `goal`, `max_iterations?` | Full harness loop (planner→…→reviewer) |
| `plan_task` | `goal` | Planner + architect only (fast) |
| `review_code` | `goal`, `plan?`, `architecture?`, `implementation_steps`, `test_plan?` | Reviewer only |
| `compress_logs` | `text`, `tail_lines?` | Compress log output (ANSI/dup/tail) |
| `get_harness_info` | — | Current configuration info |

### Client configuration examples

**Claude Code (VS Code extension):** set the MCP server command to
`coding-harness-mcp` (ensure coding-harness is installed or use the full path).

**Generic MCP client (stdio):**
```json
{
  "mcpServers": {
    "coding-harness": {
      "command": "coding-harness-mcp",
      "args": []
    }
  }
}
```

### Architecture

```
External MCP Client
    ↓  (stdio JSON-RPC)
MCP Server Interface  (mcp_server.py — thin, ≤ 200 lines)
    ↓  delegates to
Existing Harness Core (graph, agents, context, compress, tokens)
```

The MCP layer is deliberately thin — it does **not** duplicate orchestration
logic. All tool implementations call the same `run_step()`, agent functions,
and context manager used by the CLI.

## Token optimization

The harness integrates a token-optimization layer that:

| Component | What it does | Phase (from spec) |
|-----------|-------------|-------------------|
| **Estimator** | tiktoken-based, auto-calibrated against actual API responses | 1 |
| **TokenLedger** | Per-step actuals (input, output, reasoning, cache_read) from `usage_metadata` | 1 |
| **Token Usage Report** | End-of-run report with per-step table + category breakdown + savings | 1 |
| **ContextManager** | Categorised chunks, stable-prefix ordering, priority budget trimming | 1 |
| **Compressor** | ANSI strip, duplicate/blank collapse, tail-window, traceback preserve | 2 |
| **Summarizer** | Rolling task-state summary on loop iterations (threshold-guarded, no extra call when small) | 3 |
| **Model Router** | Cheaper model for summarization/compression; configurable via `ZAI_MODEL_CHEAP` | 5 |
| **Prompt-Cache Prefix** | `SystemMessage` + deterministic chunk order (cache-friendly) | 5 |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ZAI_API_KEY` | _(required)_ | Z.ai API key |
| `ZAI_ENDPOINT` | `https://api.z.ai/api/paas/v4/` | OpenAI-compatible base URL |
| `ZAI_MODEL` | `glm-5.2` | Main model |
| `ZAI_MODEL_CHEAP` | _(blank → main)_ | Cheaper model for low-stakes subtasks |
| `MAX_ITERATIONS` | `3` | Max coder→reviewer loops |
| `TOKEN_BUDGET_MAX_INPUT` | `24000` | Input budget per call (trims below this) |
| `SUMMARIZE_ON_LOOP` | `1` | Use task summary on loop iterations |
| `SUMMARIZE_THRESHOLD` | `1500` | Threshold (tokens) above which plan+arch is summarized |

## Tests

```bash
uv run python -m pytest tests/ -v
```

## Roadmap (TODOs in source)

- Real file editing in `src/coding_harness/agents/coder.py`.
- Shell command execution (lint/build/test) in `coder.py` / `tester.py`.
- OpenHands integration for autonomous tool use.
- GitHub PR / issue creation from the final summary in `src/main.py`.
- Async task tracking for MCP `run_agent_task` (return a task ID, poll later).
- Per-call ledger isolation for concurrent MCP requests.
