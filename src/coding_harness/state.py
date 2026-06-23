"""Typed LangGraph state shared by every agent node."""

from typing import TypedDict


class AgentState(TypedDict, total=False):
    goal: str
    iteration: int
    max_iterations: int

    development_plan: str
    architecture: str
    implementation_steps: str
    code_changes: str
    test_plan: str
    review_result: str
    review_complete: bool

    # Token optimization: rolling task-state summary used on loop iterations.
    task_summary: str

    summary: str
    messages: list[str]
