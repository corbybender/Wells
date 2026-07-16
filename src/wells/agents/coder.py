"""Coder: makes real edits to the workspace via the agentic executor.

In v1 the coder only *described* changes. Now it drives the tool-calling
executor (:mod:`wells.executor`) to actually read, edit, create files
and run lint/build inside the workspace — confined by the safety policy.

Behaviour by mode:
  * ``plan_mode`` on  -> the executor runs read-only and describes the edits it
    *would* make (mutating tools simulate). The returned summary is the plan.
  * otherwise         -> the executor applies edits, runs verification commands,
    and returns a summary of what changed + the verification result.

On loop iterations (>=2) the coder is seeded with the compressed review feedback
so it addresses the reviewer's specific concerns rather than starting over.
"""

import re

from wells import memory
from wells.executor import run_executor
from wells.tools import ToolContext


CODER_TASK_TEMPLATE = """You are implementing a pre-researched plan in the workspace at {workspace}.

GOAL:
{goal}

CONCRETE PLAN (produced by a planner that already read the codebase):
{context}

REVIEW FEEDBACK TO ADDRESS (if any):
{feedback}

{memory}
{repo_map}

The plan above already names the exact files, functions, and line numbers you need.
Follow it step by step:
1. Read ONLY the specific sections called out in the plan (use offset/limit — do not
   re-read whole files the planner already investigated).
2. Make the changes exactly as described.
3. Verify each change (re-read the edited section; run the verification step from the plan).
4. When done, reply with a concise summary of every file changed and the verification result.

Do not call tools further once you are done. Do not re-discover code the plan already identified.
"""


# ---------------------------------------------------------------------------
# Stepwise mode: one executor run per plan step, fresh context each time.
#
# The structural answer to small-context models drifting or silently
# truncating mid-run: instead of one long coder run whose context grows with
# every round, each numbered plan step gets its own short run_executor call
# carrying only the goal, the completed-steps ledger, and the current step.
# A model can't lose the thread across 20 rounds when no run lasts 20 rounds.
# ---------------------------------------------------------------------------

STEP_TASK_TEMPLATE = """You are executing ONE step of a larger plan in the workspace at {workspace}.

OVERALL GOAL (context only — do NOT attempt the whole goal now):
{goal}

STEPS ALREADY COMPLETED by previous runs (do not redo any of them):
{done}

YOUR CURRENT STEP — do exactly this and nothing more:
{step}

Make the change, verify it (re-read the edited section or run a quick check),
then reply with ONE short paragraph: what you changed and how you verified it.
"""

VERIFY_TASK_TEMPLATE = """All implementation steps of a plan are complete in the workspace at {workspace}.

OVERALL GOAL:
{goal}

STEPS COMPLETED:
{done}

Run the plan's verification now and report the result:
{verification}

Reply with the verification outcome and any failures found.
"""

_STEP_LINE_RE = re.compile(r"^\s*\d+[.)]\s+")


def _section(plan: str, heading: str) -> str:
    """Body of a ``## <heading>`` section, up to the next ``##`` (or EOF)."""
    m = re.search(
        rf"^##\s*{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", plan or "",
        re.M | re.S | re.I,
    )
    return m.group(1).strip() if m else ""


def _parse_plan_steps(plan: str) -> list[str]:
    """Numbered entries under the plan's '## Implementation steps' heading.

    Only that section is scanned — numbered lists elsewhere in the plan
    (risks, verification commands) must not be misread as steps. No heading,
    no stepwise: the ordinary single-run coder handles free-form plans.
    """
    body = _section(plan, "Implementation steps")
    if not body:
        return []
    steps: list[str] = []
    cur: list[str] = []
    for line in body.splitlines():
        if _STEP_LINE_RE.match(line):
            if cur:
                steps.append("\n".join(cur).strip())
            cur = [line.strip()]
        elif cur and line.strip():
            cur.append(line.strip())  # continuation of the current step
    if cur:
        steps.append("\n".join(cur).strip())
    return steps


def _stepwise_active() -> bool:
    from wells import config, providers

    mode = (getattr(config, "WELLS_STEPWISE", "auto") or "auto").strip().lower()
    if mode in ("0", "false", "no", "off"):
        return False
    if mode in ("1", "true", "yes", "on", "always"):
        return True
    prof = providers.load_profile(config.ACTIVE_PROFILE)
    return bool(prof and providers._looks_like_local_ollama(prof))


