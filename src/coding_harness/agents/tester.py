"""Tester: runs the real test suite and interprets results.

In v1 the tester only *described* a test plan. Now it runs the actual test
command via the executor (which runs ``run_tests`` / ``run_command`` under the
safety policy) and produces a structured pass/fail report the reviewer consumes.

The tester runs with read+exec tools but NOT write tools, so it cannot alter
source — it only runs commands and inspects output.
"""

from coding_harness.executor import run_executor
from coding_harness.tools import EXEC_TOOLS, ToolContext, registry


TESTER_TASK_TEMPLATE = """Run the project's tests for the workspace at {workspace} and report the result.

GOAL (for context):
{goal}

RECENTLY MADE CHANGES (coder summary):
{changes}

STEPS:
1. Inspect the repo layout (list_dir / read_file on the test config) to confirm
   the correct test command, unless one is obvious.
2. Run the test suite (run_tests, or run_command with the specific command).
3. If tests fail, read the failing test + the relevant source to characterize the
   failure precisely. Do NOT fix anything — that is the coder's job.
4. Reply with a concise report: PASS/FAIL, the command run, a one-line summary,
   and (if failing) the specific failing assertions with file:line references.
"""


def tester(state: dict) -> dict:
    print("[tester] running the test suite via executor ...")
    ctx = ToolContext.from_state(state)
    # Read+exec only — the tester must not edit source.
    toolset = registry(include_mutating=False) + [
        t for t in EXEC_TOOLS if t.name in ("run_tests", "run_command")
    ]

    task = TESTER_TASK_TEMPLATE.format(
        workspace=ctx.workspace,
        goal=state.get("goal", ""),
        changes=state.get("implementation_steps", "") or "(none)",
    )

    result = run_executor(
        task=task,
        ctx=ctx,
        toolset=toolset,
        max_steps=8,
        step_label="tester",
        temperature=0.0,
    )

    print(f"[tester] done: {result.steps_taken} steps, reason={result.stopped_reason}")
    return {
        "test_plan": result.summary,  # keep the field name for backward compat
        "test_results": result.summary,
    }
