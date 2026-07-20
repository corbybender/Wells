---
name: worktree-agents
description: How to fan out parallel WRITES safely with bg_start role=worktree (isolated git worktree per sub-agent, merged back on collect).
---

Use `bg_start role="worktree"` when you need to dispatch **multiple write
sub-tasks in parallel** and an inline `role=fix` would risk two agents
clobbering each other in the parent's working tree.

## When to pick which role

| Situation | Role |
|---|---|
| Read-only investigation (no edits) | `research` |
| One editor, or N editors hitting disjoint files | `fix` |
| N editors that might touch overlapping files, or you want a clean abort-on-conflict boundary | `worktree` |

`worktree` requires git. Non-git workspaces get an error pointing at `fix`.

## Pattern

1. **Spawn** with `role="worktree"`. Each call creates a fresh
   `git worktree` off the parent's current HEAD on its own branch under
   `wells-bg/<id>/...`. Returns a `bg-N` handle immediately; the sub-agent
   runs concurrently in its own checkout.
2. **Keep working** in the parent — reads, edits, tests all safe; the
   parent's working tree is untouched by the worktree agents.
3. **Poll** with `bg_status` until each slot reaches `done` / `error`.
4. **Collect** each with `bg_collect`. On a clean merge, the sub-agent's
   single commit is cherry-picked into the parent. On conflict, the
   cherry-pick is **aborted** and the worktree-vs-base diff is returned
   in the report (the worktree is reaped either way) — re-apply manually.

## Conflict handling — by design

Conflicts are not retried and not auto-merged. The harness deliberately
returns the diff so *you* (the parent agent) decide how to combine the
changes. Typical resolution: read the diff in the report, then
`edit_file` the affected region directly in the parent.

## Example

```
bg_start(task="Refactor auth middleware to use JWT",  role="worktree")  → bg-1
bg_start(task="Refactor session middleware to use redis store", role="worktree")  → bg-2
bg_start(task="Replace rate-limiter with token bucket", role="worktree")  → bg-3

# All three edit concurrently in their own checkouts.
bg_status   # → bg-1: done, bg-2: done, bg-3: done

bg_collect(id="bg-1")   # → merged cleanly into parent
bg_collect(id="bg-2")   # → CONFLICT (parent diverged); diff returned
                         #   re-apply manually from the diff in the report
bg_collect(id="bg-3")   # → merged cleanly into parent
```

## Limits / gotchas

- **One commit per sub-agent.** The harness commits all pending worktree
  edits into a single commit on the worktree branch before cherry-picking.
  If the sub-agent made multiple logically-distinct changes you don't want
  squashed, run separate `bg_start role=worktree` calls for each.
- **Base is fixed at spawn time.** The cherry-pick range is
  `<parent-HEAD-at-spawn>..<worktree-tip>`. If the parent advances HEAD
  (commits something else) before collect, the cherry-pick still applies
  the sub-agent's *changes* onto the new HEAD — may conflict, may not.
- **Cancellation reaps worktrees.** `Escape` / `CONTROL.cancel()` or the
  start of the next executor run will remove outstanding worktrees; the
  sub-agent's work is lost. Collect before cancelling if you want the
  work.
- **Kill switch:** set `WELLS_BG_WORKTREES=0` to refuse the role without
  disabling `bg_start`/`bg_status`/`bg_collect` entirely.

## Implementation

- Primitives live in `src/wells/worktree.py` (create / commit_pending /
  cherry_pick_into_parent / remove_worktree / reap_stray_worktrees).
- Integration lives in `src/wells/background.py`: `_bg_start` handles
  `role="worktree"`; `_finalize_worktree_slot` (called from `collect`)
  does the commit + cherry-pick + reap; `REGISTRY.reset` reaps any
  still-pending worktrees so a cancelled run can't leak disk.
- Tests in `tests/test_background_worktrees.py` cover the primitives and
  the integrated bg flow (clean merge, conflict, non-git rejection,
  kill-switch, reset-reaps).
