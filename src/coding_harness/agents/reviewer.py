"""Reviewer: judges whether the work is complete; emits a COMPLETE/INCOMPLETE decision."""

from coding_harness.runtime import run_step
from coding_harness.state import AgentState

REVIEWER_SYSTEM = """You are a strict senior code reviewer.
Decide whether the proposed work is complete and correct enough to satisfy the goal.
Respond in EXACTLY this format:
DECISION: COMPLETE
or
DECISION: INCOMPLETE
on the first line (use INCOMPLETE if more coding work is required), then a blank
line, then your detailed review with specific, actionable feedback."""


def _parse_decision(text: str) -> bool:
    """Return True if the reviewer marked the work COMPLETE."""
    first_line = text.strip().splitlines()[0].upper() if text.strip() else ""
    if "INCOMPLETE" in first_line:
        return False
    if "COMPLETE" in first_line:
        return True
    return False


def reviewer(state: AgentState) -> dict:
    print("[reviewer] reviewing work ...")

    plan = state.get("development_plan", "")
    architecture = state.get("architecture", "")
    retrieved = f"Development plan:\n{plan}\n\nArchitecture:\n{architecture}".strip()

    chunks = {"user_request": f"Goal:\n{state['goal']}"}
    if retrieved:
        chunks["retrieved_code"] = retrieved
    if state.get("implementation_steps"):
        chunks["recent_conversation"] = f"Implementation steps:\n{state['implementation_steps']}"
    if state.get("test_plan"):
        chunks["tool_outputs"] = f"Test plan:\n{state['test_plan']}"

    text, _ = run_step(
        step="reviewer",
        task_type="review",
        system=REVIEWER_SYSTEM,
        chunks=chunks,
    )
    complete = _parse_decision(text)

    print(f"[reviewer] decision: {'COMPLETE' if complete else 'INCOMPLETE'}")

    # TODO: run automated checks (tests/lint) before deciding completeness.
    return {"review_result": text, "review_complete": complete}
