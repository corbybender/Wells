"""Agentic tool-calling executor loop (Layer 2).

This is the component that makes the harness *act*: given a task and a toolset,
it runs a model-driven loop of ``model → tool_calls → observe → model`` until
the task is done or the step cap is reached. It reuses the harness's token
accounting (:data:`LEDGER`) and the compressor for tool outputs.

Two calling conventions are supported so *any decent AI* can drive it:

  * **Native tool-calling** — models that support OpenAI/Anthropic-style
    ``tool_calls`` (Z.ai GLM, OpenAI, Claude, Gemini, …). We bind the tool
    schemas via ``model.bind_tools([...])`` and dispatch the returned calls.
  * **Text fallback** — for models without native tool-calling, the same tool
    schemas are described in the system prompt and the model emits calls as
    ``<tool_call>{json}</tool_call>`` blocks, which we parse and dispatch.

The loop auto-detects which mode each response uses, so a single harness run
works across providers without per-model wiring.

The executor is deliberately *not* a LangGraph node — it is a plain callable so
the coder/tester nodes (and subagents) can invoke it and feed its summary back
into the shared :class:`AgentState`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from coding_harness import config, tools
from coding_harness.compress import compress_output
from coding_harness.tokens import LEDGER, estimate_tokens


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ExecutorResult:
    """Outcome of one executor run."""

    summary: str  # final natural-language answer from the model
    steps_taken: int = 0  # number of tool-call rounds executed
    tool_calls: list[dict] = field(
        default_factory=list
    )  # [{name, args, ok, output_preview}]
    stopped_reason: str = "done"  # done | max_steps | error
    messages: list[BaseMessage] = field(default_factory=list)


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------


def _tool_catalog(toolset: list[tools.ToolDef]) -> str:
    """Human-readable tool catalog for the text-fallback system prompt."""
    lines = []
    for t in toolset:
        params = ", ".join(
            f"{k}: {v.get('type', 'string')}"
            for k, v in t.input_schema.get("properties", {}).items()
        )
        req = ", ".join(t.input_schema.get("required", [])) or "—"
        lines.append(f"- {t.name}({params}) [required: {req}]\n    {t.description}")
    return "\n".join(lines)


def _system_prompt(task: str, toolset: list[tools.ToolDef], *, plan_mode: bool) -> str:
    catalog = _tool_catalog(toolset)
    plan_note = (
        "\n\nIMPORTANT: You are in PLAN MODE. Do NOT make changes. Use read-only tools "
        "(read_file, list_dir, glob, grep) to investigate, then describe exactly what "
        "changes you WOULD make. Write/edit/run tools will simulate."
        if plan_mode
        else ""
    )
    return f"""You are an autonomous software engineering agent working inside a real code repository.
You operate by calling tools to read files, search code, make edits, and run commands/tests,
then observing the results, until the task is complete.

TASK:
{task}

AVAILABLE TOOLS:
{catalog}
{plan_note}

WORKING RULES:
1. Investigate before acting: read/list/grep to understand the relevant code first.
2. Make focused changes; after each edit, verify (re-read, run tests/lint) before continuing.
3. When you have finished the task and verified your work, stop calling tools and reply
   with a concise summary of what you changed and the verification result.
4. If you cannot complete the task after reasonable effort, stop and explain what blocked you.

TOOL CALLING:
- If your runtime exposes native tool/function calls, use them.
- Otherwise, emit each call on its own line as: <tool_call>{{"name": "...", "args": {{...}}}}</tool_call>
  and nothing else on that line. The harness will execute it and reply with the result.
