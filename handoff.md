# HANDOFF — Wells (Windows box, `Q:\wells`)

Date: 2026-11-23. Supersedes the 2026-08-22 Linux handoff, whose facts are
now all stale (1019 tests, a 10-task corpus with an empty val split, and
pre-rewrite commit SHAs). Read AGENTS.md for the durable lessons; this file
is the state of play.

Published history was **rewritten on 2026-11-23** (see §4), so every SHA
quoted in older notes and in the 22 Wells-derived corpus tasks has changed.

---

## 1. What happened this session, in order

1. **Commit identity.** All 232 published commits and all 5 annotated tags
   were rewritten (`git filter-repo --mailmap`) to the personal identity
   `Corby Bender <corby.bender1976@gmail.com>`. File content is byte-identical
   (same tree hash). Old SHAs are no longer on any branch or tag but GitHub
   still serves them by full SHA; only GitHub Support can purge them.
2. **Corpus made portable** — the real blocker. `repo_root` was an absolute
   path on the Linux box, so 58 of 70 tasks (11 of 12 val) could not run here
   and `run_bench` refused to start; no gate had ever run on Windows. Tasks
   now carry `repo_url`, `corpus.resolve_repo_root` clones a mirror under
   `~/.wells/bench/repos/` when needed, and `wells bench verify` re-runs the
   admission gate locally. All 70 resolve here; 11 of the original 12 val
   tasks verify (the twelfth needs `pytest-httpbin` and is left out on this
   machine only — see §3).
3. **Repetition guard fixed** (test-first). It could stop a productive
   multi-file rename (search keys hold no file path, so writes never reset
   them) and it folded every search in one directory into one counter.
4. **ACP gaps closed**: token-level streaming, tool-call notifications,
   `fs/read_text_file`, `session/load` with persisted transcripts and
   follow-up context.
5. **Mega-task split.** `T-c9d4f56d48` (ACP + Code Mode + BigBrain in one
   commit-message statement, 36 hidden tests) became three val tasks whose
   statements specify the contract their tests check. All three verify.
6. **Two live bench runs**, which is where the remaining fixes came from:
   - 30 min on the old mega-task: 1.47M tokens, timeout, planner made 83
     calls with the guard correctly silent.
   - 10 min on a click task: killed by an exhausted provider quota, which
     exposed two harness bugs (dead progress forwarding, quota treated as
     transient). Both fixed; artifacts now survive a run.
7. **Windows watchdog** (`scripts/wells_resumable_launch.ps1`) so long runs
   can be restarted from checkpoint without a live shell.

## 2. Commits from this session (unpushed at the time of writing)

```
af0a9a4  feat(acp): stream text deltas and tool calls, fs/read_text_file, session/load
61ae2e2  feat(bench): portable corpus — repo_url, resolve on any machine, bench verify/repos
2eef35a  fix(executor): repetition guard no longer stops productive runs
0bca452  fix(bench): keep run evidence and forward the harness's real progress
c2cbd52  feat(bench): split the 3-feature mega-task into three single-feature tasks
14a7c90  fix(llm): treat an exhausted quota as permanent, and stop looping on it
```
Plus, already on `origin/master`: `c7bc09b` (corpus SHA remap after the
rewrite) and the three housekeeping commits it replayed.

## 3. Verified state

- **Full suite**: 1112 passed, 26 skipped, 0 failed (`pytest -q`, ~4 min).
- **Corpus**: 72 tasks — 46 train / 14 val / 12 blind; 71 verified, 1
  quarantined.
- **`wells bench verify --split val`**: 13 of 14 pass on this machine;
  `T-6f66281a1d` (requests) needs the `pytest-httpbin` plugin. That failure
  is recorded only in the git-ignored `.wells/bench/corpus/local-verify.json`;
  bench runs here skip the task, `--include-unverified` overrides. The
  bundle itself is untouched because the task is valid where it was
  verified.
- **Model quota**: the `zai` account hit its 5-hour limit mid-session
  (resets 2026-11-24 01:12). Until then every model call fails, so no bench
  or evolve gate can produce a meaningful score.

## 4. History rewrite — what is still outstanding

- GitHub Support purge of the old objects (they remain reachable by full
  SHA; a request has to come from the account owner). The 232 old SHAs are
  in `~/.wells/backups/wells-rewrite-commit-map-20261123.txt`.
- Pre-rewrite backup bundle: `~/.wells/backups/wells-pre-rewrite-*.bundle`.
- The Linux clone still has the old history: `git fetch origin && git reset
  --hard origin/master` there.
- Do not `git gc` in this checkout: it would drop the August stash
  (`stash@{0}`, "pre-migration local WIP"), which was deliberately kept.

## 5. Open work, in priority order

1. **Run a real gate once the quota resets.** The val split is now able to
   produce a score for the first time; `wells bench run --split val
   --profile <profile>` then `wells evolve gate <mutation-id> --split val`.
   Compare against the 0/1 baseline already recorded in
   `.wells/bench/results/`.
2. **Per-task oracle environments.** One requests task needs
   `pytest-httpbin`; a task-local environment description in the bundle
   would make the corpus self-sufficient instead of machine-dependent.
3. **Rust launcher** — a compiled shell around the Python engine for
   millisecond cold start. Not started; the binaries that exist are
   PyInstaller bundles.
4. **Sandbox-by-default** — deliberately not flipped. The sandbox is
   container-only (Podman/Docker), there is no container runtime on this
   machine, and `sandbox.py` sets no `--network`, `--user`, `--cap-drop`,
   `--pids` or `--memory` limits. Two design questions remain: a fixed
   `/workspace` mount gives the agent two path namespaces with no
   translation, and a container-less machine gets no sandbox at all.
5. **Corpus growth** — mine more repos (`wells bench mine --env-setup CMD`),
   especially to grow val beyond 14.

## 6. Environment notes for this machine

- `WORKSPACE_ROOT` in `.env` points at `Q:\corbytools`; bench CLI commands
  in this repo need `$env:WORKSPACE_ROOT='Q:\wells'`.
- No Docker, Podman, nerdctl or WSL: the sandbox path cannot be tested here.
- `gh` is authenticated as `corbybender`; the four tag-triggered workflows
  must be paused around any tag force-push (done during the rewrite).
- Bench logs from this session: `logs/bench-guardcheck-20261123.log`,
  `logs/bench-evidence-20261123.log`; per-step traces now survive under
  `.wells/bench/results/<bench_id>/`.
