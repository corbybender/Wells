<p align="center">
  <img src="wells_logo.png" alt="Wells — the tripod coding robot" width="260">
</p>

<h1 align="center">Wells</h1>

<p align="center">
  <a href="https://github.com/corbybender/Wells/actions/workflows/ci.yml"><img src="https://github.com/corbybender/Wells/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/wells-index/"><img src="https://img.shields.io/pypi/v/wells-index?label=wells-index" alt="wells-index on PyPI"></a>
</p>

**Bring your own API key — Anthropic (Claude), OpenAI, Google (Gemini),
Z.ai (GLM), OpenRouter, any OpenAI-compatible endpoint, or run entirely
local (Ollama, vLLM). Your choice, switchable per task.**

Wells is a **model-agnostic agentic coding platform** you run yourself: a
full-screen terminal TUI plus an orchestration engine of autonomous
tool-using agents (`planner → architect → coder → tester → reviewer →
finisher`) that actually read files, make edits, run tests, and verify their
own work — Claude Code / Codex CLI / OpenCode style, pointed at whichever
provider you already have an API key for. Local models are a first-class
option, not the point — they're just another provider profile, and nothing
in Wells requires them. Ships with a Rust structural repo index
(`wells-index`), an MCP server *and* MCP client, git-checkpointed undo, a
deterministic verification layer, **agent skills** (load-on-demand
know-how), **CodeAct** (sandboxed code execution), **background agents**
(concurrent fan-out), and a **browser tool** (Playwright — navigate, click,
type, read, screenshot a real JS-rendered session).

## Get started in one command

```bash
uv tool install git+https://github.com/corbybender/Wells.git && wells config
```

That installs `wells` on your PATH from any directory (no clone needed) and
opens the first-run provider setup — pick Claude, OpenAI, Gemini, Z.ai,
OpenRouter, or a local model. No `uv`? `pipx install
git+https://github.com/corbybender/Wells.git` works the same way.

