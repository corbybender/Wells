# HANDOFF.md — Wells + SEACS evolve subsystem → Linux migration

Date: 2026-08-22. Migrating from Windows (Q:\wells) to
corbybender@ai.corbybender.com:/home/Projects/seacs/.

---

## 1. What this project is

**Wells** — a model-agnostic agentic coding harness (planner → architect →
coder → tester → reviewer graph over LangGraph) with providers, sandboxing,
skills, traces, fleets, scheduling.

**The `evolve` package (new, uncommitted)** — the SEACS SPEC-009 measurement
foundation built directly into Wells:

| Module | Purpose |
|---|---|
| `src/wells/evolve/schema.py` | `TaskSpec` + `Oracle` — one historical fix + its deterministic test oracle (`fail_to_pass` / `pass_to_pass` / `command` / `test_files`), strict validation at every load/save |
| `src/wells/evolve/corpus.py` | Git-history miner: walks non-merge commits (test+source changes only), AST-extracts pytest node ids, **dual-validates** each candidate in a throwaway worktree (target tests MUST fail at base, pass at fix — no LLM judgment), stable sha1 60/20/20 train/val/blind splits |
| `src/wells/evolve/runner.py` | Bench runner: per task = worktree at `base_commit` → full headless Wells run in a subprocess → SWE-bench-style test patch applied at scoring → oracle exit code is the ONLY verdict → Wilson-bounded pass@1 metrics |
| CLI | `wells bench mine|list|run|results` (see `wells --help`) |

Key invariants baked in:
- The model's own COMPLETE/INCOMPLETE claim is **never** trusted; only the oracle.
- Worktree subprocess isolation (per-run profile + token ledger).
- `source_env()` injects the worktree's own `src/` into PYTHONPATH so
  oracle runs execute the checked-out code, not the installed package.
- UTF-8 forced end-to-end on all subprocess pipes.

## 2. Verified state at handoff

- **Tests**: `tests/test_evolve.py` — 13/13 pass; full suite ~1000 pass
  (was verified pre-handoff on Windows; rerun on Linux as first checkpoint).
- **Corpora on disk** (inside the zip, under `.wells/bench/corpus/`):
  - Wells' own history: **10 verified tasks** (7 train / 0 val / 3 blind).
  - `Q:/cws_HUX_ide_project`: **1 verified val task** (see §5 gotcha — the
    HUX repo itself is NOT in this zip).
- **Recorded bench results** (`.wells/bench/results/`, 5 runs):
  | bench | task | outcome |
  |---|---|---|
  | 20260822-093305 | T-11e9879fdf (semantic self-heal feat) | timeout, CorbyQwen unreachable (0 tokens) |
  | 20260822-095650 | same | harness crashed cp1252 (bug since fixed); oracle honestly scored partial |
  | 20260822-101857 | same | clean timeout at 1200s; tree-kill + oracle verified working |
  | 20260822-105041 | T-24da16076e (sandbox-probe fix) | timeout at 2400s but **14/17 target tests passed** from partial edits |
  | 20260822-113405-b43ac5 | **T-be0aa6dc3a (HUX val)** | **resolved=True** — first full-loop proof: mine → dual-validate → worktree → real run → oracle-verified resolution (pass@1 100%, Wilson LB 20.6% @ n=1) |
  | 20260822-113405-36ffcd | T-24da16076e, 2 seeds | both timeout @ 3600s on zai (task is large; candidate for longer budget or stronger profile) |

- **Windows-specific bugs found & fixed during bring-up** (all now
  platform-guarded, no action needed on Linux):
  - cp1252 pipe decoding → `encoding="utf-8"` + `PYTHONUTF8=1` for children.
  - Orphaned grandchildren hanging the drain → `_kill_tree` (taskkill /T on
    win32; `killpg` + `start_new_session=True` on POSIX).
  - `git log` body separator mangled by strip (no-body commits skipped).
  - Bare `python` resolving to a pytest-less interpreter → `sys.executable`.

## 3. What's in the zip (and what's deliberately NOT)

