"""Coder: produces concrete implementation steps (and, later, real edits).

In v1 the coder does NOT touch files; it describes the changes to make.
Each run increments ``iteration`` so the graph can enforce the loop cap.

Token optimization:
  * On loop iterations (>=2) it sends a compact task summary instead of re-sending
    the full plan + architecture verbatim, and reports the tokens that saved.
  * The latest review feedback (the actionable signal) is compressed and sent
    verbatim under ``tool_outputs`` -- never summarized away.
"""

from coding_harness.compress import compress_output
from coding_harness.runtime import run_step
from coding_harness.state import AgentState
from coding_harness.tokens import estimate_tokens

CODER_SYSTEM = """You are a senior software engineer.
Given the goal, plan, and architecture, produce precise, ordered implementation steps.
Describe exactly which files to create/modify and what each change does.
Output:
1. An ordered list of implementation steps (file path -> change).
2. A short "code_changes" section with representative snippets/pseudocode."""


def coder(state: AgentState) -> dict:
    iteration = state.get("iteration", 0) + 1
    print(f"[coder] iteration {iteration} - producing implementation steps ...")

    plan = state.get("development_plan", "")
    architecture = state.get("architecture", "")
    task_summary = state.get("task_summary", "")
    durable_tokens = estimate_tokens(plan) + estimate_tokens(architecture)

    chunks = {"user_request": f"Goal:\n{state['goal']}"}
    saved_by_summary = 0

    if iteration > 1 and task_summary:
        # Reuse the (verbatim-or-condensed) summary instead of the full plan/arch.
        chunks["task_state_summary"] = task_summary
        saved_by_summary = max(0, durable_tokens - estimate_tokens(task_summary))
    elif plan or architecture:
        # First iteration: send the durable design context verbatim.
        chunks["retrieved_code"] = f"Development plan:\n{plan}\n\nArchitecture:\n{architecture}"

    review = state.get("review_result", "")
    if review:
        chunks["tool_outputs"] = f"Previous review feedback to address:\n{compress_output(review)}"

    text, _ = run_step(
        step="coder",
        task_type="coding",
        system=CODER_SYSTEM,
        chunks=chunks,
        saved_by_summary=saved_by_summary,
    )

    # TODO: replace this text output with real file edits using an
    # OpenHands / tool-calling integration (create/edit/write operations).
    # TODO: optionally run shell commands (lint/build) and feed results back.
    return {
        "implementation_steps": text,
        "code_changes": text,
        "iteration": iteration,
    }
