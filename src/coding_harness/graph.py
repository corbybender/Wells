"""Wires the planner -> architect -> coder -> tester -> reviewer workflow.

Loop rule: when the reviewer is not satisfied, control runs the ``summarizer``
(condenses durable context for cheaper re-use) and then returns to ``coder``.
The loop is bounded by ``max_iterations`` (default 3) to avoid runaway runs.
"""

from langgraph.graph import END, START, StateGraph

from coding_harness.agents.architect import architect
from coding_harness.agents.coder import coder
from coding_harness.agents.planner import planner
from coding_harness.agents.reviewer import reviewer
from coding_harness.agents.tester import tester
from coding_harness.config import MAX_ITERATIONS
from coding_harness.state import AgentState
from coding_harness.summarize import summarizer_node


def _route_after_review(state: AgentState) -> str:
    """Conditional edge after the reviewer: end or loop back via summarizer."""
    if state.get("review_complete"):
        return "end"

    iteration = state.get("iteration", 0)
    cap = state.get("max_iterations", MAX_ITERATIONS)
    if iteration >= cap:
        print(f"[graph] reached max iterations ({cap}); stopping.")
        return "end"

    print(f"[graph] iteration {iteration} incomplete -> summarizer -> coder.")
    return "loop"


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("planner", planner)
    graph.add_node("architect", architect)
    graph.add_node("coder", coder)
    graph.add_node("tester", tester)
    graph.add_node("reviewer", reviewer)
    graph.add_node("summarizer", summarizer_node)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "architect")
    graph.add_edge("architect", "coder")
    graph.add_edge("coder", "tester")
    graph.add_edge("tester", "reviewer")
    # On INCOMPLETE: condense context, then iterate.
    graph.add_conditional_edges(
        "reviewer",
        _route_after_review,
        {"end": END, "loop": "summarizer"},
    )
    graph.add_edge("summarizer", "coder")

    return graph.compile()
