"""Architect: proposes an architecture/design for the planned work."""

from coding_harness.runtime import run_step
from coding_harness.state import AgentState

ARCHITECT_SYSTEM = """You are a senior software architect.
Propose a concrete architecture/design for the work below.
Cover:
- Components / modules and their responsibilities
- Key data models / schemas
- External dependencies and libraries
- Risk areas / trade-offs"""


def architect(state: AgentState) -> dict:
    print("[architect] proposing architecture ...")

    chunks = {
        "user_request": f"Goal:\n{state['goal']}",
        "retrieved_code": f"Development plan:\n{state.get('development_plan', '')}",
    }
    architecture, _ = run_step(
        step="architect",
        task_type="architecture",
        system=ARCHITECT_SYSTEM,
        chunks=chunks,
    )

    # TODO: write an ARCHITECTURE.md / design doc to the target repo.
    return {"architecture": architecture}