def _stepwise_coder(state: dict, ctx: ToolContext, steps: list[str], iteration: int) -> dict:
    goal = state.get("goal", "")
    done: list[str] = []
    reports: list[str] = []
    last_messages = None
    all_ok = True

    for i, step in enumerate(steps, 1):
        print(f"[coder] stepwise {i}/{len(steps)}: {step.splitlines()[0][:80]}")
        task = STEP_TASK_TEMPLATE.format(
            workspace=ctx.workspace,
            goal=goal,
            done="\n".join(done) or "(none yet — this is the first step)",
            step=step,
        )
        result = run_executor(task=task, ctx=ctx, step_label=f"coder-{iteration}.{i}")
        one_line = " ".join((result.summary or "").split())[:300]
        reports.append(f"Step {i} [{result.stopped_reason}]: {one_line}")
        last_messages = result.messages
        if result.stopped_reason in ("error", "stuck_loop", "budget", "cancelled"):
            all_ok = False
            reports.append(
                f"(aborted: step {i} did not complete cleanly; "
                f"{len(steps) - i} remaining step(s) were not attempted)"
            )
            break
        done.append(f"{i}. {step.splitlines()[0][:120]} -> {one_line[:160]}")

    verification = _section(state.get("development_plan", ""), "Verification")
    if all_ok and verification:
        vtask = VERIFY_TASK_TEMPLATE.format(
            workspace=ctx.workspace, goal=goal,
            done="\n".join(done), verification=verification,
        )
        vres = run_executor(task=vtask, ctx=ctx, step_label=f"coder-{iteration}.verify")
        reports.append(
            f"Verification [{vres.stopped_reason}]: "
            + " ".join((vres.summary or "").split())[:400]
        )
        last_messages = vres.messages

    combined = "\n".join(reports)
    print(f"[coder] stepwise done: {len(done)}/{len(steps)} steps completed")
    return {
        "implementation_steps": combined,
        "code_changes": combined,
        "iteration": iteration,
        "executor_messages": last_messages,
    }


def _build_context_block(state: dict) -> str:
    """Compact durable context: prefer the rolling summary on loop iterations."""
    iteration = state.get("iteration", 0)
    task_summary = state.get("task_summary", "")
    if iteration >= 1 and task_summary:
        return task_summary
    plan = state.get("development_plan", "")
    arch = state.get("architecture", "")
    parts = []
    if plan:
        parts.append(f"Development plan:\n{plan}")
    if arch:
        parts.append(f"Architecture:\n{arch}")
    return "\n\n".join(parts) or "(none)"


def coder(state: dict) -> dict:
    iteration = state.get("iteration", 0) + 1
    print(f"[coder] iteration {iteration} - driving executor to implement the goal ...")

    ctx = ToolContext.from_state(state)

    # Stepwise mode: first iteration only — review-loop iterations carry
    # targeted feedback that doesn't decompose into the original plan steps.
    if iteration == 1:
        plan_steps = _parse_plan_steps(state.get("development_plan", ""))
        if len(plan_steps) >= 2 and _stepwise_active():
            print(f"[coder] stepwise mode: {len(plan_steps)} plan steps, "
                  f"fresh context per step.")
            return _stepwise_coder(state, ctx, plan_steps, iteration)

    feedback = state.get("review_result", "") or "(none)"
    mem_slice = memory.load(ctx.workspace).section_for_context(max_chars=2000)
    mem_block = (
        f"PROJECT MEMORY (AGENTS.md — established facts about this repo):\n{mem_slice}"
        if mem_slice
        else ""
    )
    from wells.repomap import repo_map_block

    task = CODER_TASK_TEMPLATE.format(
        workspace=ctx.workspace,
        goal=state.get("goal", ""),
        context=_build_context_block(state),
        feedback=feedback,
        memory=mem_block,
        repo_map=repo_map_block(ctx.workspace, goal=state.get("goal", "")),
    )

    result = run_executor(
        task=task,
        ctx=ctx,
        step_label=f"coder-{iteration}",
        seed_messages=list(state.get("executor_messages") or []) or None,
    )

    print(
        f"[coder] executor done: {result.steps_taken} steps, reason={result.stopped_reason}"
    )

    return {
        "implementation_steps": result.summary,
        "code_changes": result.summary,
        "iteration": iteration,
        "executor_messages": result.messages,
    }
