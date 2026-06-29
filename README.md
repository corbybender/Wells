# coding-harness

A local, **model-agnostic agentic coding harness**. Give it a software
development goal and it runs an orchestration loop of
`planner → architect → coder → tester → reviewer → finisher`, where the
coder/tester/reviewer are **autonomous tool-using agents** that actually
read files, make edits, run tests, and verify their own work — Claude-Code /
OpenHands style. The harness is **provider-agnostic**: drive it with Z.ai GLM,
OpenAI, Anthropic, OpenRouter, Ollama, or any OpenAI-compatible endpoint.

## What it does

```
START → planner → architect → coder → tester → reviewer → decision
         ^                                           |
         |____ summarizer ← (if INCOMPLETE) _________|
                                                     ↓
                                          finisher (memory + git/PR) → END
```

- **Planner / architect** turn the goal into a plan + architecture (reads
  `AGENTS.md` project memory).
- **Coder** is an agentic loop: it uses `read_file` / `glob` / `grep` /
  `write_file` / `edit_file` / `run_command` / `run_tests` / `spawn_subagent`
  to actually implement the goal in your workspace, then verifies its work.
- **Tester** runs the real test suite and reports pass/fail with file:line refs.
- **Reviewer** independently re-checks the work (reads changed files, re-runs
  tests) and emits `COMPLETE` / `INCOMPLETE`.
- On `INCOMPLETE`, the **summarizer** condenses durable context and the loop
  returns to the coder (bounded by `MAX_ITERATIONS`).
- **Finisher** writes a lesson to `AGENTS.md` (so the harness learns across
  runs) and optionally creates a `wells/<slug>` branch + commit + PR.

Everything goes through a **token-optimization layer** (estimator + calibration,
per-category context trimming, log compression, rolling summaries, model router,
cache-friendly prompts) and a **workspace confinement + safety policy** layer.

## Provider profiles (model-agnostic)

Models are configured as named **profiles**. Any number can coexist; one is
*active*, one optionally *cheap* (used for summarization/compression).

| Profile name | Provider kind | Notes |
|---|---|---|
| `zai` (default) | `openai` (OpenAI-compatible) | Z.ai GLM via the **coding endpoint** `/api/coding/paas/v4/`. Backward-compatible with legacy `ZAI_*` vars. |
| `openai` | `openai` | OpenAI directly |
| `openrouter` | `openai` | OpenRouter (hundreds of models) |
| `anthropic` | `anthropic` | Requires `pip install langchain-anthropic` |
| `ollama` | `ollama` | Local models; requires `pip install langchain-ollama` |
| `local` | `openai` | Any local vLLM / Ollama OpenAI shim |
| `together` / `groq` / `fireworks` / `deepseek` / `mistral` | `openai` | One-line setup |
| `google` / `bedrock` / `azure` | provider-specific | Optional provider packages |

A profile is configured with three env vars:

```bash
MODEL_<name>=<model-id>            # required
API_KEY_<name>=<key>               # if the provider needs one
BASE_URL_<name>=<url>              # for OpenAI-compatible endpoints
```

Select which profiles exist and which is active:

```bash
MODEL_PROFILES=zai,openrouter,local
MODEL_PROFILE=openrouter           # the active profile
MODEL_PROFILE_CHEAP=zai            # optional: cheaper model for subtasks
```

