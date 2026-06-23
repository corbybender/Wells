Build a local Python LangGraph-based agentic coding harness.

Goal:
Create a working project that lets me define a software development goal, then runs an orchestration loop with planner, architect, coder, tester, and reviewer stages. The harness should use my Z.ai API key through LangChain’s OpenAI-compatible ChatOpenAI client.

Environment:

* Python project managed by uv
* LangGraph
* LangChain
* Z.ai API using OpenAI-compatible endpoint
* Base URL: https://api.z.ai/api/paas/v4/
* Model should be configurable, default to glm-5.2
* API key loaded from .env as ZAI_API_KEY

Build requirements:

1. Create a clean project structure.

2. Add a .env.example file.

3. Add a README.md with setup and run instructions.

4. Add a src/main.py entrypoint.

5. Add a src/config.py file for loading environment variables.

6. Add a src/state.py file defining the LangGraph state.

7. Add a src/agents/ directory with:

   * planner.py
   * architect.py
   * coder.py
   * tester.py
   * reviewer.py

8. Add a src/graph.py file that wires the LangGraph workflow together.

9. The app should accept a goal from the command line, for example:

   uv run python src/main.py "Build a Payload CMS HTML-to-schema converter"

10. The workflow should be:

START
→ planner
→ architect
→ coder
→ tester
→ reviewer
→ decision

If reviewer says complete, end.
If reviewer says not complete, loop back to coder.
Limit loops to 3 iterations.

11. The first version does not need to actually edit files yet. It should produce:

* development plan
* architecture proposal
* implementation steps
* test plan
* review result
* final summary

12. Add clear TODOs where real file editing, shell execution, OpenHands integration, and GitHub PR creation would be added later.

13. Use typed state with TypedDict.

14. Keep code simple and readable.

15. Make the harness runnable immediately after install.

Expected commands after completion:

uv sync
cp .env.example .env

# user adds ZAI_API_KEY

uv run python src/main.py "Build a Payload CMS HTML-to-schema converter"

Deliverables:

* Working code
* README.md
* .env.example
* No placeholder imports that break runtime
* No missing files
* No unexplained setup steps

After building, run the app once with a sample goal and fix any errors.
