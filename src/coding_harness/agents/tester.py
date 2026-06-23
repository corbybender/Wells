"""Tester: designs a test plan for the current implementation steps."""

from coding_harness.runtime import run_step
from coding_harness.state import AgentState

TESTER_SYSTEM = """You are a senior QA / test engineer.
Design a test plan that validates the implementation below against the goal.
Cover:
- Unit tests (what to assert)
- Integration tests
- Manual / smoke checks
- Edge cases"""


def tester(state: AgentState) -> dict:
    print("[tester] designing test plan ...")

    chunks = {
        "user_request": f"Goal:\n{state['goal']}",
        "recent_conversation": f"Implementation steps:\n{state.get('implementation_steps', '')}",
    }
    test_plan, _ = run_step(
        step="tester",
        task_type="testing",
        system=TESTER_SYSTEM,
        chunks=chunks,
    )

    # TODO: generate actual test files and execute the test suite.
    # TODO: feed pass/fail results into the reviewer via state.
    return {"test_plan": test_plan}