Prefer cloning the repo, running against a specific project without a
global install, or want the full walkthrough of every option? →
[Quick start](#quick-start).

## Contents

- [How Wells compares](#how-wells-compares) — [vs Aider](#vs-aider) · [vs OpenHands](#vs-openhands) · [vs OpenCode](#vs-opencode) · [vs Claude Code](#vs-claude-code) · [vs Cursor](#vs-cursor)
- [What it does](#what-it-does) — the planner→coder→tester→reviewer graph
- [The TUI](#the-tui) — commands, keyboard shortcuts, the info panel
- [Provider profiles](#provider-profiles-model-agnostic) — model-agnostic setup
- [Quick start](#quick-start) — cloning, embedding, global install, manual setup
- [CLI](#cli)
- [Scheduled runs](#scheduled-runs) — unattended recurring runs (Task Scheduler / cron)
- [Notifications & daily spend cap](#notifications--daily-spend-cap) — desktop/webhook alerts, cross-run budget
- [Safety model](#safety-model) — auto / approve / dryrun / plan / sandbox
- [Operating rules](#operating-rules--deterministic-not-hopeful) — deterministic enforcement + permission allowlists
- [Repository index](#repository-index-wells-index) (`wells-index`)
- [Behavioral principles](#behavioral-principles-agentmd) (`AGENT.md`)
- [Global user memory](#global-user-memory) — standing preferences across every project
- [Agent capabilities](#agent-capabilities)
  - [Skills](#skills--load-on-demand-know-how) · [CodeAct](#codeact--let-it-compute) · [Browser](#browser--drive-a-real-js-rendered-session)
  - [Background agents](#background-agents--concurrent-fan-out) · [Subagent personas](#subagent-personas--custom-specialist-identities) · [Model-driven todo list](#model-driven-todo-list)
- [MCP — server and client](#mcp--server-and-client)
- [Project structure](#project-structure)
- [Configuration reference](#configuration-reference)
- [Token & cost optimization](#token--cost-optimization)
- [Tests & CI](#tests--ci)
- [Semantic code retrieval](#semantic-code-retrieval)

## How Wells compares

Wells sits in the same space as **Aider**, **OpenHands** (formerly
OpenDevin), **OpenCode**, and **Claude Code** — an autonomous coding agent
you run yourself, not an editor-embedded copilot. **Continue.dev** is a
different category (an IDE extension) rather than a competing harness, so
it isn't compared here. **Cursor** is the same category mismatch (a full
IDE, not a CLI/TUI) — but its Agent/Composer mode is a real, comparable
agent loop, so it gets a table below with that caveat up front rather than
being excluded outright.

### vs Aider

| | Aider | Wells |
|---|---|---|
| Loop | Single-pass edit → diff → commit | Full graph: `indexer → planner → [architect] → coder → tester → reviewer ↔ summarizer → finisher` — plan, execute, test, and review are separate, checkpointed stages |
| Repo grounding | Static ctags repo-map | Planner *agentically* investigates (`find_symbol`/`grep`/`read_file`, plus parallel research fan-out) before writing a plan with exact files and line numbers |
| Test verification | Model interprets test output | **Deterministic test-gate**: the real suite's exit code is ground truth — a green suite skips the LLM interpretation pass entirely |
| Broken edits | Caught on the next round-trip | Self-heal runs ruff/`node --check`/JSON-parse after every write and feeds failures back the *same* round |
| Weak/local models | Assumes a capable model | Built for them: structured-output JSON-grammar fallback, Ollama context-window auto-detect + warmup, compact-prompt mode |
| Policy enforcement | None built in | Tiered rules engine (`.wells/rules.yaml`) + liabilities tracking + `hooks.yaml` — deterministic, not just prompted |
| Recovery | Re-run from scratch | Per-node session checkpoints (`/resume`), pre-run git snapshot (`/undo`), full run trace + replay |
| Parallel edits | Sequential | Background git-worktree fan-out — multiple sub-agents edit concurrently in isolated checkouts, cherry-picked back on collect |

### vs OpenHands

| | OpenHands | Wells |
|---|---|---|
| Footprint | Docker sandbox + web UI | CLI/TUI — one Python env, no container required |
| Operational visibility | Browser-based event stream | Textual TUI dashboard in one pane: live token/cost ledger, pipeline-stage breadcrumb with the active step highlighted, pinned-file context, MCP server manager, index stats, git branch, open liabilities |
| Providers | Typically one model per session | Named provider profiles (`zai`/`openai`/`anthropic`/`gemini`/`ollama`/`openrouter`/`local`/...), with automatic cheap-task *and* vision-task routing per call |
| Interop | Its own agent, standalone | MCP **server** (drive Wells from Claude Code/OpenCode/other CLIs) *and* MCP **client** (give Wells external tool servers) |
| Domain know-how | Baked into the base prompt | **Skills**: progressive-disclosure `SKILL.md` files — name + one-line description always visible, full body loads only when relevant |
| Deterministic computation | Reasons it out | **CodeAct**: sandboxed `run_code` tool for exact arithmetic/regex/counts instead of eyeballed answers |
| Subagent identities | One generic agent persona | Custom `PERSONA.md` specialists (system prompt + toolset), invoked per-call via `bg_start(persona=...)` — a security reviewer and a performance investigator get different framing, not the same generic prompt |
| Unattended operation | Needs a live session | `wells schedule` registers a goal with the OS's own scheduler (Task Scheduler/cron) — no Wells process has to be running for it to fire |
| Cross-project memory | Session-scoped | `~/.wells/memory/` — standing preferences that follow you to every project, distinct from per-repo `AGENTS.md` |

### vs OpenCode

The closest peer of the four — also a provider-agnostic TUI harness with
subagents, custom commands, MCP, and a permission system. Credit where
due: several Wells features exist because OpenCode (and Claude Code, next)
already proved the pattern — this is a comparison between two harnesses
built on overlapping ideas, not a one-sided list.

| | OpenCode | Wells |
|---|---|---|
| Test verification | Model-driven (runs tests via its own tool calls, no ground-truth gate) | **Deterministic test-gate**: the real suite's exit code is ground truth — a green suite skips the LLM interpretation pass entirely, and post-edit self-heal (fast: ruff/`node --check`/JSON-parse; optional semantic: pyright/`tsc`/`cargo check`/`go vet`) catches broken syntax *and* type errors the same round |
| Sandboxing | Permission gating (allow/deny/ask); no container/VM isolation found | `sandbox` mode: `run_command` and `run_code` execute in a disposable Podman/Docker container, picked explicitly per run |
| MCP | Client only (connects out to MCP servers) | **Server** (drive Wells from Claude Code/OpenCode/other CLIs) *and* **client** |
| Unattended operation | Not found | `wells schedule` — Task Scheduler/cron, no live process required, with completion notifications (desktop/webhook) and a cross-run daily spend cap |
| Code intelligence | Live LSP servers per language (accurate, but a process to start/maintain per language) | Precomputed Rust structural index (`wells-index`) — `find_symbol`/`find_references`/`find_callers` in one lookup, ~98% fewer tokens than a live search; a different tradeoff (speed + low overhead vs. a language server's live accuracy), not a strict upgrade |
| Parallel work | Subagents (blocking) | Both blocking (`parallel_research`) *and* non-blocking (`bg_start`/`bg_status`/`bg_collect`) fan-out, plus `fleet` — N full parallel worktree attempts at the same task, pick the winner |

### vs Claude Code

The actual named target for this project, and the one Wells borrows the
most acknowledged patterns from: subagent personas, custom slash commands,
hooks, a model-driven todo list, and cross-project memory all exist in
Wells *because* Claude Code proved they work well. Where Wells tries to go
further:

| | Claude Code | Wells |
|---|---|---|
| Provider | Anthropic's Claude only | Any of them — Claude, OpenAI, Gemini, Z.ai, OpenRouter, any OpenAI-compatible endpoint, or fully local (Ollama/vLLM), switchable per task, not a one-time choice |
| Test verification | Model decides whether/when to run tests via its own Bash calls | **Deterministic test-gate** + automatic post-edit self-heal — enforced by the harness, not left to model judgment |
| Sandboxing | Bash tool runs on the host directly (as far as the CLI itself exposes) | `sandbox` mode: disposable Podman/Docker container per run, opt-in |
| Unattended operation | Cloud-hosted scheduled routines (Anthropic's infrastructure, needs an account/plan tier) | `wells schedule` — your own machine's Task Scheduler/cron, no cloud dependency; completion notifications + a cross-run daily spend cap guard against a stuck/runaway recurring goal |
| Parallel fan-out | Task-tool subagent delegation (blocking) | Blocking *and* non-blocking (`bg_start`), plus `fleet` — N parallel worktree attempts, pick the winner, merge or discard |
| Code intelligence | Grep/Glob live search each time | Precomputed structural index (`wells-index`) — exact file:line answers, ~98% fewer tokens per lookup |
| Cost visibility | Point-in-time (`/cost`) | Live, continuously-updating token/dollar ledger in the status panel throughout the run |
| Permission allowlists | ✓ (`settings.json` allow/ask/deny) | ✓ (`.wells/rules.yaml` `severity: allow`) — built to reach parity with this, not a Wells-only feature |
| Custom subagents / slash commands / hooks / todo list / auto-memory | ✓ (the originals) | ✓ (deliberately mirrored — `PERSONA.md`, `.wells/commands/`, `hooks.yaml`, `update_todos`, `~/.wells/memory/`) |

### vs Cursor

Cursor is a full IDE (a VS Code fork), not a CLI/TUI — genuinely a
different product category, the same mismatch as Continue.dev. It gets a
table anyway because its Agent/Composer mode is a real, comparable agent
loop (runs terminal commands, edits multiple files, has its own rules and
MCP support) — just wrapped in a GUI editor instead of a terminal.

| | Cursor (Agent mode) | Wells |
|---|---|---|
| Interface | Full Electron-based IDE — a GUI is required | Terminal TUI or headless CLI (`wells -p --output-format json`) — scriptable, runs over SSH, no GUI needed |
| Providers (BYOK) | OpenAI, Anthropic, Google, Azure, AWS Bedrock | All of those, plus Z.ai, OpenRouter, any OpenAI-compatible endpoint, and fully local (Ollama/vLLM) |
| Background/parallel agents | Cloud Agents — isolated VMs in Cursor's own cloud infrastructure | `bg_start`/`fleet` — your own local git worktrees, on your own disk, nothing leaves your machine unless you choose a hosted provider for the model call itself |
| Persistent instructions | `.cursor/rules` — static context injected at the start of every prompt | Both static (`AGENT.md`/`AGENTS.md`/`RULES.md`, prompt-injected) *and* a stateful liability ledger + tool-boundary enforcement that runs *before* a call, not just prompted |
| Test verification | Not found as a distinct ground-truth gate | **Deterministic test-gate** + automatic post-edit self-heal |
| MCP | Client only (confirmed); no evidence of an MCP server mode | **Server** *and* **client** |

## What it does

```
START → indexer → planner ──(simple plan)──────────┐
                     │ (complex)                    ▼
                  architect ─────────────────────► coder → tester ──(tests FAIL)──┐
                                                     ▲         │ (pass/unknown)   │
                                                     │         ▼                  │
                                                summarizer ◄─ reviewer ◄──────────┘
                                                     ▲         │(INCOMPLETE)
                                                     └─────────┘
                                                               │(COMPLETE / cap)
                                                    finisher (memory + git/PR) → END
```

- **Indexer** builds/refreshes the structural repo index (symbols, references,
  call graph) before anything else runs.
- **Planner** is agentic: it investigates the codebase with read-only tools
  (index-first lookups, plus a `parallel_research` fan-out that runs 2–4
  read-only subagents concurrently), then writes a concrete plan with exact
  files and line numbers — and labels it `SIMPLE` or `COMPLEX`.
- **Architect** validates complex plans; simple plans skip straight to the
  coder (one less LLM call).
- **Coder** drives the agentic executor: reads, edits, creates files, and runs
  verification inside your workspace. Edits are whitespace-tolerant
  (an indentation slip in the model's match string no longer wastes a
  round-trip) and every applied change shows a colorized diff live.
  After each write, the harness itself runs the fastest checker for the file
  type (ruff/py_compile, `node --check`, JSON parse) and injects failures
  into the model's next observation — broken code is caught in milliseconds,
  not a tester round-trip later. Opt in to a slower, project-aware **semantic**
  pass (`WELLS_SEMANTIC_CHECK=1`) that runs a real type-checker — pyright/mypy,
  `tsc`, `cargo check`, `go vet` — for the file's language and catches type
  errors and bad cross-file references the fast pass can't see.
- **Tester** runs a *deterministic gate first*: if the repo has a recognizable
  test setup, the harness executes the suite and records the exit code as
  ground truth. Green suite → the LLM interpretation pass is skipped entirely.
  Red suite → routes straight back to the coder (reviewer skipped) with the
  failure report as feedback.
- **Reviewer** independently verifies the work (reads changed files, re-runs
  tests) and emits `COMPLETE` / `INCOMPLETE`. Tester + reviewer route to the
  cheap model profile when one is configured (`CHEAP_VERIFY`).
- **Summarizer** condenses durable context on loop iterations (bounded by
  `MAX_ITERATIONS`).
- **Finisher** writes a lesson to `AGENTS.md` project memory and optionally
  creates a `wells/<slug>` branch + commit + PR.

The session is **checkpointed after every node**, so a crash loses at most one
node's work and `/resume` continues from the last state. Every run also
snapshots your working tree first — `/undo` reverts everything a run changed.

## The TUI

Running `wells` with no arguments opens the full-screen TUI: scrollable output
log, multi-line prompt (Shift+Enter for newlines, ↑/↓ history, persisted
across sessions), and an always-on status bar showing workspace, model, live
token count **and dollar cost**, operating mode, pinned-file count, and — while
running — the current agent activity (`coder-1 · step 12/60`, current tool).
**Escape cancels a running task** cooperatively at the next step boundary.
Answers stream token-by-token. **F2** hides the panel (full-width select/copy);
**F4** stages a clipboard screenshot as an image attachment (see below —
Ctrl+V can't do this: terminals intercept it as their own paste keybinding
and swallow it when the clipboard holds an image instead of text).

The right-hand panel always shows one of two states, never blank: a plain
`○ chat` line when idle, or — the moment anything routes through the
planning graph — the full `indexer → planner → architect → coder → tester →
reviewer → summarizer → finisher` step list, with the in-flight step
highlighted (`▶` yellow + elapsed time), completed steps checked off
(`✓` green), and failures marked (`✗` red). A third, independent section
renders the model's own **live task breakdown** (the `update_todos` tool —
see [Agent capabilities](#agent-capabilities)) whenever it declares one: each
item shown pending (dim), in-progress (`▶` yellow, at most one at a time),
or completed (struck through) — complementary to the pipeline breadcrumb,
which shows *structural* graph position, not what the model actually
decided to do inside a step.

| Command | What it does |
|---|---|
| `/mode plan\|approve\|auto\|dryrun\|sandbox` | Switch operating mode (read-only / confirm each change / full autonomy / simulate / containerized shell) — or pick it visually via the `/config` choice picker |
| `/add <path>` / `/drop <path>` / `/context` | Pin files into every prompt (guaranteed context, token-trimmed) |
| `/image <path>` / `clear` | Attach an image file to the next task (screenshot of a bug, a design mock, a diagram) |
| `/paste-image` (or **F4**) | Grab an image from the system clipboard and attach it to the next task |
| `/undo` | Revert everything the last run changed (automatic pre-run git checkpoint) |
| `/config` | Modal settings panel — all settings grouped, edit in place, saves to `.env`. Choice-constrained settings (`HARNESS_SAFETY`, `PLAN_MODE`, ...) open an arrow+Enter picker instead of typing — no way to mistype a value |
| `/mcp` | Modal MCP server manager — add / enable / disable / test / remove servers |
| `/rules` | Operating rules + open liabilities (`list` / `reload` / `discharge <id>` / `add` / `remove <id>`) |
| `/skills` | Modal skills manager — list / view / add / edit / remove `SKILL.md` know-how |
| `/agents` | Modal subagent-persona manager — list / view / add / edit / remove `PERSONA.md` custom specialist identities |
| `/memory` | Modal global-memory manager — list / view / add / edit / remove standing preferences (`~/.wells/memory/`, every project) |
| `/schedule` | Register/list/remove unattended recurring runs (Task Scheduler / cron) |
| `/orchestrate` | Route the next message through the full planning graph |
| `/resume` / `/sessions` | Continue a previous session / browse history |
| `/index` | Build or refresh the structural repo index |
| `/doctor` | Diagnose the environment (model ping + latency, API key, TLS, index health, git, checkers) |
| `/export [path]` | Save the session transcript to a file |
| `/status` `/info` `/help` `/clear` `/quit` | Status panel, effective config, command list, clear history, exit |

Under `approve` mode, destructive tool calls (writes, shell commands, MCP
calls) pause the run and ask y/N in the TUI. `AUTO_COMMIT=1` (opt-in) commits
each successful run with an LLM-generated Conventional Commits message and a
Wells authorship trailer.

Images staged with `/image`, `/paste-image`, or F4 are sent to whatever
model is active — but if it isn't vision-capable (most coding-tuned models
aren't), the harness automatically routes just that one call to
`MODEL_PROFILE_VISION` instead (see
[Provider profiles](#provider-profiles-model-agnostic)) and reverts to
normal routing on the very next call. No manual switching back and forth.
The `browser_screenshot` tool (see [Browser](#browser--drive-a-real-js-rendered-session))
uses the same `MODEL_PROFILE_VISION` routing independently, to describe a
page back to the agent in text.

## Provider profiles (model-agnostic)

Models are configured as named **profiles**. Any number can coexist; one is
*active*, one optionally *cheap* (used for summarization/classification and,
with `CHEAP_VERIFY`, the tester/reviewer).

| Profile name | Provider kind | Notes |
|---|---|---|
| `zai` (default) | `openai` (OpenAI-compatible) | Z.ai GLM via the **coding endpoint** `/api/coding/paas/v4/`. Backward-compatible with legacy `ZAI_*` vars. |
| `openai` | `openai` | OpenAI directly |
| `openrouter` | `openai` | OpenRouter (hundreds of models, incl. several free-tier `:free` vision models — good fit for `MODEL_PROFILE_VISION`); auto-detects `OPENROUTER_API_KEY` |
| `anthropic` | `anthropic` | Requires `pip install langchain-anthropic` |
| `ollama` | `ollama` | Local models; requires `pip install langchain-ollama` |
| `local` | `openai` | Any local vLLM / Ollama OpenAI shim |
| `together` / `groq` / `fireworks` / `deepseek` / `mistral` | `openai` | One-line setup |
| `google` / `gemini` | `google` | Google Gemini. Requires `pip install langchain-google-genai` |
| `bedrock` / `azure` | provider-specific | Optional provider packages |

A profile is configured with three env vars:

```bash
MODEL_<name>=<model-id>            # required
API_KEY_<name>=<key>               # if the provider needs one
BASE_URL_<name>=<url>              # for OpenAI-compatible endpoints
```

`API_KEY_<name>` also falls back to that provider's own standard env var
(`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`GROQ_API_KEY`, `TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `MISTRAL_API_KEY`,
`DEEPSEEK_API_KEY`) for a profile named after that provider — so a key you
already have set from another tool works immediately, no duplicate
`API_KEY_<name>` needed.

Select which profiles exist and which is active:

```bash
MODEL_PROFILES=zai,openrouter,local
MODEL_PROFILE=openrouter           # the active profile
MODEL_PROFILE_CHEAP=zai            # optional: cheaper model for subtasks
MODEL_PROFILE_VISION=openrouter    # optional: routed to for image-attached
                                    # tasks when the active profile isn't
                                    # vision-capable (e.g. a free OpenRouter
                                    # vision model alongside a non-vision main)
```

Manage every profile from `/config` (full CRUD, no manual `.env` editing
required):

- `p` — switch the active profile, or edit an existing one's model/key/URL
- `+` — add a brand-new profile from scratch
- `-` — remove a profile from `MODEL_PROFILES`; if it was the active/cheap/
  vision slot, that slot is cleared (cheap/vision) or reassigned (active)
  automatically
- `v` — dedicated vision-profile flow: point `MODEL_PROFILE_VISION` at an
  existing profile, create a new one on the spot (suggests a free
  OpenRouter vision model when the name is `openrouter`), or clear it back
  to "same as active"

Typing an env var name directly (e.g. `MODEL_PROFILE_CHEAP`) still works too.

Optional provider packages are imported lazily — the harness runs out-of-the-box
with only `langchain-openai` (the OpenAI-compatible path covers Z.ai, OpenAI,
OpenRouter, Together, Groq, Fireworks, local vLLM, Ollama's OpenAI shim, …).

Dollar costs are estimated from a built-in rate table (GLM / GPT / Claude /
DeepSeek / local); pin exact rates per profile with
`MODEL_PRICE_<profile>=<in>,<out>` ($/1M tokens).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

### Option A — Cloned standalone (no install needed)

After `git clone`, run the one-time setup script for your OS. It puts the
`wells` command on your PATH — no package build, no PyPI, works the same on
Mac, Linux, and Windows:

```bash
git clone https://github.com/corbybender/Wells.git
cd Wells

./install.sh             # Mac/Linux — or install.ps1 on Windows (PowerShell)
# open a new terminal, then:

wells config              # first run: set up your provider
wells info                 # show effective configuration
wells                      # open the TUI
wells "your goal"          # run the harness single-shot on THIS repo
```

`wells` itself still handles the venv/deps automatically on every run — the
installer only wires up the command name. You can also skip the installer
and call the launcher directly: `./wells` (Mac/Linux) or `wells.bat`
(Windows).

### Option B — Drive a DIFFERENT project (embedding)

Wells can operate on any project, not just itself. Clone it anywhere, then
point it at your project with `--workspace`:

```bash
# From your project root, with Wells cloned as a subfolder:
./Wells/wells --workspace . "add JWT auth to the Express app"

# Or an absolute path:
./Wells/wells --workspace /home/me/myapp "fix the failing tests"

# Preview only (plan mode — describe edits without applying):
./Wells/wells --workspace . --plan "refactor the data layer"
```

All file operations, shell commands, and tests run inside the `--workspace`
directory; Wells' own source is never touched.

### Option C — Global install (available everywhere)

Install once, directly from GitHub (not published to PyPI — only the
`wells-index` native indexer is; `pip install wells` won't find this
package), then `wells` is on your PATH from any directory:

```bash
uv tool install git+https://github.com/corbybender/Wells.git
# or: pipx install git+https://github.com/corbybender/Wells.git
# or, from a local clone: uv tool install . / pipx install .

wells "your goal"                        # from any directory
wells --workspace /path/to/project "goal"
```

**Note:** During installation, `uv` may show a warning about hardlinks across
filesystems (`WARN: Hardlink or symlink copy required…`). It's harmless;
suppress it with `UV_LINK_MODE=copy`.

### Manual setup (any option)

```bash
cp .env.example .env             # then edit .env with your API key
# or run the interactive menu:
./wells config
```

## CLI

Both `wells` and `wells` work identically (they're the same entry point).

```
wells                                     # launch the TUI
wells "<goal>"                            # run the full harness (single-shot)
wells --workspace /path "fix the bug"     # run against another project
wells --safety dryrun "goal"              # force dry-run (preview only)
wells --safety sandbox "goal"             # shell commands run in a disposable container
wells --plan "<goal>"                     # plan mode: plan edits, don't apply
wells config                              # interactive settings menu (terminal)
wells info                                # show effective configuration
wells principles                          # show active operating principles (AGENT.md)
wells --version                           # show version
wells "<goal>" MAX_ITERATIONS=5           # inline setting override
```

In the TUI, `/config` opens the modal settings panel instead (same schema,
same `.env` persistence).

## Scheduled runs

Register a goal to run **unattended, on a recurring interval**, via the
OS's native scheduler — Task Scheduler on Windows, cron on Linux/macOS.
Wells doesn't need to be running for a scheduled run to fire; the
scheduler invokes `wells` directly.

```bash
wells schedule add nightly-lint every1h "run the linter and fix any issues"
wells schedule list
wells schedule remove nightly-lint
```

Or from inside the TUI/REPL: `/schedule add` (opens a form), `/schedule
list`, `/schedule remove <name>`. Interval spec: `every<N>m` / `every<N>h`
(e.g. `every15m`, `every2h`), `daily@HH:MM`, or a raw 5-field cron
expression (Linux/macOS only — Windows Task Scheduler has no
cron-expression concept, so it's rejected there rather than
mistranslated). Schedules are tracked in `~/.wells/schedules.json` (so
`wells schedule list` works even if the OS-side entry was later removed by
hand) and mirrored into the real scheduler — verified live against Windows
Task Scheduler: registration, `schtasks /query` showing the correct
recurrence, removal, and confirmed-gone via a second query.

Each schedule gets a small wrapper script (`~/.wells/schedule-scripts/`)
instead of trying to embed the goal directly in a Task Scheduler/cron
command line — a goal can contain spaces, quotes, and newlines, and
getting nested command-line quoting right (especially on Windows) is
fragile. A PowerShell here-string / bash heredoc, written directly by
Python and never re-parsed through a shell, sidesteps that whole class of
bug.

## Notifications & daily spend cap

Two gaps a scheduled/unattended run opens up: no way to know it finished
without checking logs by hand, and no ceiling on how much a stuck or
runaway recurring goal can spend across a day.

**Run-completion notifications** (`WELLS_NOTIFY=1`) fire on every run —
headless or TUI — once it finishes:

* **Desktop popup** via each OS's own native mechanism: a balloon tip
  (`System.Windows.Forms.NotifyIcon`) on Windows, `osascript` on macOS,
  `notify-send` on Linux. No extra package on any platform.
* **Webhook** — `WELLS_NOTIFY_WEBHOOK_URL` gets a POST of
  `{"text": ..., "event": ..., "detail": ...}`. That's exactly the shape
  Slack's Incoming Webhooks expect, so pointing it at a Slack webhook URL
  works with zero Slack-specific code; any other JSON-accepting endpoint
  works too.

Both channels are best-effort — a notification failure never affects the
run's own reported outcome.

**Daily spend cap** (`WELLS_DAILY_BUDGET`, dollars) tracks cumulative
spend across every run in `~/.wells/spend.json` (global, resets at
midnight local time) and refuses to *start* a new run once the day's
budget is reached — headless exits `1` with a clear error, the TUI
prints the same message and doesn't launch. This is a different guarantee
from `MAX_RUN_TOKENS` (which caps *one* run's tokens): the daily cap stops
the *next* run in a recurring schedule from starting once the day's
money is spent. Unset or `0` (the default) means no cap.

```bash
export WELLS_NOTIFY=1
export WELLS_NOTIFY_WEBHOOK_URL=https://hooks.slack.com/services/...
export WELLS_DAILY_BUDGET=5.00
```

Or set any of the three from `/config` (category: Notifications).

## Safety model

The agent operates inside a **workspace root** (path escapes blocked) and a
**safety policy** for writes, shell commands, and MCP tool calls:

| Mode (`/mode` or `HARNESS_SAFETY`) | Behaviour |
|---|---|
| `auto` (default) | Execute immediately, confined to `WORKSPACE_ROOT`. Destructive commands (`rm -rf /`, `mkfs`, …) are always blocked. |
| `approve` | Every destructive action pauses the run and asks y/N in the TUI. |
| `dryrun` | Never execute — describe what *would* happen. Truly side-effect free. |
| `plan` (`PLAN_MODE=1`) | All mutating tools simulate; reads still work. Preview exactly what would change. |
| `sandbox` | Same full autonomy as `auto` — but shell commands (`run_command`) *and* CodeAct's `run_code` execute inside a disposable container instead of directly on your machine. Requires Podman (recommended, lightweight) or Docker; picked explicitly, per run. |

`sandbox` is opt-in and additive: every other mode behaves exactly as
before, so day-to-day use never launches a container runtime. Reach for it when you
don't fully trust a repo, want an unattended `auto` run walled off, or are
testing something you'd rather not risk on the host. File reads/writes/edits
are unaffected in every mode — the workspace is bind-mounted into the
container, so both sides see identical bytes; Wells' own workspace
confinement already keeps those inside `WORKSPACE_ROOT` regardless of mode.
One container is launched per workspace (lazily, on the first sandboxed
command) and reused for the rest of the session; `WELLS_SANDBOX_IMAGE`
overrides the default `python:3.12-slim` image. Pick the mode with the new
`/config` choice picker (arrow keys + Enter) or `/mode sandbox`.

Two extra safety nets regardless of mode: every run **snapshots the working
tree** (including untracked files) to a hidden git commit before starting —
`/undo` restores it — and `MAX_RUN_TOKENS` hard-caps a run's spend.

## Operating rules — deterministic, not hopeful

Prompted rules are probabilistic: every model eventually forgets a wall of
rules at prompt top. Wells enforces rules in tiers, strongest first:

1. **Tool-boundary enforcement** (`.wells/rules.yaml`, merged over
   `~/.wells/rules.yaml`): every tool call is checked *before* execution.
   `block` refuses outright, `confirm` pauses for y/N, `warn` injects the rule
   into the model's next observation, `allow` pre-clears `approve` mode's
   normal per-action y/N for a matching call (a **permission allowlist** —
   "always allow `git status`, always ask before `rm`" instead of a blanket
   prompt for every mutating call; `auto`/`dryrun`/`plan`/`sandbox` are
   unaffected either way), and `liability` registers a stateful obligation —
   e.g. *a rented GPU was started and must be terminated*. **A run cannot
   silently end with an open liability**: Wells attempts an automatic
   discharge pass, marks the run INCOMPLETE otherwise, shows a red
   `⚠ LIABILITY` badge in the status bar, warns on next startup, and keeps
   the ledger in `~/.wells/liabilities.json` so even a crash can't lose track
   of a running paid resource.
2. **Moment-of-relevance injection**: when a rule fires, its text lands in
   the exact tool observation the model reads next — one rule, at the moment
   it applies — plus open liabilities pinned into the never-pruned working
   memory.
3. **Prompt + audit**: the workspace `RULES.md` (universal, incident-derived
   rules) is injected into every system prompt, and the reviewer audits
   compliance — violations force the INCOMPLETE loop.

Manage with `/rules` (`list`, `reload` after editing, `discharge <id>` to
acknowledge a manually-closed resource, `add`/`remove <id>` for simple
pattern-triggered rules — liability rules, with their open/close regex
pairs, stay hand-edited). `/rules add` (or the TUI form) writes a new rule
by **appending** to `.wells/rules.yaml` — never a full parse+rewrite — so
an existing hand-edited file keeps every comment exactly as written;
`remove` does need a full rewrite and won't preserve comments. Default
rules ship globally on first run: GPU-rental teardown tracking,
force-push/hard-reset confirmation, bulk-rsync confirmation,
auth-preflight and monitor-quality warnings, and one `allow` rule
(`git status`/`diff`/`log`/`show`/`branch` never need confirmation, even
in `approve` mode). Kill-switch: `RULES_ENFORCE=0`; auto-discharge:
`RULES_AUTODISCHARGE`.

## Repository index (wells-index)

Wells ships a Rust structural indexer ([`wells-index`](wells-index/) —
[on PyPI](https://pypi.org/project/wells-index/)): tree-sitter parsing for 8
languages, SQLite + LZ4 storage, BLAKE3 incremental hashing. It powers:

- **Index-first tools** — `find_symbol`, `find_references`, `find_callers`,
  `search_symbols`, `list_symbols`: exact file:line answers instead of grep
  walls (~98% fewer tokens per lookup).
- **The repo map** — a compressed *files → key symbols* map injected into
  planner/coder prompts, **ranked by relevance to the current goal**, so the
  model starts knowing where things live instead of spending steps on
  discovery.
- A **background file watcher** keeps the index live during a session; the
  indexer node refreshes it before every orchestrate run. `/doctor` detects a
  stale native core and self-repairs from the repo-bundled binaries.

## Behavioral principles (AGENT.md)

Every agent in the harness — regardless of which model you've configured — is
governed by the same behavioral constitution: the **operating principles** in
`AGENT.md`. These 11 rules (Think Before Coding, Simplicity First, Surgical
Changes, Goal-Driven Execution, Deterministic First, Budget Everything, Verify
Before Trust, Fail Loud, Isolate Side Effects, Check Before Declaring Done,
Evidence Over Confidence) are **always injected** into every agent's system
prompt, so the harness behaves consistently whether you drive it with GLM, GPT,
Claude, Gemini, or a local model.

This is distinct from per-project `AGENTS.md` memory, and from global,
cross-project **user memory**:

| | `AGENT.md` (bundled) | `AGENTS.md` (per-project) | User memory (global) |
|---|---|---|---|
| **Purpose** | Behavioral rules — *how* the agent works | Project knowledge — *what* it knows about this repo | Standing facts about *you* — how you like to work |
| **Scope** | Every run, every agent, every project | One project; accumulates over runs | Every project you point Wells at |
| **Ship location** | Inside the harness package | The workspace root | `~/.wells/memory/` |
| **Who writes it** | The harness authors (you can override) | The harness finisher + you | You, via `/memory` |

### Override precedence (highest first)

1. **`WELLS_PRINCIPLES` env var** — point at any file path. Use this for
   organization-wide principles across all projects.
2. **`AGENT.md` in the workspace root** — lets a team customize the rules for
   one project. Version-controlled with that project.
3. **The bundled `AGENT.md`** — the default constitution shipped with the
   harness. Always present as a baseline.

Inspect the active principles with `wells principles` or the MCP
`get_principles` tool.

## Global user memory

`AGENTS.md` accumulates facts about *one repo*; this is the complementary,
orthogonal piece — durable preferences that follow you to every project,
the same category of thing Claude Code's own auto-memory keeps across
conversations. An entry is a small `<name>.md` file directly under
`~/.wells/memory/` (YAML front-matter + a short body):

```markdown
---
name: terse-commit-messages
type: feedback
description: Prefers short, why-focused commit messages over detailed ones.
---

Keep commit subject lines under 50 chars. Only add a body when the "why"
isn't obvious from the diff — never restate what changed.
```

Unlike skills/personas (below), entries are injected **directly and in
full** into every system prompt — even in compact mode, alongside the
`AGENT.md` principles — since these are meant to be short standing facts
the agent should always have, not on-demand procedures. A total-size
budget keeps an accumulated store from bloating every prompt, trimming the
oldest entries with a notice rather than silently dropping one.

Manage with `/memory` (list / show / add / edit / remove — CLI or the
modal TUI form). `type` is free-form categorization (conventional values:
`user`, `feedback`, `reference`) — it doesn't gate behavior, unlike a
persona's `tools:` field. `WELLS_USER_MEMORY=0` disables the feature
entirely.

## Agent capabilities

Three capability layers let Wells' agents *teach themselves*, *compute*, and
*work in parallel* — each is a self-contained, feature-gated module that
plugs into the same executor loop and safety model. Together they address a
core insight from Microsoft's Agent Framework write-up on
[scaling harness capabilities](https://devblogs.microsoft.com/agent-framework/agent-harness-scaling-the-claw-or-harness-capabilities/):
stuffing every instruction into the system prompt doesn't scale, some
questions need *computation* not *reasoning*, and blocking fan-out wastes the
parent agent's time.

### Skills — load-on-demand know-how

#### The problem

Stuffing every how-to into the system prompt bloats context and dilutes focus.
Wells already injects `AGENT.md` principles, `RULES.md`, a goal-ranked repo
map, and pinned context into every prompt — adding domain-specific how-to
(tutorials, runbooks, architecture deep-dives) to that always-on block would
starve the budget for the actual task.

#### The solution: progressive disclosure

**Skills** are small `SKILL.md` files that package a chunk of know-how. The
agent sees only each skill's **name and one-line description** up front
(injected into the system prompt as a compact index), and loads the **full
body** on demand via the `load_skill` tool — *only when a request matches that
skill*. Context stays small and focused; domain how-to scales without bloating
every call.

This is the natural complement to `AGENTS.md` memory:

| | `AGENTS.md` (project memory) | Skills |
|---|---|---|
| **Content** | *Accumulated facts* about a repo (what the harness learned) | *How-to procedures* authored by you |
| **Visibility** | Always-on (small, trimmed by the budget) | Name + description always visible; body loads on demand |
| **Who writes it** | The harness finisher + you | You (via `/skills` menu or by hand) |
| **Size** | Kept small (budget-trimmed) | Can be large (only loaded when relevant) |

#### SKILL.md format

Each skill lives in `skills/<name>/SKILL.md` with YAML front-matter + markdown
body:

```markdown
---
name: release-checklist
description: How to cut and publish a new release of this project.
---

1. Bump the version in `package.json` and `Cargo.toml`
2. Update `CHANGELOG.md` with the changes since the last tag
3. Run the full test suite: `uv run pytest -q`
4. Tag: `git tag -a v0.x.0 -m "Release v0.x.0"`
5. Push tags: `git push --tags`
6. The CD pipeline publishes to PyPI automatically
```

The front-matter fields:

| Field | Required | Purpose |
|---|---|---|
| `name` | Yes (defaults to folder name) | The identifier the agent uses with `load_skill` |
| `description` | Recommended | One line shown in the always-on index — should help the agent decide when to load it |

The body is free-form markdown — instructions, code blocks, links, diagrams.
It's capped at ~8 KB when loaded (truncated with a notice) so a single skill
can't blow the context budget.

#### Discovery

Skills are discovered from (first match wins, so earlier roots can shadow):

1. `<workspace>/skills/` — the conventional location
2. Any extra directory in `WELLS_SKILLS_PATHS` (path-separator list)

Two layouts are supported:

```
skills/
  release/SKILL.md        ← skill folder (recommended)
  add-provider/SKILL.md
  SKILL.md                ← a single skill at the skills/ root
```

Discovery is cached by directory mtime, so editing or adding a skill file
invalidates the cache automatically. The `skills.clear_cache()` call after
every mutation (create/update/delete) ensures the next read sees the change.

#### How the agent uses skills

1. Every system prompt includes a compact index:

   ```
   === AVAILABLE SKILLS (load with the load_skill tool) ===
   - release-checklist: How to cut and publish a new release of this project.
   - add-provider: How to add a new model provider profile.
   Call load_skill(name) to load a skill's full instructions when a request
   matches one. Do not load a skill unless it is relevant.
   === END SKILLS ===
   ```

2. When a request matches a skill (e.g. "cut a release"), the agent calls
   `load_skill("release-checklist")` and the full body lands in its context.
3. If no skill matches, nothing is loaded — zero overhead.

#### Managing skills with `/skills`

Manage skills from the TUI or CLI:

| Command | What it does |
|---|---|
| `/skills` | Open the modal manager — list, view, add, edit, remove (keyboard-driven) |
| `/skills list` | List all discovered skills (name + description) |
| `/skills show <name>` | Print the full `SKILL.md` file |
| `/skills add <name>` | Create a new skill (interactive; or use the modal form with Ctrl+S) |
| `/skills edit <name>` | Edit an existing skill's description and/or body |
| `/skills remove <name>` | Delete a skill (its folder + file) |

**TUI modal** — bare `/skills` opens a full-screen manager:

| Key | Action |
|---|---|
| `↑` `↓` | Select a skill |
| `Enter` | View the full `SKILL.md` |
| `a` | Add a new skill (form: name, description, markdown body editor) |
| `e` | Edit the selected skill |
| `d` | Remove the selected skill |
| `Esc` | Close |

In the add/edit form: `Ctrl+S` saves, `Esc` cancels.

All mutations go through the **safety gate** (plan/approve/dryrun apply),
**validate the skill name** (lowercase letters, digits, hyphens — blocks path
traversal), and only delete skills **under the workspace `skills/` tree**
(skills loaded from `WELLS_SKILLS_PATHS` can't be deleted from the menu).

**Configuration:**

| Variable | Default | Description |
|---|---|---|
| `WELLS_SKILLS` | `1` | Discover skills and expose `load_skill` (set `0` to disable) |
| `WELLS_SKILLS_PATHS` | _(blank)_ | Extra skill search dirs (OS path-separator list) |

### CodeAct — let it compute

#### The problem

Some questions are *calculations*, not lookups: "what's the total LOC across
the changed files", "does this regex match all 12 of these strings", "generate
the cartesian product of these test configurations", "count how many functions
transitively call `auth_check`". Doing arithmetic in the model's head or
eyeballing a regex is exactly what the harness's "Deterministic First" and
"Verify Before Trust" principles say not to do.

#### The solution: `run_code`

**CodeAct** gives the agent a `run_code` tool: it writes a small Python
snippet, the harness runs it in a **workspace-confined subprocess**, and
returns structured `stdout` / `stderr` / `exit code`. The agent gets a clean,
bounded result to reason over — no guessing.

**Example tool calls the agent makes:**

```python
# Count lines changed in the working tree
import subprocess
diff = subprocess.check_output(["git", "diff", "--stat"]).decode()
print(diff)

# Validate a regex against test strings
import re
pat = re.compile(r'^\d{4}-\d{2}-\d{2}$')
for s in ["2024-01-15", "99-1-1", "2024-13-40"]:
    print(f"{s}: {'match' if pat.match(s) else 'no match'}")
```

#### Confinement + guardrails

| Guardrail | What it does |
|---|---|
| **Workspace-confined** | `cwd` = workspace, so `open("src/utils.py")` works for repo inspection |
| **Source screening** | Refuses code containing `os.system`, `subprocess`, `popen`, `__import__`, or `fork()` — use `run_command` for shell work |
| **Deny-list screening** | The same `BLOCKED_COMMANDS` regex list that screens `run_command` is applied to the source text (catches `rm -rf /` in a string literal) |
| **Output truncation** | stdout capped at 8 KB, stderr at 4 KB — a runaway `print` in a loop can't blow the budget |
| **Timeout** | Hard wall-clock cap (default 30s via `CODEACT_TIMEOUT`; also bounded by `SHELL_TIMEOUT`) |
| **Safety gate** | Honours plan/dry-run/approve modes like every other mutating tool |
| **Sandbox mode** | Under `HARNESS_SAFETY=sandbox` / `/mode sandbox`, `run_code` executes inside the same disposable container as `run_command` (piped over stdin) instead of a host subprocess — see [Safety model](#safety-model) |

**Why a confined subprocess, not Hyperlight/Monty?** Zero extra dependencies —
works out of the box everywhere Python runs. Workspace confinement + the
existing safety gate + the deny-list give the same first line of defense the
article's `LocalShellExecutor` relies on, and `sandbox` mode (see
[Safety model](#safety-model)) adds real container isolation on top for
anyone who wants it, without imposing it by default.

**Configuration:**

| Variable | Default | Description |
|---|---|---|
| `WELLS_CODEACT` | `1` | Expose the `run_code` tool (set `0` to disable) |
| `CODEACT_TIMEOUT` | `30` | Max seconds for a single `run_code` execution |

### Browser — drive a real, JS-rendered session

#### The problem

`fetch_url` only ever sees a page's initial static HTML. Most real web
apps — single-page apps, dashboards, a local dev server's own frontend —
render their actual content with JavaScript, so `fetch_url` against them
returns an empty shell. Verifying a UI change, working through a logged-in
flow, or filling out a multi-step form needs a real browser, not a text fetch.

#### The solution: `browser_navigate` / `browser_click` / `browser_type` / `browser_read` / `browser_screenshot`

A genuine headless browser session (Playwright), lazily launched on first
use and kept alive for the rest of the process — cookies and login state
persist across calls within a session, the same way a human keeps one tab
open. Playwright drives whichever Chromium-based browser is already
installed (Chrome, Edge, Brave) rather than downloading its own copy,
falling back to Playwright's bundled Chromium only if none is found —
`WELLS_BROWSER_EXECUTABLE` pins an exact path if auto-detection picks the
wrong one.

| Tool | What it does |
|---|---|
| `browser_navigate` | Open a URL; returns the title + rendered visible text |
| `browser_click` | Click an element by CSS selector or visible text (falls back to a text search) |
| `browser_type` | Type into an input by selector or visible label/placeholder; optional `submit` |
| `browser_read` | Return the current page's rendered visible text |
| `browser_screenshot` | Screenshot the page and get back a text description of its layout and every interactive element |

`browser_screenshot` routes the PNG through the configured **vision
profile** (`MODEL_PROFILE_VISION` — see [Provider profiles](#provider-profiles-model-agnostic))
automatically, so screenshots are readable even when the active model isn't
vision-capable itself; if no vision profile resolves to something usable it
still saves the file and says so instead of failing the call.

`browser_navigate` / `browser_read` / `browser_screenshot` are read-only
(no safety gate, same as `fetch_url`); `browser_click` / `browser_type` can
have real side effects (submit a form, click "delete") and go through the
same plan/approve/dryrun gate as every other mutating tool.

**On by default**, same as the other agent capabilities — the tools are
always registered, so the agent knows they exist. Playwright the package
still needs a one-time separate install (not part of the base dependency
set):

```bash
pip install 'wells[browser]'      # or: uv sync --extra browser
```

The Chromium **download** step (`playwright install chromium`) usually
isn't needed at all — if Chrome, Edge, or Brave is already on the machine
(true for almost everyone), Wells drives that instead. Only run it if none
of those are installed. Calling a `browser_*` tool before Playwright itself
is installed returns a clear, actionable error instead of silently
failing — the same pattern the `anthropic`/`ollama`/`google` provider
profiles already use for their own optional packages. Turn the tools off
entirely with `WELLS_BROWSER=0` (or the `/config` picker) if you don't want
them offered at all.

**Configuration:**

| Variable | Default | Description |
|---|---|---|
| `WELLS_BROWSER` | `1` | Expose the `browser_*` tools (set `0` to disable; still needs the `browser` extra installed to actually run) |
| `WELLS_BROWSER_HEADLESS` | `1` | Run headless (set `0` to watch it drive a real window) |
| `WELLS_BROWSER_EXECUTABLE` | _(auto-detect)_ | Pin an exact browser executable path, skipping auto-detection |

### Background agents — concurrent fan-out

#### The problem

`parallel_research` already fans out 2–4 read-only research subagents in
parallel — but it **blocks**: the parent agent waits for all subagents to
finish before it can do anything else. If one subagent takes 30 seconds, the
parent is stuck for 30 seconds. The fan-out timing is also the *tool's*
decision, not the agent's.

#### The solution: start / check / collect

**Background agents** flip the blocking pattern to the async start / check /
collect model from the article. The agent gets three tools:

| Tool | What it does | Returns |
|---|---|---|
| `bg_start` | Launch a sub-agent on a background daemon thread | Handle id (e.g. `bg-1`) — immediately, does not block |
| `bg_status` | Poll all background agents | List with status (`running`/`done`/`error`/`cancelled`) + elapsed seconds |
| `bg_collect` | Collect a finished agent's report (once) | The subagent's full report, or "still running" if it isn't done |

The fan-out becomes the **agent's decision**: it starts N tasks, keeps working
(reading files, making edits, running tests), and collects results when
convenient — checking back periodically with `bg_status`.

**Example workflow the agent drives:**

```
bg_start(task="research the auth module's token validation flow")    → bg-1
bg_start(task="research the database migration history")             → bg-2
bg_start(task="find all callers of the deprecated API")              → bg-3

  # Agent keeps working while they run:
  read_file("src/main.py")
  edit_file("src/main.py", ...)
  run_tests()

bg_status                                                            → bg-1: done, bg-2: done, bg-3: running
bg_collect(id="bg-1")                                                → report from auth research
bg_collect(id="bg-2")                                                → report from migration research
  # bg-3 still running — collect later or move on
```

#### Roles — research, fix, worktree

| Role | Edits? | Where | Use when |
|---|---|---|---|
| `research` (default) | No | — | Read-only investigation; safe to fan out widely |
| `fix` | Yes | Parent workspace directly | One editor in flight, or edits target disjoint files |
| `worktree` | Yes | Its own isolated `git worktree`, cherry-picked into the parent on `bg_collect` | Multiple write-fan-outs target overlapping areas, or whenever isolation is cheaper than reasoning about interleaving |

The `worktree` role is what unblocks **parallel write steps**: two
`bg_start role=worktree` agents run genuinely concurrently against their own
checkouts (shared object store — fast, disk-cheap), and `bg_collect` merges
each one's commit back into the parent. On conflict the cherry-pick is
aborted and the diff is returned to the parent agent for manual re-apply — no
surprise merges, no semantic guesses. Requires git; non-git workspaces get an
error pointing at `role=fix`.

```
bg_start(task="refactor the auth middleware", role="worktree")        → bg-1
bg_start(task="refactor the session middleware", role="worktree")     → bg-2
bg_start(task="refactor the rate-limiter",   role="worktree")         → bg-3

  # All three edit concurrently in their own worktrees; the parent's
  # working tree is untouched until each is collected.

bg_status                                                            → all three: done
bg_collect(id="bg-1")                                                → merged into parent
bg_collect(id="bg-2")                                                → CONFLICT, diff returned
bg_collect(id="bg-3")                                                → merged into parent
```

#### Lifecycle + safety

| Property | Behaviour |
|---|---|
| **Concurrency** | Each sub-agent runs on a daemon thread (LLM calls are I/O-bound, matching `parallel_research`) |
| **Registry** | Process-wide, keyed by short stable ids (`bg-1`, `bg-2`, …); resets at the start of each executor run so slots don't leak |
| **Collect once** | A result is collected at most once, then cleared — keeps memory bounded across a long run |
| **Recursion blocked** | A sub-agent cannot start its own background agents (`ctx.subagent` is checked at dispatch) |
| **Cooperative cancellation** | Escape / `CONTROL.cancel()` marks running slots as cancelled; pending threads check at step boundaries |
| **Worktree reaping** | For `role="worktree"`, the worktree + branch are reaped on collect and on `reset()` — a cancelled run never leaks disk |
| **Roles** | `role=research` (read-only, default), `role=fix` (parent workspace), or `role=worktree` (isolated checkout, merged on collect) |
| **Safety gate** | Each sub-agent's tool calls pass through the same safety gate as the parent |

**Contrast with `parallel_research`:**

| | `parallel_research` | Background agents (`bg_*`) |
|---|---|---|
| **Blocking** | Yes — parent waits for all to finish | No — returns immediately, collect later |
| **Fan-out timing** | Tool's decision (2–4 fixed) | Agent's decision (any number) |
| **Parent works during?** | No | Yes |
| **Read-only?** | Yes | `research` = yes; `fix` / `worktree` = can edit |
| **Isolation** | N/A (read-only) | `fix` = parent workspace; `worktree` = own checkout |
| **Use case** | Quick parallel exploration | Long-running fan-out the parent checks back on |

**Configuration:**

| Variable | Default | Description |
|---|---|---|
| `WELLS_BG_AGENTS` | `1` | Expose `bg_start`/`bg_status`/`bg_collect` (set `0` to disable) |
| `WELLS_BG_WORKTREES` | `1` | Allow `bg_start role=worktree` (isolated git worktree per sub-agent; set `0` to refuse the role without disabling the bg tools) |

### Subagent personas — custom specialist identities

#### The problem

`bg_start`'s `research`/`fix`/`worktree` roles control *mutation mechanics*
(read-only vs. writes vs. isolated checkout) but say nothing about
*expertise* — every subagent gets the same generic prompt regardless of
what it's actually being asked to do. A security review and a performance
investigation deserve different framing, different things to look for,
different tone in the report back.

#### The solution: `PERSONA.md` + `bg_start(persona=...)`

A **persona** is a small `agents/<name>/PERSONA.md` file — discovered,
cached, and CRUD'd exactly like [skills](#skills--load-on-demand-know-how)
— that packages a subagent identity: a system prompt establishing its
expertise/voice/constraints, plus which toolset tier it gets:

```markdown
---
name: security-reviewer
description: Reviews a diff for injection/auth/secrets issues before merge.
tools: readonly
---

You are a senior application-security reviewer. For every file you're
shown, look specifically for: injection (SQL/command/template), broken
auth/session handling, hardcoded secrets, and unsafe deserialization.
Report findings as `file:line — issue — why it's exploitable`.
```

Even cheaper than skills: a persona's full system prompt never touches the
**parent's** context at all — only its name + one-line description show up
there (so the parent knows it exists), and the full prompt is handed to the
**subagent** run itself the moment `bg_start(persona=security-reviewer,
role=research, task="review this diff")` picks it. `role` still governs
mutation/isolation mechanics — `role=research` is always forced read-only
regardless of what a persona's `tools:` front-matter requests; the role is
the safety ceiling, the persona is the voice/expertise within it.

Manage with `/agents` (list / show / add / edit / remove — CLI or the
modal TUI form, identical interaction model to `/skills`). `WELLS_AGENTS=0`
disables discovery; `WELLS_AGENTS_PATHS` adds extra search directories
(path-separator list), same convention as `WELLS_SKILLS_PATHS`.

### Model-driven todo list

#### The problem

The pipeline breadcrumb (in [The TUI](#the-tui)) shows *structural* graph
position — which fixed node (planner/coder/tester/...) is running — but
nothing about what the coder actually decided to do *inside* a long,
multi-step task. That's the transparency Claude Code's own todo-list
rendering gives that a fixed pipeline view can't.

#### The solution: `update_todos`

A tool the model calls to declare or update its own task breakdown for the
current task — rendered live as a third, independent section of the info
panel. Resends the full list each call (no partial-update API to keep in
sync); at most one item may be `in_progress` at a time, so the panel always
has one unambiguous point of progress to highlight. Read-only (pure
in-memory display state, no workspace mutation), so it's available to
read-only investigations too, same as `web_search`/`fetch_url`. Cleared at
the start of every run — a todo list belongs to one task, not the whole
session. `WELLS_TODO=0` disables it.


## MCP — server *and* client

### Server: drive Wells from other agents

The harness exposes its capabilities as a
[Model Context Protocol](https://modelcontextprotocol.io) server over stdio,
so external agent clients (Claude Code, OpenCode, Codex CLIs, Gemini CLI, …)
can invoke the harness:

```bash
wells-mcp          # console script
```

Exposed tools include `run_agent_task` (full loop), `plan_task`,
`review_code`, `run_executor`, `spawn_subagent`, `search_repo`, `read_file`,
`run_command`, `git_status`, `get_memory`, `compress_logs`,
`get_harness_info`, and `get_principles`.

```json
{
  "mcpServers": {
    "wells": { "command": "wells-mcp", "args": [] }
  }
}
```

### Client: give Wells external tools

Wells also connects *out* to MCP servers (databases, docs, GitHub, memory
banks) and registers their tools for the agent as `mcp_<server>_<tool>`.
**Two transports are supported:**

| Transport | Spec shape | Use when |
|---|---|---|
| **stdio** | `{"command": "...", "args": [...]}` | Local subprocess — the classic MCP servers (`uvx mcp-server-fetch`, `npx @modelcontextprotocol/server-*`) |
| **HTTP** (streamable-http) | `{"url": "https://...", "headers": {...}}` | Remote MCP server speaking the newer spec (default when `url` is present) |
| **SSE** (legacy) | `{"url": "https://...", "transport": "sse"}` | Remote server that only speaks the older SSE protocol |

Configure via the **`/mcp` modal manager** in the TUI (add / enable /
disable / test / remove — no JSON editing), the `/mcp add …` subcommands
(auto-routes: a second arg starting with `http(s)://` becomes an HTTP
server; otherwise it's stdio), or by editing `~/.wells/mcp.json` directly
(created on first run with ready-to-enable samples: fetch, filesystem,
github, postgres, sqlite, memory, plus HTTP/SSE templates). The
`MCP_SERVERS` env var (JSON) overrides the file. Every external call
passes the safety gate, so `approve` and `dryrun` apply to MCP tools too.

## Project structure

```
src/wells/
├── main.py            # CLI entry: run / config / info / principles
├── cli.py             # REPL command layer: slash commands, run paths
├── tui.py             # Textual TUI: log, prompt, status bar, modals
├── control.py         # run control: cooperative cancel, activity, UI events
├── settings.py        # settings schema + .env persistence
├── config.py          # env vars, budgets, workspace/safety knobs
├── providers.py       # named provider profiles → chat-model factory
├── pricing.py         # dollar-cost estimation from the token ledger
├── state.py           # TypedDict LangGraph state
├── graph.py           # LangGraph workflow with conditional routing
├── runtime.py         # run_step(): LLM call + usage capture (reasoning nodes)
├── executor.py        # agentic tool loop: native+text tools, masking, streaming
├── tools.py           # repo tools: read/glob/grep/write/edit/shell/subagents
├── checkers.py        # post-edit self-heal: fast (ruff/node --check/json) + semantic (pyright/tsc/cargo/go vet)
├── notify.py          # run-completion desktop + webhook notifications
├── spend_guard.py     # cross-run daily spend cap
├── repomap.py         # goal-ranked repo map (files → key symbols)
├── safety.py          # workspace confinement + auto/approve/dryrun gate
├── subagents.py       # parallel read-only research fan-out
├── memory.py          # AGENTS.md project memory
├── gitops.py          # branch/commit/PR + working-tree snapshots (/undo)
├── finisher.py        # post-run memory write-back + git/PR node
├── sessions.py        # session persistence, /resume, per-node checkpoints
├── tokens.py          # token estimation, thread-safe ledger, usage report
├── context.py         # categorized, budget-trimmed prompt assembly
├── compress.py        # log/output compressor
├── summarize.py       # rolling task-state summarizer
├── index_tools.py     # wells-index bindings + stale-core self-repair
├── index_watcher.py   # background incremental re-indexing
├── mcp_server.py      # MCP server (Wells as a tool provider)
├── mcp_client.py      # MCP client (external tools for the agent)
├── logo.py            # TUI glyph lockup
├── principles.py      # AGENT.md injection
├── skills.py          # Agent skills: discoverable SKILL.md, load-on-demand
├── codeact.py         # CodeAct: sandboxed run_code tool
├── browser.py         # Browser tools: navigate/click/type/read/screenshot (Playwright)
├── sandbox.py         # sandbox mode: disposable per-workspace container (Podman/Docker) for run_command
├── background.py      # Background agents: bg_start/bg_status/bg_collect (research/fix/worktree)
├── worktree.py        # Per-subagent git worktrees (role=worktree isolation + cherry-pick)
├── personas.py        # Subagent personas: agents/<name>/PERSONA.md, discoverable custom identities*
├── user_memory.py     # Global user memory: ~/.wells/memory/, cross-project standing preferences
├── schedule.py        # wells schedule: unattended recurring runs (Task Scheduler / cron)
├── todo.py            # update_todos tool: model-declared task breakdown for the info panel
└── agents/            # planner / architect / coder / tester / reviewer
wells-index/           # Rust structural indexer (tree-sitter + SQLite)
.github/workflows/     # ci.yml (pytest) + release-index.yml (PyPI wheels)
```

<sup>*`personas.py` manages the workspace-level `agents/` **content** directory
(user-authored `PERSONA.md` files) — a different thing from the
`src/wells/agents/` **package** above it (the harness's own planner/
architect/coder/tester/reviewer code). Named `personas.py`, not `agents.py`,
specifically to avoid colliding with that package.</sup>

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `MODEL_PROFILES` | `zai` | Comma-separated list of configured profile names |
| `MODEL_PROFILE` | `zai` | Active profile for main reasoning/coding |
| `MODEL_PROFILE_CHEAP` | _(blank)_ | Profile for low-stakes subtasks (defaults to active) |
| `MODEL_<name>` / `API_KEY_<name>` / `BASE_URL_<name>` | — | Per-profile model, key, endpoint |
| `MODEL_PRICE_<name>` | _(rate table)_ | Exact $/1M rates: `in,out` |
| `WORKSPACE_ROOT` | `cwd` | Directory the agent is confined to |
| `HARNESS_SAFETY` | `auto` | `auto` / `approve` / `dryrun` (or use `/mode`) |
| `PLAN_MODE` | `0` | When on, mutating tools simulate |
| `MAX_ITERATIONS` | `0` (no limit) | Max coder↔reviewer loops |
| `MAX_TOOL_STEPS` | `0` (no limit) | Max tool-call rounds per executor run |
| `PLANNER_MAX_STEPS` / `TESTER_MAX_STEPS` / `REVIEWER_MAX_STEPS` / `SUBAGENT_MAX_STEPS` | `0` (no limit) | Per-agent step caps |
| `MAX_RUN_TOKENS` | `0` (off) | Hard token cap per run; warns at 80% |
| `SELF_CHECK` | `1` | Post-edit lint/syntax self-heal |
| `WELLS_SEMANTIC_CHECK` | `0` | Post-edit **type-checker** self-heal (pyright/mypy, tsc, cargo check, go vet) — project-aware, opt-in |
| `WELLS_NOTIFY` | `0` | Desktop + webhook notification on run completion |
| `WELLS_NOTIFY_WEBHOOK_URL` | _(blank)_ | POST target for run-completion notifications (Slack-compatible payload) |
| `WELLS_DAILY_BUDGET` | `0` (off) | Refuse new runs once today's cumulative spend reaches this many dollars |
| `CHEAP_VERIFY` | `1` | Route tester/reviewer to the cheap profile |
| `AUTO_COMMIT` | `0` | Commit each successful run (Conventional Commits) |
| `STREAM_OUTPUT` | `1` | Stream answers token-by-token |
| `INDEX_AUTO_UPDATE` | `1` | Keep the repo index fresh automatically |
| `MCP_SERVERS` | _(blank)_ | JSON server map; overrides `~/.wells/mcp.json` |
| `SHELL_TIMEOUT` | `120` | Max seconds for a single shell command |
| `TOKEN_BUDGET_MAX_INPUT` | `24000` | Input budget per call (above this, trims) |
| `SUMMARIZE_ON_LOOP` | `1` | Replace durable context with a summary on loops |
| `LLM_TIMEOUT` / `LLM_MAX_RETRIES` | `180` / `5` | Per-call timeout / transient-error retries |
| `WELLS_OPEN_PR` | `0` | When `1`, the finisher pushes + opens a PR via `gh` |
| `WELLS_PRINCIPLES` | _(bundled)_ | Path to a custom AGENT.md constitution |
| `BLOCKED_COMMANDS` | _(see source)_ | `\|`-separated regex patterns always refused |
| `WELLS_MASK_BATCH` | `4` | Batch-stable masking: don't re-mask until the cutoff has advanced this many rounds past the last batch (0 = mask every round, the old behavior) |
| `WELLS_SKILLS` | `1` | Discover `skills/<name>/SKILL.md` and expose `load_skill` (on/off) |
| `WELLS_SKILLS_PATHS` | _(blank)_ | Extra skill search dirs (path-separator list) |
| `WELLS_CODEACT` | `1` | Expose the sandboxed `run_code` tool for in-context computation |
| `CODEACT_TIMEOUT` | `30` | Max seconds for a single `run_code` execution |
| `WELLS_BG_AGENTS` | `1` | Expose `bg_start` / `bg_status` / `bg_collect` for concurrent fan-out |
| `WELLS_BG_WORKTREES` | `1` | Allow `bg_start role=worktree` (isolated git worktree per sub-agent) |
| `MODEL_PROFILE_VISION` | _(blank)_ | Profile routed to for image-attached tasks when the active profile isn't vision-capable (defaults to active) |
| `WELLS_BROWSER` | `1` | Expose the `browser_*` tools (set `0` to disable; still needs the `browser` extra installed to actually run) |
| `WELLS_BROWSER_HEADLESS` | `1` | Run the browser session headless (set `0` to watch it) |
| `WELLS_BROWSER_EXECUTABLE` | _(auto-detect)_ | Pin an exact browser executable path, skipping auto-detection of Chrome/Edge/Brave |
| `WELLS_SANDBOX_IMAGE` | `python:3.12-slim` | Container image used for `sandbox` mode's disposable container |
| `WELLS_SANDBOX_RUNTIME` | _(auto)_ | Pin the container CLI (`podman` or `docker`); auto-detects, preferring Podman |
| `WELLS_AGENTS` | `1` | Discover `agents/<name>/PERSONA.md` and expose them to `bg_start(persona=...)` |
| `WELLS_AGENTS_PATHS` | _(blank)_ | Extra persona search dirs (path-separator list) |
| `WELLS_USER_MEMORY` | `1` | Inject standing preferences from `~/.wells/memory/` into every project's prompts |
| `WELLS_USER_MEMORY_DIR` | `~/.wells/memory` | Override the global memory directory (mainly for tests) |
| `WELLS_TODO` | `1` | Expose `update_todos` for a model-declared live task breakdown in the info panel |

Legacy `ZAI_*` variables keep working unchanged — they seed the built-in `zai`
profile.

## Token & cost optimization

| Component | What it does |
|---|---|
| **Estimator + Ledger** | tiktoken-based, auto-calibrated; thread-safe per-step actuals from `usage_metadata` |
| **Dollar pricing** | Live cost in the status bar and run footers |
| **Observation masking** | Old tool outputs compressed to typed one-liners; AI reasoning turns kept verbatim |
| **Batch-stable masking** | Masking fires in batches (`_MASK_BATCH_ROUNDS`), so the provider's prompt cache stays warm between batches instead of being invalidated every round. `wells analyze` reports cache breaks round-by-round |
| **Working memory** | Compact structured state (files read/modified, failed approaches, test status) injected every round — prevents re-reads and repeated failures |
| **Repo map** | Goal-ranked structure injection — fewer discovery steps |
| **Deterministic gates** | Real test runs and fast checkers replace LLM judgment calls where possible |
| **Summarizer + trimming** | Rolling task-state summary on loops; categorized budget trimming |
| **Model router** | Cheap profile for summarization/classification/verification |

## Tests & CI

```bash
uv run pytest -q          # 700+ tests (a handful more if the optional
                           # `browser` extra + Chromium are installed)
```

The suite covers provider resolution, tool confinement + every safety mode,
the executor loop (mocked model — no API credits needed), cancellation and
budget stops, graph routing (complexity skip, test-gate fail-fast), fuzzy
edits, self-heal checkers, repo-map ranking, git snapshot/undo, pricing, MCP
client CRUD, background-agent worktree lifecycle (create/merge/conflict/reap),
and the settings persistence. GitHub Actions runs it on every
push/PR (`ci.yml`); `release-index.yml` builds and publishes `wells-index`
wheels (Linux/macOS/Windows × Python 3.12/3.13) to PyPI on an `index-v*` tag.

## Roadmap

- ~~Embedding-based retrieval for very large repos.~~ ✅ Shipped — see
  "Semantic code retrieval" below.


## Semantic code retrieval

Wells ships optional **embedding-based** symbol search alongside the
structural indexer. The `semantic_search` tool finds functions/classes by
*meaning* rather than exact name — useful when the user (or the planner)
describes what something does without naming it.

- **Model**: `BAAI/bge-small-en-v1.5` (384-dim, runs locally via ONNX).
- **Storage**: `sqlite-vec` virtual table inside the existing
  `.wells_index/index.db` — no separate vector database.
- **Repomap re-rank**: when embeddings are available, `build_repo_map`
  blends cosine similarity with the keyword heuristic, so files are ranked
  by semantic relevance to the goal (capped at +8.0 boost so keyword hits
  still dominate).

### Install behaviour

The launcher (`wells.bat` / `wells`) auto-installs `fastembed` and
`sqlite-vec` on first run, after the main `uv sync`. The install is:

- **Cached**: a stamp file (`.venv/.embed-stamp`) keyed on `pyproject.toml`
  means it only runs once per spec change.
- **Best-effort**: if the install fails (e.g. corporate proxy blocks PyPI),
  Wells still starts; `semantic_search` returns an informative message and
  the other tools are unaffected.
- **Opt-out**: set `WELLS_NO_EMBEDDINGS=1` to skip the install entirely
  (e.g. for CI or minimal-footprint installs).

Manual install also works: `uv pip install fastembed sqlite-vec`.

On first `semantic_search` call, the corpus is embedded once (a few seconds
for a typical repo); subsequent calls hit the cached table.

