# AGENTS.md — project memory for the Wells harness

This file accumulates durable facts the agent has learned about this repo.
Edit or prune freely; it is version-controlled with the code.

## Lessons Learned

### 2026-11-20 — Ecosystem round: cross-harness skills, sandbox auto, reflexion mirror
- **skills.py** now discovers `.claude/skills`, `.agents/skills`, `.codex/skills`
  (workspace) + `~/.claude/skills`, `~/.agents/skills` (user) — Agent Skills
  packs written for Claude Code/Codex/Kimi work unmodified. First-wins order:
  workspace `skills/` shadows cross-harness on collision. WELLS_CROSS_SKILLS=0.
- **safety.policy()**: `auto` upgrades to `sandbox` when WELLS_SANDBOX_AUTO=1
  AND a container runtime answers (probe cached in _sandbox_probe_result).
  Opt-in for now; flipping the shipped default is a release decision.
- **reflections.capture_from_state** mirrors critiques into the BigBrain log
  (keyword-recallable via memory_search) after the dedup+gate checks pass.

### 2026-11-20 — Feature day: BigBrain bridge, Code Mode, ACP server
- **BigBrain** (`wells.memory_bridge`): library-only embed of bigbrain-memory
  (installed editable from Q:\\BigBrain; optional dep `wells[memory]` via git
  URL — needed `tool.hatch.metadata.allow-direct-references = true`).
  Finisher writes lessons to the BigBrain transaction log + deterministic
  compile; `memory_search` is the read-only recall tool. Retrieval stays
  Wells-side (fastembed) — BigBrain is the store, not the retriever.
- **Code Mode** (`wells.codemode`): `program` tool = one Python program that
  calls harness tools as functions on `tools.*`, every inner call through
  tools.dispatch so safety gating applies *inside* the program. Restricted
  exec (no open/exec/eval/__import__; allow-listed stdlib imports), budgets:
  40 calls / 200k chars / timeout. NOT a hard sandbox — documented as such.
  WELLS_CODE_MODE=0 disables (weak/local model profiles should).
- **ACP server** (`wells.acp_server`, entry `wells-acp`): JSON-RPC over
  stdio, LSP framing, session/new + session/prompt + session/cancel; runs
  the headless `wells.main._run_goal` path. Zed config: agent_servers →
  command "wells-acp".
- **Gotcha**: ACP cancel tests left CONTROL.cancel() set, which made every
  LATER test file (background/codemode/executor) abort at its first
  checkpoint. Autouse CONTROL.reset() fixture added. Global control state
  requires teardown hygiene in any test that touches cancellation.

### 2026-11-20 — run_code Windows fix
codeact.py resolved `python` from PATH → Windows Store stub (exit 9009).
Fixed by preferring `sys.executable`. The 8 historical test_codeact.py
failures are gone; suite is fully green.

### 2026-11-20 — Observability wiring (Phase 1)
Wells went dark during runs because `_invoke_with_retry` (config.py) logged
via bare `print()` (bypassing the CONTROL event bus) and no `llm_start`
existed before the first streamed chunk. Fixed by emitting `llm_start` /
`llm_retry` / `first_token` / `tool_progress` events and wiring graph nodes
through `CONTROL.stage_start/end` via `_with_stage` in graph.py. The quiet
contract (test_quiet_executor_emits_no_ui_events) still holds — retry events
intentionally bypass quiet gating because the old prints leaked to the main
log anyway, just unstyled.

### 2026-11-20 — test_codeact.py failures are environmental, not regressions
8 tests in test_codeact.py fail with exit 9009 ("Python was not found") —
`run_code` shells out to `python` which resolves to the Windows Store stub
on this machine. Fix the PATH/App Execution Aliases before blaming code
changes.

### 2026-08-27 12:43 — Return the top 5 current headlines from www.cnn.com. Use your web tools to actually fetch the page; do not answer from m
Summary: [executor error: NotFoundError: error while getting model: get model '"hf.co/ibm-granite/granite-4.2-3b-GGUF:Q8_0"': model not found]

### 2026-08-27 12:34 — Return the top 5 current headlines from www.cnn.com. Use your web tools to actually fetch the page; do not answer from m
Summary: [executor error: NotFoundError: error while getting model: get model '"hf.co/ibm-granite/granite-4.2-3b-GGUF:Q8_0"': model not found]