Included: `src/`, `tests/`, `.git/` (REQUIRED — bench worktrees are created
from this repo's object store), `.wells/` (corpora + results + traces),
`skills/`, `uv.lock`, `pyproject.toml`, `.python-version`, `.env.example`,
docs, installers, bench logs (small, kept as evidence).

Excluded:
- **`.env` — contains live API keys. Copy it over manually and securely**
  (see §4 step 3). Do not commit it anywhere.
- `.venv/` (364M Windows binaries — rebuild with `uv sync`).
- `wells-index/` (279M Windows Rust build + grammar submodules — rebuild on
  Linux or run without the indexer; Wells degrades gracefully to grep).
- `.fastembed_cache/`, `.ruff_cache/`, `.pytest_cache/`, `__pycache__`,
  `.claude/`, `.wells_index/` (index cache).

## 4. Linux bring-up (in order)

```bash
# 0. (On the Windows/origin machine) send the archive:
#    ssh corbybender@ai.corbybender.com "mkdir -p /home/Projects/seacs"
#    scp seacs_handoff.zip corbybender@ai.corbybender.com:/home/Projects/seacs/

# 1. Unpack
cd /home/Projects/seacs
unzip -q seacs_handoff.zip -d .

# 2. Line endings: files were zipped from a Windows working tree.
#    If `git status` shows mass modifications, renormalize:
git config core.autocrlf input
git checkout -- .
git status   # should show only the real changes (below)

# 3. Secrets: copy .env manually (NOT in the zip), e.g. from origin:
#    scp Q:/wells/.env corbybender@ai.corbybender.com:/home/Projects/seacs/.env
chmod 600 .env

# 4. Python env (requires uv: curl -LsSf https://astral.sh/uv/install.sh | sh)
uv sync          # rebuilds .venv from uv.lock (Python pinned via .python-version)

# 5. Fix corpus repo_root paths (task JSONs embed the Windows path):
python - <<'EOF'
import json, pathlib
for p in pathlib.Path('.wells/bench/corpus/tasks').glob('*.json'):
    d = json.loads(p.read_text(encoding='utf-8'))
    if d.get('repo_root', '').lower().startswith('q:'):
        d['repo_root'] = str(pathlib.Path('.').resolve())
        p.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding='utf-8')
        print('rewrote', p.name)
EOF

# 6. Optional: rebuild the tree-sitter indexer (else grep fallback)
git submodule update --init
# then build wells-index per its README (cargo build --release)

# 7. Verify the whole stack
uv run pytest tests/test_evolve.py -q          # 13 passed
uv run pytest -q                               # full suite
uv run python -c "from wells.main import main; main()" bench list

# 8. FIRST COMMIT on Linux — the evolve subsystem is uncommitted work
#    (.wells/ is gitignored; force-add only the corpus + results):
git add src/wells/evolve tests/test_evolve.py src/wells/main.py \
        src/wells/_gitutils.py handoff.md
git add -f .wells/bench/corpus .wells/bench/results
git commit -m "feat(evolve): SEACS phase 1 — task corpus mining, bench runner, oracle scoring"
```

## 5. Gotchas & known gaps

1. **The only val task lives in another repo.** `T-be0aa6dc3a` was mined
   from `Q:/cws_HUX_ide_project` (its corpus + results are in this zip, but
   the repo isn't). Either also copy that repo and rewrite its
   `repo_root`, or drop `.wells/bench/corpus/tasks/T-be0aa6dc3a*.json` and
   re-mine/grow val coverage on Linux. Wells' own corpus currently has
   7 train / 0 val / 3 blind.
2. **Re-mining needs history depth.** Wells' 192 commits are nearly
   exhausted under the strict test+source-same-commit rule. To grow the
   corpus: mine other repos (`wells bench mine --workspace <repo>`) or
   loosen the heuristic in `corpus.iter_candidates`.
3. **Timeouts on big tasks.** T-24da16076e (17-test sandbox fix) hit 14/17
   at 40 min and 0/17-ish at 60 min on the zai profile — budget 90+ min or
   use a stronger profile for train-split work; the val task resolved
   within 60.
4. **Tokens lost on timeout**: the harness child is tree-killed before its
   JSON payload is emitted, so `tokens_total=0` on timeout rows. Fix idea:
   have the child ledger flush incrementally (a `--tokens-file` flag).
5. **Sandbox tests** use podman/docker — native on Linux, so
   `tests/test_sandbox.py` should behave better than on Windows.
6. **Profile for runs**: `MODEL_PROFILE=CorbyQwen` (home-hosted) is default
   in .env but was unreachable at handoff; the zai cloud profile is the
   proven one for benches (`--profile zai`).

## 6. Next steps (post-migration, in priority order)

1. Bring-up per §4; commit the evolve baseline.
2. Prove more `resolved=True`: run the val split (needs §5.1 resolved) and
   retry T-24da16076e with 90-min budget.
3. **Phase 2 — the evolution engine**: mutate the harness's soft tissue
   (AGENT.md principles, tool descriptions, skills), gate each mutation on
   `bench run --split val` Wilson LB + `wells replay` (trace corpus) as the
   regression suite; promote/rollback/version. This is the actual SEACS
   self-improvement loop; everything built so far is its measurement
   foundation.
4. **Phase 3**: blind-split ledger + harness-version Elo ladder; consider
   vLLM on the 5060 Ti for cheap routing (subagents/summaries) while
   keeping frontier profiles for architect/coder.
5. Optional: token-loss fix (§5.4), per-phase timing metrics, concurrent
   bench workers (runner is embarrassingly parallel per task/seed).

## 7. Quick reference

```bash
wells bench mine --workspace <repo> [--max N] [--skip-validation]  # build corpus
wells bench list [--split train|val|blind|all]                     # inspect corpus
wells bench run --split val --profile zai [--task T-xxxx] [--seeds K] [--timeout S]
wells bench results [ID]                                           # view runs
wells replay latest                                                # harness regression check
```

Architecture docs: README.md (Wells), RULES.md (operating rules the agent
itself must follow), `src/wells/evolve/` docstrings (design rationale per
module).
