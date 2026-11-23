"""Wires the planner -> architect -> coder -> tester -> reviewer workflow.

Loop rule: when the reviewer is not satisfied, control runs the ``summarizer``
(condenses durable context for cheaper re-use) and then returns to ``coder``.
The loop is bounded by ``max_iterations`` (default 3) to avoid runaway runs.

After the loop ends (COMPLETE, or the cap was hit), a ``finisher`` node runs to
write the project-memory lesson and (optionally) create a git branch/commit/PR.
"""

from langgraph.graph import END, START, StateGraph

from wells.agents.architect import architect
from wells.agents.coder import coder
from wells.agents.planner import planner
from wells.agents.reviewer import reviewer
from wells.agents.tester import tester
from wells.config import MAX_ITERATIONS, INDEX_AUTO_UPDATE, llm_unavailable
from wells.control import CONTROL, ui
from wells.finisher import finisher
from wells.state import AgentState
from wells.summarize import summarizer_node
from wells.tools import ToolContext


def _with_stage(name: str, fn):
    """Wrap a graph node so the TUI's InfoPanel breadcrumb tracks it.

    Calls ``CONTROL.stage_start`` on entry and ``stage_end`` on exit, so the
    pipeline timeline (indexer → planner → …) shows live per-node elapsed
    time instead of appearing only when an agent happens to set it. Routing
    output goes through the event bus (``graph`` kind) with a print fallback
    for plain-CLI runs — previously these were bare ``print()`` calls that
    reached the TUI unstyled via the stdout-capture shim.
    """

    def wrapped(state: AgentState) -> AgentState:
        CONTROL.stage_start(name)
        try:
            return fn(state)
        except Exception:
            CONTROL.stage_end(name, ok=False)
            raise
        else:
            CONTROL.stage_end(name, ok=True)

    return wrapped


def indexer_node(state: AgentState) -> AgentState:
    """Build or update the structural repository index (if available).

    Runs transparently before planning. Sets index_ready=True when complete.
    If wells-index is not installed or INDEX_AUTO_UPDATE is disabled, this is a no-op.
    """
    # Import here to avoid circular dependency and late-bind the availability check
    from wells import index_tools

    if not INDEX_AUTO_UPDATE or not index_tools.INDEXER_AVAILABLE:
        return {"index_ready": False}

    try:
        ctx = ToolContext.from_state(state)
        result = index_tools.index_workspace(ctx)
        return {"index_ready": result.ok}
    except Exception:
        # If indexing fails, continue anyway (graceful degradation)
        return {"index_ready": False}


def _route_after_plan(state: AgentState) -> str:
    """Conditional edge after the planner: skip the architect for simple tasks.

    The planner labels its own plan SIMPLE/COMPLEX. Simple plans (localized
    edits, no new modules/APIs) go straight to the coder — the architect pass
    would only restate them. Unknown/missing labels default to complex.
    """
    if state.get("plan_complexity") == "simple":
        ui("graph", "[dim]simple plan -> coder (architect skipped).[/dim]")
        return "code"
    return "design"


def _route_after_tests(state: AgentState) -> str:
    """Conditional edge after the tester: fail-fast or auto-approve.

    Deterministic failure → loop straight back to the coder (reviewer skipped).
    Simple plan + deterministic green (tester set review_complete) → straight
    to the finisher: nothing left for the reviewer to judge. Otherwise the
    reviewer decides as usual.
    """
    if state.get("tests_passed") is True and state.get("review_complete"):
        ui("graph", "[dim]auto-approved (simple + green) -> finisher (reviewer skipped).[/dim]")
        return "finalize"
    if state.get("tests_passed") is False:
        if llm_unavailable():
            # The tests failed because nothing could be written: the provider
            # is out of quota. Looping re-runs the suite against the same
            # untouched tree — observed live as nine iterations over ten
            # minutes, every model call failing before it started.
            ui("graph", "[dim]LLM unavailable (provider quota) -> finalizing "
                         "instead of looping.[/dim]")
            return "finalize"
        iteration = state.get("iteration", 0)
        cap = state.get("max_iterations", MAX_ITERATIONS)
        if cap == 0 or iteration < cap:  # cap 0 = no limit
            ui("graph", f"[dim]tests failing (iteration {iteration}) -> summarizer -> coder "
                         "(reviewer skipped).[/dim]")
            return "loop"
    return "review"


def _route_after_review(state: AgentState) -> str:
    """Conditional edge after the reviewer: loop or finalize."""
    if state.get("review_complete"):
        return "finalize"

    if state.get("review_error"):
        # The reviewer's own LLM call failed — this isn't feedback, it's an
        # infra failure. Looping the coder against it (possibly forever,
        # with max_iterations=0) burns cycles "fixing" a review that never
        # happened. Stop now instead of pretending this was a real verdict.
        ui("graph", "[dim]reviewer could not run (LLM/tool error) -> "
                     "finalizing instead of looping on a broken review.[/dim]")
        return "finalize"

    if llm_unavailable():
        ui("graph", "[dim]LLM unavailable (provider quota) -> finalizing "
                     "instead of looping.[/dim]")
        return "finalize"

    iteration = state.get("iteration", 0)
    cap = state.get("max_iterations", MAX_ITERATIONS)
    if cap and iteration >= cap:  # cap 0 = no limit
        ui("graph", f"[dim]reached max iterations ({cap}); finalizing.[/dim]")
        return "finalize"

    ui("graph", f"[dim]iteration {iteration} incomplete -> summarizer -> coder.[/dim]")
    return "loop"


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("indexer", _with_stage("indexer", indexer_node))
    graph.add_node("planner", _with_stage("planner", planner))
    graph.add_node("architect", _with_stage("architect", architect))
    graph.add_node("coder", _with_stage("coder", coder))
    graph.add_node("tester", _with_stage("tester", tester))
    graph.add_node("reviewer", _with_stage("reviewer", reviewer))
    graph.add_node("summarizer", _with_stage("summarizer", summarizer_node))
    graph.add_node("finisher", _with_stage("finisher", finisher))

    graph.add_edge(START, "indexer")
    graph.add_edge("indexer", "planner")
    # Simple plans skip the architect (it would only restate them).
    graph.add_conditional_edges(
        "planner",
        _route_after_plan,
        {"design": "architect", "code": "coder"},
    )
    graph.add_edge("architect", "coder")
    graph.add_edge("coder", "tester")
    # Deterministic test failures loop straight back; simple + green suite
    # auto-approves straight to the finisher (reviewer skipped both ways).
    graph.add_conditional_edges(
        "tester",
        _route_after_tests,
        {"review": "reviewer", "loop": "summarizer", "finalize": "finisher"},
    )
    # On INCOMPLETE: condense context, then iterate.
    graph.add_conditional_edges(
        "reviewer",
        _route_after_review,
        {"finalize": "finisher", "loop": "summarizer"},
    )
    graph.add_edge("summarizer", "coder")
    graph.add_edge("finisher", END)

    return graph.compile()
