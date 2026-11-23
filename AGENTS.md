# AGENTS.md — project memory for the Wells harness

This file accumulates durable facts the agent has learned about this repo.
Edit or prune freely; it is version-controlled with the code.

## Lessons Learned

### 2026-11-20 — Distribution: PyPI + binaries shipped; two real bugs found by shipping
- PyPI name is **wells-agent** (bare `wells` taken). `bigbrain-memory` and
  `wells-index` also published (trusted publishing, OIDC, env `pypi`, no
  tokens). Install is ONE command again since 0.4.0:
  `uv tool install wells-agent`.
- **Hard dep on native modules breaks everything — UNLESS the wheels are
  abi3**: `wells-index` (PyO3) as a core dep with cp312/cp313-only wheels made
  `uvx wells-agent` unresolvable on CPython 3.15 (the 0.3.0 bug). Fixed for
  good in wells-index 0.2.0 with `pyo3/abi3-py312` — stable-ABI wheels, one
  per platform, install on every future CPython. Since then it's a core dep
  again. Lesson: native deps belong in core only with abi3 wheels.
- **Trusted publishers must be re-registered after a repo rename**: the
  wells-index publisher still pointed at the pre-rename
  `corbybender/Wells-Coding-Harness`, so OIDC publish failed with a bare
  "exchange failure". Project-level publisher page lives at
  pypi.org/manage/project/<name>/settings/publishing/.
- Version lived in 3 places and drifted (`--version` said 0.2.0 while the
  package was 0.3.0). Now single source: importlib.metadata in
  `wells/__init__.py`; mcp_server imports `__version__`.