"""


# ---------------------------------------------------------------------------
# Tool-call parsing (text fallback)
# ---------------------------------------------------------------------------

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def parse_text_tool_calls(text: str) -> list[dict]:
    """Parse ``<tool_call>{...}</tool_call>`` blocks from model text.

    Returns a list of ``{"name": ..., "args": {...}}`` dicts. Malformed blocks
    are skipped (the model will be told the parse error in the observation).
    """
    calls: list[dict] = []
    for m in _TOOL_CALL_RE.finditer(text):
        blob = m.group(1).strip()
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            calls.append({"_parse_error": blob})
            continue
        name = obj.get("name")
        args = obj.get("args") or obj.get("arguments") or {}
        if name:
            calls.append({"name": name, "args": args})
    return calls


# ---------------------------------------------------------------------------
# Token accounting helper
# ---------------------------------------------------------------------------


def _account_usage(
    *, step: str, model: str, messages: list[BaseMessage], resp: BaseMessage
) -> None:
    """Record token usage for one executor round into the global ledger."""
    um = getattr(resp, "usage_metadata", None) or {}
    full = "\n".join(getattr(m, "content", "") or "" for m in messages)
    input_tokens = um.get("input_tokens") or estimate_tokens(full)
    output_tokens = um.get("output_tokens") or estimate_tokens(
        getattr(resp, "content", "") or ""
    )
    reasoning = ((um.get("output_token_details") or {}).get("reasoning")) or 0
    cache_read = ((um.get("input_token_details") or {}).get("cache_read")) or 0
    LEDGER.record(
        step=step,
        task_type="executor",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        cache_read_tokens=cache_read,
        category_tokens={"executor_input": input_tokens},
    )


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def run_executor(
    *,
    task: str,
    ctx: tools.ToolContext,
    toolset: list[tools.ToolDef] | None = None,
    max_steps: int | None = None,
    profile: str | None = None,
    temperature: float = 0.2,
    system_prefix: str = "",
    seed_messages: list[BaseMessage] | None = None,
    step_label: str = "executor",
) -> ExecutorResult:
    """Run the agentic tool-calling loop until completion or the step cap.

    Parameters
    ----------
    task : str
        Natural-language task for the executor (the "what to do").
    ctx : ToolContext
        Workspace/safety/plan-mode context threaded into every tool call.
    toolset : list[ToolDef], optional
        Tools the model may call. Defaults to all tools (read+write+exec); pass
        ``tools.registry(include_mutating=False)`` for read-only subagents.
    max_steps : int, optional
        Cap on tool-call rounds. Defaults to ``config.MAX_TOOL_STEPS``.
    profile : str, optional
        Provider profile to use. Defaults to the active profile.
    """
    toolset = toolset if toolset is not None else tools.ALL_TOOLS
    cap = max_steps if max_steps is not None else config.MAX_TOOL_STEPS
    profile = profile or config.ACTIVE_PROFILE

    system = _system_prompt(task, toolset, plan_mode=ctx.plan_mode)
    if system_prefix:
        system = f"{system_prefix}\n\n{system}"

    messages: list[BaseMessage] = [SystemMessage(content=system)]
    if seed_messages:
        messages.extend(seed_messages)
    else:
        messages.append(HumanMessage(content=f"Please complete this task:\n{task}"))

    # Try to bind native tool schemas; fall back to text mode if unsupported.
    llm = (
        config.get_llm_for_task("coding", temperature=temperature)
        if profile == config.ACTIVE_PROFILE
        else config.providers.get_chat_model(profile, temperature=temperature)
    )
    bound = _try_bind_tools(llm, toolset)
    native_tools = bound is not None
    if native_tools:
        llm = bound

    model_label = config.providers.load_profile(profile)
    model_name = model_label.label() if model_label else profile

    history: list[dict] = []
    steps = 0
    while steps < cap:
        try:
            resp = config._invoke_with_retry(llm, messages)
        except Exception as e:
            return ExecutorResult(
                summary=f"[executor error: {type(e).__name__}: {e}]",
                steps_taken=steps,
                tool_calls=history,
                stopped_reason="error",
                messages=messages,
            )
        _account_usage(step=step_label, model=model_name, messages=messages, resp=resp)
        messages.append(resp)

        # Extract tool calls (native first, then text fallback).
        calls = _extract_calls(resp, native_tools=native_tools)

        if not calls:
            # No tool calls -> the model produced a final answer.
            return ExecutorResult(
                summary=(getattr(resp, "content", "") or "").strip() or "(no output)",
                steps_taken=steps,
                tool_calls=history,
                stopped_reason="done",
                messages=messages,
            )

        # Execute each call and append observations.
        for call in calls:
            steps += 1
            name = call.get("name")
            args = call.get("args") or {}
            if not name:
                obs_text = "[error: malformed tool call — missing name]"
                history.append(
                    {"name": "?", "args": args, "ok": False, "output_preview": obs_text}
                )
                messages.append(
                    _tool_message(obs_text, tool_call_id="parseerr", name="parse_error")
                )
                continue
            result = tools.dispatch(name, args, ctx)
            obs_text = result.to_model_text()
            history.append(
                {
                    "name": name,
                    "args": args,
                    "ok": result.ok,
                    "output_preview": (result.output or result.error)[:200],
                    "simulated": result.simulated,
                }
            )
            print(
                f"[executor] {name}({list(args.keys())}) -> {'ok' if result.ok else 'fail'}"
                + (" [simulated]" if result.simulated else "")
            )
            # Native tool calls need a matching tool_call_id on the ToolMessage.
            tcid = call.get("id") or f"call_{steps}"
            messages.append(
                _tool_message(obs_text, tool_call_id=tcid, name=name, ai_message=resp)
            )

        if steps >= cap:
            # One final round to let the model summarize after hitting the cap.
            try:
                final = config._invoke_with_retry(llm, messages)
                _account_usage(
                    step=step_label, model=model_name, messages=messages, resp=final
                )
                messages.append(final)
                return ExecutorResult(
                    summary=(getattr(final, "content", "") or "").strip()
                    or "(reached step cap)",
                    steps_taken=steps,
                    tool_calls=history,
                    stopped_reason="max_steps",
                    messages=messages,
                )
            except Exception:
                return ExecutorResult(
                    summary="(reached step cap)",
                    steps_taken=steps,
                    tool_calls=history,
                    stopped_reason="max_steps",
                    messages=messages,
                )

    return ExecutorResult(
        summary="(reached step cap)",
        steps_taken=steps,
        tool_calls=history,
        stopped_reason="max_steps",
        messages=messages,
    )


# ---------------------------------------------------------------------------
# Internals: tool binding + call extraction + tool message
# ---------------------------------------------------------------------------


def _try_bind_tools(llm, toolset: list[tools.ToolDef]):
    """Try to bind native tool schemas. Returns the bound model or None."""
    try:
        schemas = tools.langchain_tool_schemas(toolset)
        return llm.bind_tools(schemas)
    except (NotImplementedError, AttributeError, TypeError, ValueError):
        # Provider/model doesn't support tool calling -> text fallback.
        return None
    except Exception:
        # Other errors (e.g. provider quirks) -> be conservative, use text mode.
        return None


def _extract_calls(resp: BaseMessage, *, native_tools: bool) -> list[dict]:
    """Pull tool calls out of a model response.

    Prefers native ``tool_calls``; falls back to parsing ``<tool_call>`` blocks
    from the text content so models without native tool support still work.
    """
    calls: list[dict] = []
    if native_tools:
        for tc in getattr(resp, "tool_calls", None) or []:
            calls.append(
                {
                    "name": tc.get("name"),
                    "args": tc.get("args") or tc.get("arguments") or {},
                    "id": tc.get("id"),
                }
            )
        if calls:
            return calls
    # Text fallback.
    text = getattr(resp, "content", "") or ""
    if isinstance(text, list):  # some providers return content blocks
        text = " ".join(b.get("text", "") for b in text if isinstance(b, dict))
    return parse_text_tool_calls(text)


def _tool_message(
    text: str, *, tool_call_id: str, name: str, ai_message: BaseMessage | None = None
) -> ToolMessage:
    """Build a ToolMessage observation, compressing long outputs."""
    if len(text.splitlines()) > 60:
        text = compress_output(text, tail_lines=60)
    return ToolMessage(content=text, tool_call_id=tool_call_id, name=name)
