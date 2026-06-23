"""Entry point for the agentic coding harness.

Usage:
    coding-harness "Build a Payload CMS HTML-to-schema converter"
"""

import sys

from coding_harness.config import (
    MAX_ITERATIONS,
    SUMMARIZE_ON_LOOP,
    SUMMARIZE_THRESHOLD,
    ZAI_API_KEY,
    ZAI_ENDPOINT,
    ZAI_MODEL,
    ZAI_MODEL_CHEAP,
)
from coding_harness.graph import build_graph
from coding_harness.tokens import LEDGER


def _print_section(title: str, body: str) -> None:
    line = "=" * 70
    print(f"\n{line}\n{title}\n{line}\n{body or '(empty)'}")


def _print_final_summary(state: dict) -> None:
    _print_section("DEVELOPMENT PLAN", state.get("development_plan", ""))
    _print_section("ARCHITECTURE PROPOSAL", state.get("architecture", ""))
    _print_section("IMPLEMENTATION STEPS", state.get("implementation_steps", ""))
    _print_section("TEST PLAN", state.get("test_plan", ""))
    _print_section("REVIEW RESULT", state.get("review_result", ""))

    status = "COMPLETE" if state.get("review_complete") else "INCOMPLETE"
    summary = (
        f"Goal: {state['goal']}\n"
        f"Status: {status}\n"
        f"Iterations used: {state.get('iteration', 0)} / "
        f"{state.get('max_iterations', MAX_ITERATIONS)}\n"
        f"Model: {ZAI_MODEL} @ {ZAI_ENDPOINT}"
    )
    # TODO: turn the summary into a GitHub PR description / issue comment.
    _print_section("FINAL SUMMARY", summary)


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('Usage: coding-harness "<your development goal>"')
        sys.exit(1)

    goal = sys.argv[1].strip()

    if not ZAI_API_KEY:
        print("ERROR: ZAI_API_KEY is not set. Copy .env.example to .env and add your key.")
        sys.exit(1)

    LEDGER.reset()

    print(f"Model: {ZAI_MODEL} @ {ZAI_ENDPOINT}")
    if ZAI_MODEL_CHEAP:
        print(f"Cheap model (summarize/compress): {ZAI_MODEL_CHEAP}")
    print(f"Loop summarization: {'on (threshold ' + str(SUMMARIZE_THRESHOLD) + ' tok)' if SUMMARIZE_ON_LOOP else 'off'}")
    print(f"Max coder<->reviewer iterations: {MAX_ITERATIONS}")
    print(f"Goal: {goal}")
    print("-" * 70)

    app = build_graph()
    initial_state = {
        "goal": goal,
        "iteration": 0,
        "max_iterations": MAX_ITERATIONS,
        "messages": [],
    }

    final_state = app.invoke(initial_state)
    _print_final_summary(final_state)
    print("\n" + LEDGER.format_report())


if __name__ == "__main__":
    main()