Optional provider packages are imported lazily — the harness runs out-of-the-box
with only `langchain-openai` (the OpenAI-compatible path covers Z.ai, OpenAI,
OpenRouter, Together, Groq, Fireworks, local vLLM, Ollama's OpenAI shim, …).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

```bash
uv sync                          # install dependencies
cp .env.example .env             # then edit .env
coding-harness config            # interactive settings menu (recommended)
```

Or skip the menu and edit `.env` directly. Then run a task:

```bash
coding-harness "Add a JWT auth middleware to the Express app"
```

## CLI

```
coding-harness "<goal>"                    # run the full harness
coding-harness --plan "<goal>"             # plan mode: plan edits, don't apply
coding-harness config                      # interactive settings menu
coding-harness info                        # show effective configuration
coding-harness "<goal>" MAX_ITERATIONS=5   # inline setting override
```

Running `coding-harness` with no arguments opens the settings menu.

### Interactive settings menu

`coding-harness config` shows every setting grouped (Providers, Run, Tokens,
LLM) and lets you change any value by number/name, switch or add provider
profiles, and persist to `.env` — all in one place. Changes apply live and are
written back comment-preserving.

```
================================================================
 Wells harness — current settings
================================================================
[Providers]
  MODEL_PROFILE                openrouter
  ...
> p) Switch / edit provider profile (fast path)
> +) Add a new provider profile
> MAX_ITERATIONS        edit by env-var name
> s) Save & exit     q) Quit without saving     w) Write .env now
```

## Safety model

The agent operates inside a **workspace root** (path escapes blocked) and a
**safety policy** for writes and shell commands:

| `HARNESS_SAFETY` | Behaviour |
|---|---|
| `auto` (default) | Execute immediately, confined to `WORKSPACE_ROOT`. Destructive commands (`rm -rf /`, `mkfs`, …) are always blocked. |
| `approve` | Require an approval callback; degrades to dry-run when no callback is wired. |
| `dryrun` | Never execute — describe what *would* happen. Truly side-effect free. |

`PLAN_MODE=1` forces all mutating tools to simulate (reads still work), so you
can preview exactly what the agent would change.

## Project structure

```
src/coding_harness/
├── main.py            # CLI entry: run / config / info
├── settings.py        # interactive settings menu + .env persistence
├── config.py          # env vars, budgets, workspace/safety knobs
├── providers.py       # named provider profiles → chat-model factory (Layer 0)
├── state.py           # TypedDict LangGraph state
├── graph.py           # LangGraph workflow (planner→…→reviewer→finisher)
├── runtime.py         # run_step(): LLM call + usage capture (reasoning nodes)
├── executor.py        # agentic tool-calling loop (Layer 2) — native + text fallback
├── tools.py           # repo tools: read/list/glob/grep/write/edit/shell/subagent
├── safety.py          # workspace confinement + auto/approve/dryrun gate
├── subagents.py       # parallel research/fix subagents (Layer 3)
├── memory.py          # AGENTS.md project memory (Layer 3)
├── gitops.py          # branch/commit/diff/PR via git + gh (Layer 3)
├── finisher.py        # post-run memory write-back + git/PR node
├── tokens.py          # token estimation, ledger, usage report
├── context.py         # categorized, budget-trimmed prompt assembly
├── compress.py        # log/output compressor
├── summarize.py       # rolling task-state summarizer
├── mcp_server.py      # MCP server exposing all capabilities
└── agents/            # planner / architect / coder / tester / reviewer
```

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `MODEL_PROFILES` | `zai` | Comma-separated list of configured profile names |
| `MODEL_PROFILE` | `zai` | Active profile for main reasoning/coding |
| `MODEL_PROFILE_CHEAP` | _(blank)_ | Profile for low-stakes subtasks (defaults to active) |
| `MODEL_<name>` | — | Model id for profile `<name>` |
| `API_KEY_<name>` | — | API key for profile `<name>` |
| `BASE_URL_<name>` | — | OpenAI-compatible base URL for profile `<name>` |
| `WORKSPACE_ROOT` | `cwd` | Directory the agent is confined to |
| `HARNESS_SAFETY` | `auto` | `auto` / `approve` / `dryrun` |
| `PLAN_MODE` | `0` | When on, coder plans but does not apply edits |
| `MAX_ITERATIONS` | `3` | Max coder↔reviewer loops |
| `MAX_TOOL_STEPS` | `25` | Max tool-call rounds per executor run |
| `SHELL_TIMEOUT` | `120` | Max seconds for a single shell command |
| `TOKEN_BUDGET_MAX_INPUT` | `24000` | Input budget per call (above this, trims) |
| `SUMMARIZE_ON_LOOP` | `1` | Replace durable context with a summary on loop iterations |
| `SUMMARIZE_THRESHOLD` | `1500` | Tokens above which durable context is summarized |
| `LLM_TIMEOUT` | `180` | Per-call timeout |
| `LLM_MAX_RETRIES` | `5` | Retry attempts for transient errors |
| `WELLS_OPEN_PR` | `0` | When `1`, the finisher pushes + opens a PR via `gh` |
| `BLOCKED_COMMANDS` | _(see source)_ | `\|`-separated regex patterns always refused |

### Legacy `ZAI_*` variables

Existing `.env` files using `ZAI_API_KEY` / `ZAI_MODEL` / `ZAI_ENDPOINT` keep
working unchanged — they seed the built-in `zai` profile. Explicit
`MODEL_zai` / `BASE_URL_zai` / `API_KEY_zai` vars take precedence.

## MCP server

The harness exposes its capabilities as a [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio, so external agent clients (Claude Code, OpenCode, Codex CLIs,
Gemini CLI, …) can invoke the harness.

```bash
coding-harness-mcp          # console script
python -m coding_harness    # same thing
```

### Exposed tools (13)

| Tool | Description |
|---|---|
| `run_agent_task` | Full harness loop (planner→…→reviewer→finisher) with workspace + safety overrides |
| `plan_task` | Planner + architect only (fast) |
| `review_code` | Reviewer only on provided context |
| `run_executor` | Single autonomous executor loop for an arbitrary task |
| `spawn_subagent` | Focused research (read-only) or fix subagent |
| `search_repo` | Glob + grep search (read-only) |
| `read_file` | Read a workspace file (read-only) |
| `run_command` | Run a shell command (confined, blocklisted, gated) |
| `git_status` | Git status + diff stat (read-only) |
| `get_memory` | Read project memory (`AGENTS.md`) |
| `compress_logs` | Compress log output (ANSI/dup/tail) |
| `get_harness_info` | Effective configuration |

### Client configuration

```json
{
  "mcpServers": {
    "coding-harness": { "command": "coding-harness-mcp", "args": [] }
  }
}
```

## Token optimization

| Component | What it does |
|---|---|
| **Estimator** | tiktoken-based, auto-calibrated against actual API responses |
| **TokenLedger** | Per-step actuals (input/output/reasoning/cache_read) from `usage_metadata` |
| **Token Usage Report** | End-of-run report with per-step table + category breakdown + savings |
| **ContextManager** | Categorized chunks, stable-prefix ordering, priority budget trimming |
| **Compressor** | ANSI strip, duplicate/blank collapse, tail-window, traceback preserve |
| **Summarizer** | Rolling task-state summary on loop iterations (threshold-guarded) |
| **Model Router** | Cheaper model for summarization/compression via `MODEL_PROFILE_CHEAP` |
| **Prompt-Cache Prefix** | `SystemMessage` + deterministic chunk order (cache-friendly) |

## Tests

```bash
uv run python -m pytest tests/ -v
```

The suite covers provider-profile resolution, the coding-endpoint precedence,
tool confinement + every safety mode, the agentic executor loop (with a mock
model so it runs without API credits), MCP tool registration, and the
settings-menu `.env` persistence.

## Roadmap

- Async task tracking for MCP `run_agent_task` (return a task ID, poll later).
- Per-call ledger isolation for concurrent MCP requests.
- Embedding-based code retrieval (replace full-repo injection for large repos).
- Streaming executor output for interactive UIs.
