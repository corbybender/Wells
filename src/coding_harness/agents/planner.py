"""Planner: turns the raw goal into a structured development plan."""

from coding_harness import memory
from coding_harness.runtime import run_step

PLANNER_SYSTEM = """You are a senior software planner.
Break the given development goal into a clear, actionable development plan.
Produce a concise plan with:
- Project objectives
- Phases / milestones
- Key deliverables
- Out-of-scope items
- Success criteria

If PROJECT MEMORY is provided, use it to ground the plan in this repo's known
files, conventions, and prior gotchas — do not contradict established facts."""


def planner(state: dict) -> dict:
    print("[planner] drafting development plan ...")

    goal = memory.inject_into_prompt(
        f"Goal:\n{state.get('goal', '')}", state.get("workspace_root")
    )
    chunks = {"user_request": goal}
    plan, _ = run_step(
        step="planner",
        task_type="planning",
        system=PLANNER_SYSTEM,
        chunks=chunks,
        workspace=state.get("workspace_root"),
    )
    return {"development_plan": plan}
