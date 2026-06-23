"""Planner: turns the raw goal into a structured development plan."""

from coding_harness.runtime import run_step
from coding_harness.state import AgentState

PLANNER_SYSTEM = """You are a senior software planner.
Break the given development goal into a clear, actionable development plan.
Produce a concise plan with:
- Project objectives
- Phases / milestones
- Key deliverables
- Out-of-scope items
- Success criteria"""


def planner(state: AgentState) -> dict:
    print("[planner] drafting development plan ...")

    chunks = {"user_request": f"Goal:\n{state['goal']}"}
    plan, _ = run_step(
        step="planner",
        task_type="planning",
        system=PLANNER_SYSTEM,
        chunks=chunks,
    )

    # TODO: persist the plan to a work-item store or markdown file on disk.
    return {"development_plan": plan}
