# HANDOFF.md — Wells + SEACS, Linux (RTX 5060 Ti box)

Date: 2026-08-22, end of session. All work below is pushed to
`origin/master` (github.com/corbybender/Wells) — HEAD is `6b8522d`.
Verified via `git fetch && git status` (clean, "up to date with
origin/master", 0 commits ahead/behind) before writing this.

---

## 1. What happened today, in order

1. **Linux migration bring-up** (from the Windows handoff zip) — corpus
   `repo_root` paths rewritten, `.venv`/`uv.lock` synced, full suite green.
2. **Found and fixed a real bug**, not a migration artifact:
   `src/wells/sandbox.py`'s live-container path was SIGKILLing its own
   caller's entire process group on every sandboxed `run_code` call (bad
   `Popen`/`communicate()` interaction feeding a `killpg` with no
   dedicated process group). Also fixed a SELinux Enforcing bind-mount
   permission issue on this host (`:Z` relabel flag).
3. **Wired up `wells bench`** — handoff.md (the old version) documented it
   as done; it wasn't actually connected to `main.py`'s CLI dispatch.
   Fixed.
4. **Built SEACS Phase 2 — the evolution engine** (`wells evolve
   propose|gate|promote|reject`): mutate `AGENT.md`, gate the candidate
   against a baseline via real bench passes + trace replay, promote or
   reject. Scoped to `AGENT.md` only for v1 (see `src/wells/evolve/mutate.py`
   docstring for why, not tools/skills).
5. **README updated** to document bench + evolve, corrected project
   structure (it was already missing `traces.py`/`fleet.py` before today),
   test count bumped to reality.
6. **Speed investigation** (live-profiled a real dummy task, not guessed):
   - `wells.config` eagerly imported `langchain_core.messages` at module
     level — used in exactly one legacy function. Deferred it: 45% faster
     cold-start (114ms → 62ms) on every CLI/subprocess invocation.
   - Found + fixed a false-positive in the executor's silent-failure
     retry guard: "no tests found" wasn't recognized as an acknowledged
     outcome, forcing an unneeded extra round.
   - Found + fixed the actual driver of that: the tester's own prompt
     told the model to explore the repo layout before running tests, when
     `run_tests` already auto-detects the command. Tightened the prompt.
     Measured: tester went from up to 8 exploratory rounds down to 2
     (straight to `run_tests`, correct conclusion, no wasted
     investigation) across 3 live verification runs.

## 2. Commits pushed today (oldest → newest)

```
60ac54f  feat(evolve): SEACS phase 1 baseline + fix(sandbox): self-kill on container exec
baaa228  feat(cli): wire up wells bench mine|list|run|results
ce5da12  feat(evolve): Phase 2 — the evolution engine (AGENT.md mutation, v1)
c11db61  docs(readme): document SEACS bench + evolve, update structure/test count
14070c6  perf(config): defer langchain_core import to first real LLM call
fbfbd4d  fix(executor): recognize "no tests found" as an acknowledged outcome
6b8522d  perf(tester): tell the model to call run_tests first, not explore first
```

## 3. Verified state at handoff

- **Full suite**: 1019 passed, 23 skipped, 0 failed (`uv run pytest -q`).
- **Corpus**: 10 tasks total — 7 train / **0 val** / 3 blind. This is a
  known, unfixed blocker (see §4.1) — `wells evolve gate` defaults to
  `--split val` and will fail loudly (by design, not a bug) until this
  grows.
- **Bench results**: 6 recorded runs in `.wells/bench/results/` (5 from
  the Windows session, kept as evidence + 1 today: the `T-24da16076e`
  90-minute retry, which **still timed out** — see §4.2, do not blindly
  retry this task again).
- **Mutations**: 1 recorded, `20260822-170503-4177cd`, status=`rejected`
  (a real end-to-end smoke test of the propose→gate→reject loop — the
  candidate was a placeholder, not a real AGENT.md improvement; correctly
  rejected on purpose, not a failure).
- **No orphaned sandbox containers, no stray git worktrees** — checked
  clean at end of session (`podman ps -a`, `git worktree list`).

## 4. Known gaps / things NOT to blindly redo tomorrow

### 4.1 Zero val-split tasks — blocks real `evolve gate` runs

10 total corpus tasks, deterministic 60/20/20 hash split landed 0 in
val. `wells evolve gate` (default `--split val`) will error immediately
with the exact remediation message. Options, in order of likely payoff:
- Mine more repos: `wells bench mine --workspace <repo>`. Tried `cog`
  (`/home/corbybender/Projects/cog`, 62 commits) today — only 1 mineable
  candidate found, and it failed pytest collection in the throwaway
  worktree (likely needs the repo installed, not just checked out).
  Worth revisiting with `pip install -e .` in the mining worktree, or
  picking a repo with simpler test collection.