- **macos-14 label is RETIRED** ("configuration 'macos-14-arm64-us-default is
  not supported") and macos-13 never schedules; `macos-15` / `macos-15-intel`
  work. Probed live on a throwaway branch before burning another release tag.
- Gotcha: `workflow_dispatch` 404s unless the workflow exists on the default
  branch — use a `push:`-triggered probe branch instead.
- Lost work by committing real changes on a probe branch then deleting it —
  recovered via `git cherry-pick -n` from the reflog. Don't mix probe and
  product commits.

### 2026-11-20 — Bench forensics: alternating loop defeated the stuck-loop guard
60-min val run on T-c9d4f56d48: **4,609 tool calls** (2,177 list_dir, 1,488
read_file; 856 reads of one throwaway `maths.py`), 0/1 resolved, tokens=0.
The agent **alternated** read_file → list_dir → read_file, which resets the
consecutive-repeat detector (`_last_call_repeat`) every round — the guard
structurally cannot see it. Fix: cumulative per-target counter
(`_repeat_hits`) for read-only tools — coach at 4, escalate/hard-stop at 8,
reset on write to that path (`WELLS_REPEAT_WARN`/`WELLS_REPEAT_STOP`).
Also: bench timeout is **per task**, not per run. And T-c9d4f56d48 is a
3-feature mega-task (ACP + Code Mode + BigBrain, 35 fail_to_pass tests) —
arguably too large to be a fair corpus task; consider splitting.

### 2026-11-23 — Corpus is portable; the guard's two false positives; history rewrite
- **History rewritten** (`git filter-repo --mailmap`) so every author, committer
  and tagger is the personal identity. Old SHAs are gone from branches/tags but
  still reachable from GitHub by full SHA (Support purge is the only fix).
  Commit map kept at `~/.wells/backups/wells-rewrite-commit-map-20261123.txt`;
  pre-rewrite bundle alongside it. The 22 Wells-derived tasks were remapped, so
  the corpus still pins live commits. Task ids keep their OLD sha prefix on
  purpose (the split is a hash of the id); `mine_corpus` therefore also skips on
  (fix-commit message + test files), not just task id.
- **`repo_url` makes the corpus portable.** `repo_root` was an absolute path on
  the mining machine, so 58/70 tasks (11/12 val) were unrunnable on Windows and
  `run_bench` refused to start — the real reason no gate had ever run here.
  `resolve_repo_root` falls back to a mirror under `~/.wells/bench/repos/`.
  `wells bench repos` reports resolution; `wells bench verify` re-runs the
  admission gate locally. Successes are stored in the bundle; **failures are
  not** — they go to git-ignored `local-verify.json`, because a task can be
  perfectly valid where it was first verified, and bench runs here skip it
  (`--include-unverified` overrides).
- **Oracle runs scrub `*_PROXY`** from the inherited environment: requests'
  suite asserts `no_proxy` is unset, so an agent session exporting NO_PROXY made
  a *correct* fix commit fail validation. An oracle measures the patch, not the
  shell that launched it.
- **Repetition guard false positives (fixed, test-first).** It cleared only the
  edited path's counter, but a search key (`grep:<pattern>`) holds no path, so
  the ordinary rename rhythm (grep old name → fix next file → grep again) hit
  the 8-hit hard stop. Any successful mutation now clears `_repeat_hits`. Also
  `grep`/`glob` keys were path-first, folding every search in one directory into
  a single counter; they are now `pattern@path`. Live 30-min run on the old
  mega-task: planner 83 calls, guard silent and correct — under old keys it
  would have fired.
- **Bench evidence survives now.** `_execute_task` copies `.wells/traces/*.json`
  into `<bench_id>/<task>-s<seed>/` before the worktree is deleted, and
  `TaskResult` records `artifacts` + `tool_calls`. Before this, a 30-minute
  timeout left only the word "timeout" (and 1.47M spent tokens).
- **Bench progress forwarding was dead**: in `--output-format json` mode
  `_run_goal` redirects stdout to stderr, and the runner only forwarded stdout.
  stderr is forwarded now.
- ACP gaps closed: token-level streaming, `fs/read_text_file`, `session/load`
  (transcripts under `~/.wells/acp/sessions/`, follow-ups carry earlier turns).
- `scripts/wells_resumable_launch.ps1` is the Windows watchdog. No pidfile — it
  holds the `Start-Process` handle, so a recycled PID cannot fool it (the bash
  version has to verify PIDs). Test gotcha: probing strays with
  `CommandLine -like '*fake_job.ps1*'` matches the *probe's own* command line;
  record PIDs from the child instead.
- Mega-task `T-c9d4f56d48` split into `T-583db3ff8f-{acp-server,code-mode,bigbrain-memory}`,
  all val (hand-assigned: the id hash would have put two in train). Its problem
  statement was a commit message while 36 hidden tests pinned internal APIs, so
  each child's statement now specifies the contract.
- **Out-of-quota is not a rate limit.** A 5-hour quota 429 was classed as
  transient, so every call burned the full retry budget (30s) and the graph then
  ran all nine iterations against an untouched tree — 10 minutes, zero model
  calls. `config._quota_exhausted` matches the permanent markers
  ("Usage limit reached", `insufficient_quota`, …), fails at once, records
  `llm_unavailable()` for 30 min, and both graph routers finalize instead of
  looping. `_run_goal` resets the record per goal.

### 2026-11-20 — SEACS corpus growth
Facts: corpus lives at <workspace>/.wells/bench/corpus (**72 tasks as of
2026-11-23: 46 train / 14 val / 12 blind**, 71 verified + 1 quarantined;
the old "10 tasks, empty val" note is long superseded — an empty val bucket
at n=10 is ~11% likely by hash, so it was never evidence of a bug).
`.env` WORKSPACE_ROOT points at q:\\corbytools, so bench CLI
commands need `$env:WORKSPACE_ROOT='Q:\wells'` override in this repo.
Growth levers shipped: `wells bench mine --env-setup CMD` (repeatable;
would have made the `cog` repo minable), `wells bench quarantine ID`
(rejected tasks stay on disk but never run — T-24da16076e sandbox-probe task
quarantined), and list_tasks now filters status=rejected.
**UTF-8 bug found by mining**: _gitutils.git used text=True without
encoding → cp1252 reader-thread crash on any non-ASCII commit subject
(em-dashes!) → mining silently saw 0 candidates. Fixed with
encoding="utf-8", errors="replace" in _gitutils + corpus env_setup path.
Also fixed in codeact.py: prefer sys.executable over PATH python.

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
