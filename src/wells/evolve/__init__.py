"""Bench & evolution subsystem (SEACS SPEC-009 built into Wells).

Phase 1 — the measurement foundation everything else gates on:

  * :mod:`wells.evolve.schema` — the task-bundle data model (one historical
    bug fix + its deterministic test oracle).
  * :mod:`wells.evolve.corpus` — git-history mining with dual validation
    (tests must fail at base, pass at fix) and deterministic 60/20/20
    train/val/blind splits.
  * :mod:`wells.evolve.runner` — executes a split as isolated headless
    harness runs in throwaway worktrees, scored by the oracle (never by the
    model's own claim), with Wilson-bounded pass@1 metrics.

Later phases (harness mutation, gating, Elo ladder) consume these two
primitives: nothing self-modifies until it can be measured honestly first.
"""

from wells.evolve.corpus import (
    assign_split,
    list_tasks,
    load_corpus,
    mine_corpus,
)
from wells.evolve.runner import (
    BenchRun,
    run_bench,
    summarize,
    wilson_lower_bound,
)
from wells.evolve.schema import SchemaError, TaskSpec, Oracle

__all__ = [
    "BenchRun",
    "SchemaError",
    "TaskSpec",
    "Oracle",
    "assign_split",
    "list_tasks",
    "load_corpus",
    "mine_corpus",
    "run_bench",
    "summarize",
    "wilson_lower_bound",
]
