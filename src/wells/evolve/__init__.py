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

Phase 2 — the evolution engine (:mod:`wells.evolve.mutate`): proposes a
candidate replacement for the harness's own ``AGENT.md``, gates it against
a baseline using the runner above, and promotes or rejects it. The first
thing that *acts* on Phase 1's numbers instead of just producing them.

Later phases (tool/skill mutation, Elo ladder) extend the same
propose/gate/promote shape to more of the harness's soft tissue.
"""

from wells.evolve.corpus import (
    assign_split,
    list_tasks,
    load_corpus,
    mine_corpus,
)
from wells.evolve.mutate import (
    MutationManifest,
    gate_mutation,
    list_manifests as list_mutations,
    load_manifest as load_mutation,
    promote_mutation,
    propose_mutation,
    reject_mutation,
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
    "MutationManifest",
    "SchemaError",
    "TaskSpec",
    "Oracle",
    "assign_split",
    "gate_mutation",
    "list_mutations",
    "list_tasks",
    "load_corpus",
    "load_mutation",
    "mine_corpus",
    "promote_mutation",
    "propose_mutation",
    "reject_mutation",
    "run_bench",
    "summarize",
    "wilson_lower_bound",
]
