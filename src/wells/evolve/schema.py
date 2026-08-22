"""Task bundle schema for the bench/evolution subsystem (SEACS SPEC-009 §3.1).

A *task bundle* is the unit the bench runner executes and the deterministic
oracle scores: one historical bug fix, expressed as

  * ``base_commit`` — the repo state where the bug is present and the
    target tests fail,
  * ``problem_statement`` — what the agent is told (the fix commit's
    message; the agent never sees the diff itself),
  * ``test_oracle`` — the deterministic ground truth: ``fail_to_pass``
    test node ids that must pass after the run, ``pass_to_pass`` ids that
    must keep passing, and the command that runs them.

Everything is JSON-serializable and stable on disk under
``<workspace>/.wells/bench/corpus/tasks/<task_id>.json`` so a corpus is
versionable with the repo it was mined from, and two different harness
versions can be compared over byte-identical task sets.

Validation is deliberately strict at the boundaries (``validate()`` raises
``SchemaError``) — a malformed task silently admitted to a corpus poisons
every benchmark computed from it afterwards.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Commit subjects that never make useful problem statements — machinery
# commits whose "bug" isn't a bug, or noise commits with no describable
# failure at all.
_SKIP_SUBJECT_RE = re.compile(
    r"^(merge |revert |bump |chore(\([^)]*\))?:|initial commit|wip\b|misc\b)",
    re.IGNORECASE,
)

TASK_SCHEMA_VERSION = 1


class SchemaError(ValueError):
    """Raised when a task bundle fails validation."""


@dataclass
class Oracle:
    """Deterministic ground truth for one task (SWE-bench style).

    ``test_files`` are the test files the fix commit changed — at scoring
    time the runner applies exactly these from ``source_commit`` onto the
    agent's worktree before running the tests (the "test patch"): the agent
    never sees the target tests while working, and its own edits to them
    are overwritten by the authoritative versions. Tasks without a source
    commit (hand-authored) may leave this empty and manage test state
    themselves.
    """

    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    command: str = ""  # empty = autodetect at run time (tools._autodetect_test_command)
    test_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Oracle":
        return cls(
            fail_to_pass=list(d.get("fail_to_pass") or []),
            pass_to_pass=list(d.get("pass_to_pass") or []),
            command=str(d.get("command") or ""),
            test_files=list(d.get("test_files") or []),
        )


@dataclass
class TaskSpec:
    """One mined, (optionally) verified task bundle."""

    task_id: str
    repo_root: str  # absolute path of the repo it was mined from (local corpora)
    base_commit: str
    problem_statement: str
    test_oracle: Oracle = field(default_factory=Oracle)
    environment_setup: list[str] = field(default_factory=list)
    source_commit: str = ""  # the fix commit the task was derived from
    split: str = "train"  # train | val | blind — deterministic, assigned at mining
    status: str = "verified"  # verified | unverified | rejected
    schema_version: int = TASK_SCHEMA_VERSION
    mined_at: str = ""

    # ------------------------------------------------------------------ IO

    def to_dict(self) -> dict:
        d = asdict(self)
        d["test_oracle"] = self.test_oracle.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TaskSpec":
        oracle = d.get("test_oracle") or {}
        return cls(
            task_id=str(d["task_id"]),
            repo_root=str(d.get("repo_root") or ""),
            base_commit=str(d.get("base_commit") or ""),
            problem_statement=str(d.get("problem_statement") or ""),
            test_oracle=Oracle.from_dict(oracle) if oracle else Oracle(),
            environment_setup=list(d.get("environment_setup") or []),
            source_commit=str(d.get("source_commit") or ""),
            split=str(d.get("split") or "train"),
            status=str(d.get("status") or "unverified"),
            schema_version=int(d.get("schema_version") or TASK_SCHEMA_VERSION),
            mined_at=str(d.get("mined_at") or ""),
        )

    def save(self, tasks_dir: Path) -> Path:
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / f"{self.task_id}.json"
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "TaskSpec":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # ----------------------------------------------------------- validation

    def validate(self) -> None:
        """Raise SchemaError on any malformed field.

        Called on every load and save — a corpus file edited by hand (or a
        mining bug) must fail loudly at the boundary, not surface as a
        mysterious benchmark number later.
        """
        problems: list[str] = []
        if not self.task_id or not re.fullmatch(r"[A-Za-z0-9._\-]+", self.task_id):
            problems.append(f"task_id {self.task_id!r} missing/has unsafe characters")
        if not re.fullmatch(r"[0-9a-f]{7,40}", self.base_commit):
            problems.append(f"base_commit {self.base_commit!r} is not a sha")
        if self.source_commit and not re.fullmatch(
            r"[0-9a-f]{7,40}", self.source_commit
        ):
            problems.append(f"source_commit {self.source_commit!r} is not a sha")
        if not self.problem_statement.strip():
            problems.append("problem_statement is empty")
        if not self.test_oracle.fail_to_pass:
            problems.append("oracle has no fail_to_pass tests — nothing to score")
        if self.split not in ("train", "val", "blind"):
            problems.append(f"split {self.split!r} not in train|val|blind")
        if self.status not in ("verified", "unverified", "rejected"):
            problems.append(f"status {self.status!r} not recognized")
        for tid in list(self.test_oracle.fail_to_pass) + list(
            self.test_oracle.pass_to_pass
        ):
            if not tid.strip() or "\n" in tid or '"' in tid:
                problems.append(f"oracle test id {tid!r} is blank/multiline/quoted")
        if problems:
            raise SchemaError(f"task {self.task_id}: " + "; ".join(problems))


def subject_is_mineable(subject: str) -> bool:
    """Quick pre-filter on commit subjects before the expensive dual check."""
    s = subject.strip()
    return bool(s) and not _SKIP_SUBJECT_RE.match(s)