- The HUX val task (`T-be0aa6dc3a`, previously `resolved=True` on
  Windows) needs its source repo (`cws_HUX_ide_project`) copied over and
  `repo_root` rewritten — repo was never included in the migration zip.
  Would restore exactly 1 val task, enough to unblock `evolve gate
  --split val` with n=1 (still noisy, but functional).
- `wells bench mine`'s heuristic (`corpus.iter_candidates`) is strict:
  commit must touch test+source together, tests must AST-parse to pytest
  node ids, and dual-validate (fail at base, pass at fix) in an isolated
  worktree. Loosening it is a real option but changes what "verified"
  means for the whole corpus — think before touching this.

### 4.2 T-24da16076e (sandbox-probe fix task) — do not blindly retry

This specific corpus task has repeatedly timed out across BOTH the
Windows session and a 90-minute Linux retry today. Root-caused today:
its `base_commit` (`41f04dc`) predates the sandbox self-kill fix from
§1.2 — while the agent works on this task, its own verification (running
`pytest tests/test_sandbox.py`) can trigger the exact bug being fixed,
crashing its own progress-check silently (SIGKILL, no traceback). 196
orphaned containers were left behind by the last attempt (cleaned up).
This task may be structurally unreliable as a benchmark on this specific
host (SELinux Enforcing + rootless Podman) independent of model quality.
Don't burn more budget retrying it without a different approach (e.g., a
much longer timeout is not the fix — the problem isn't speed, it's the
agent's own verification loop crashing).

### 4.3 Cost/spend reporting is not trustworthy — verify before citing

`spend_guard.today_spend()` / `pricing.py`'s `_RATE_TABLE` assume
pay-per-token billing for every profile. The `zai` profile here points at
Z.ai's **Coding Plan** endpoint (`api.z.ai/api/coding/paas/v4/`), a flat-
rate subscription — actual marginal cost is most likely near $0, subject
to a request/quota limit, not a dollar figure. `pricing.py` has no
`MODEL_PRICE_zai` override set, so it's silently printing a fictional
per-token estimate. **Do not repeat "$X spent" claims from this project
without checking `pricing.py`'s rate table first** — this bit the agent
once today (see conversation history); the honest fix (`MODEL_PRICE_zai=0,0`
in `.env`, or the correct real rate if there is one) was offered but not
applied — ask the user or apply it before trusting this number again.

## 5. Next steps, priority order

1. **Grow the corpus** (§4.1) — the single biggest lever for making
   `evolve gate` mean anything. Either fix `cog`'s pytest-collection issue
   in the mining worktree, or bring over the HUX repo for its 1 known-good
   val task, or find/mine a third, simpler repo.
2. Once val tasks exist: run a REAL `wells evolve propose --auto` (an
   actual harness-drafted AGENT.md candidate, not the placeholder used
   for today's smoke test) and gate it for real — see if the loop
   surfaces a genuine improvement.
3. Continue the speed work if there's appetite: today's fixes targeted
   round-trip *count* (proven the highest-leverage lever — see the live
   profiling numbers in commit `6b8522d`'s message). The same "tighten
   the prompt to stop redundant exploration" pattern likely applies to
   the coder/reviewer nodes too — profile a real run first before
   guessing, same as today (`WELLS_STARTUP_PROFILE=1` for cold-start;
   there's no live per-node profiler in the codebase yet, today's
   instrumentation was a throwaway script — worth turning into a proper
   `WELLS_NODE_PROFILE=1` env-gated feature in `graph.py`/`executor.py`
   if this becomes a recurring need).
4. Optional, lower priority (from the original Phase-1 handoff, still
   open): token-loss-on-timeout fix (`_run_harness`'s tree-kill happens
   before the child's JSON payload flushes, so `tokens_total=0` on
   timeout rows — a `--tokens-file` incremental-flush flag was the
   suggested fix, never implemented).

## 6. Quick reference

```bash
wells bench mine --workspace <repo> [--max N] [--skip-validation]
wells bench list [--split train|val|blind|all]
wells bench run --split val --profile zai [--task ID] [--seeds N] [--timeout SECONDS]
wells bench results [ID]

wells evolve propose --file candidate.md "rationale"
wells evolve propose --auto "rationale"          # opt-in — real tokens
wells evolve gate <mutation_id> --split val
wells evolve list / show <id> / promote <id> / reject <id>

uv run pytest -q                                  # 1019 passed, 23 skipped
```

Architecture docs: `README.md` (Wells — see "SEACS — oracle-scored bench +
AGENT.md evolution" section), `RULES.md` (operating rules), `src/wells/evolve/`
module docstrings (design rationale per file).
