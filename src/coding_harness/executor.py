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
    AIMessage,
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


def _system_prompt(task: str, toolset: list[tools.ToolDef], *, plan_mode: bool,
                   workspace: str | None = None) -> str:
    catalog = _tool_catalog(toolset)
    plan_note = (
        "\n\nIMPORTANT: You are in PLAN MODE. Do NOT make changes. Use read-only tools "
        "(read_file, list_dir, glob, grep) to investigate, then describe exactly what "
        "changes you WOULD make. Write/edit/run tools will simulate."
        if plan_mode
        else ""
    )
    # The harness operating principles (AGENT.md) are always prepended so every
    # executor run is governed by the constitution, regardless of model.
    from coding_harness.principles import inject_into_prompt as inject_principles
    base = f"""You are an autonomous software engineering agent working inside a real code repository.
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
    return inject_principles(base, workspace)


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
    *, step: str, model: str, messages: list[BaseMessage], resp: BaseMessage,
    saved_by_trim: int = 0,
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
        saved_by_trim=saved_by_trim,
    )


# ---------------------------------------------------------------------------
# Working memory — structural task state maintained across tool calls
# ---------------------------------------------------------------------------

_WM_TAG = "<working_memory>"  # prefix that identifies the WM HumanMessage in the list


@dataclass
class WorkingMemory:
    """Compact structured state updated after every tool call and injected into
    every LLM request.  Never pruned — it IS the compressed truth of the run.

    Prevents the costliest agent failure modes:
    - Re-reading files already processed
    - Re-attempting approaches that already failed
    - Forgetting test state between rounds
    """

    task: str = ""
    files_modified: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    failed_commands: list[str] = field(default_factory=list)
    test_status: str = ""

    def update_from_tool(self, name: str, args: dict, result_text: str, ok: bool) -> None:
        path = (
            args.get("path") or args.get("file_path")
            or args.get("filename") or args.get("filepath") or ""
        )
        if name in {"read_file", "view_file", "cat"} and path:
            if path not in self.files_read:
                self.files_read.append(path)
        elif name in {"write_file", "edit_file", "patch_file", "str_replace_editor",
                      "str_replace", "create_file"} and path and ok:
            if path not in self.files_modified:
                self.files_modified.append(path)
            if path not in self.files_read:
                self.files_read.append(path)
        elif name in {"run_tests", "pytest", "test"}:
            lines = [l.strip() for l in result_text.splitlines() if l.strip()]
            for line in lines:
                low = line.lower()
                if any(k in low for k in ("passed", "failed", "error", "ok", "tests ran")):
                    self.test_status = line[:200]
                    break
            else:
                self.test_status = lines[0][:200] if lines else ""
        elif name in {"run_command", "bash", "shell"} and not ok:
            cmd = str(args.get("command", ""))[:60]
            err_lines = result_text.strip().splitlines()
            err = err_lines[-1][:80] if err_lines else ""
            entry = f"{cmd} → {err}"
            if entry not in self.failed_commands:
                self.failed_commands.append(entry)

    def is_empty(self) -> bool:
        return not any([self.files_modified, self.files_read,
                        self.failed_commands, self.test_status])

    def to_xml(self) -> str:
        parts = [_WM_TAG]
        if self.task:
            parts.append(f"  <task>{self.task[:300]}</task>")
        if self.files_modified:
            parts.append(
                f"  <files_modified>{', '.join(self.files_modified[-12:])}</files_modified>"
            )
        # Only list read-only files (not ones already in files_modified — redundant)
        read_only = [f for f in self.files_read if f not in self.files_modified]
        if read_only:
            parts.append(f"  <files_read>{', '.join(read_only[-12:])}</files_read>")
        if self.failed_commands:
            parts.append("  <failed_approaches>")
            for fc in self.failed_commands[-5:]:
                parts.append(f"    - {fc}")
            parts.append("  </failed_approaches>")
        if self.test_status:
            parts.append(f"  <test_status>{self.test_status}</test_status>")
        parts.append("</working_memory>")
        return "\n".join(parts)


def _inject_wm(messages: list[BaseMessage], wm: WorkingMemory) -> list[BaseMessage]:
    """Insert or replace the working memory HumanMessage (never pruned).

    Position: immediately after the first HumanMessage (the task).
    """
    if wm.is_empty():
        return messages
    wm_msg = HumanMessage(content=wm.to_xml())
    result = list(messages)
    for i, m in enumerate(result):
        if isinstance(m, HumanMessage) and (m.content or "").startswith(_WM_TAG):
            result[i] = wm_msg
            return result
    # Not found — insert after first HumanMessage
    for i, m in enumerate(result):
        if isinstance(m, HumanMessage):
            result.insert(i + 1, wm_msg)
            return result
    result.append(wm_msg)
    return result


# ---------------------------------------------------------------------------
# Observation masking (primary context management)
# ---------------------------------------------------------------------------

# Keep this many most-recent AI+Tool rounds verbatim; replace content in older ones.
_MASK_KEEP_ROUNDS = int(__import__("os").environ.get("WELLS_KEEP_ROUNDS", "4"))
# Only mask tool outputs larger than this many estimated tokens (small ones are cheap).
_MASK_MIN_TOKENS = int(__import__("os").environ.get("WELLS_MASK_MIN", "120"))
# Absolute drop threshold — safety valve, fires only when masking isn't enough.
_DROP_THRESHOLD = int(__import__("os").environ.get("WELLS_CTX_LIMIT", "18000"))
_DROP_TARGET = int(__import__("os").environ.get("WELLS_CTX_TARGET", "12000"))


def _ctx_tokens(messages: list[BaseMessage]) -> int:
    """Rough token estimate for the full message list."""
    total = 0
    for m in messages:
        content = getattr(m, "content", "") or ""
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        total += estimate_tokens(content)
        for tc in getattr(m, "tool_calls", None) or []:
            total += estimate_tokens(str(tc.get("args", {})))
    return total


def _mask_tool_result(name: str, args: dict, content: str) -> str:
    """Compress a large tool result to a typed 1-line summary.

    Type-aware so each summary carries the most useful signal for its tool kind.
    Index tools (find_symbol, etc.) are never masked — their output is already compact.
    """
    path = (
        args.get("path") or args.get("file_path")
        or args.get("filename") or args.get("pattern") or ""
    )
    lines = [l for l in content.splitlines() if l.strip()]
    n = len(lines)

    if name in {"read_file", "view_file", "cat"}:
        return f"[FILE_READ: {path} — {n} lines, content processed]"
    elif name in {"list_dir", "ls", "glob"}:
        return f"[LIST: {path or '.'} — {n} entries]"
    elif name in {"grep", "search", "ripgrep"}:
        pat = args.get("pattern", path) or ""
        matches = [l for l in lines if ":" in l or l.startswith("/")]
        return f"[GREP: '{pat[:60]}' — {len(matches)} matches]"
    elif name == "write_file":
        return f"[WRITE: {path} — {n} lines written, ok]"
    elif name in {"edit_file", "patch_file", "str_replace_editor", "str_replace", "create_file"}:
        return f"[EDIT: {path} — changes applied]"
    elif name in {"run_tests", "pytest", "test"}:
        for line in lines:
            low = line.lower()
            if any(k in low for k in ("passed", "failed", "error", "ok", "test")):
                return f"[TESTS: {line[:140]}]"
        return f"[TESTS: {lines[0][:140]}]" if lines else "[TESTS: complete]"
    elif name in {"run_command", "bash", "shell"}:
        cmd = str(args.get("command", ""))[:50]
        first = lines[0][:100] if lines else "ok"
        return f"[CMD '{cmd}': {first}]"
    elif name.startswith("find_") or name.startswith("search_") or name.startswith("list_symbol"):
        return content  # index tools are compact; never mask them
    else:
        first = lines[0][:120] if lines else "ok"
        return f"[{name}: {first}]"


def _apply_observation_masking(
    messages: list[BaseMessage],
    tool_meta: dict[str, tuple[str, dict]],
) -> tuple[list[BaseMessage], int]:
    """Replace large ToolMessage content in old rounds with typed 1-line summaries.

    The JetBrains Research finding (NeurIPS 2025 DL4C): masking beats naive drop
    at 52% lower cost with +2.6% solve rate because the AI reasoning turns — which
    describe what was learned and decided — are preserved intact. Only the raw tool
    output (file contents, grep walls, command stdout) is compressed.

    Always keeps:
    - All AIMessages verbatim (reasoning gold, never touched)
    - Last _MASK_KEEP_ROUNDS rounds verbatim (fresh context the model needs)
    - Tool results under _MASK_MIN_TOKENS (cheap to keep)
    - Index tool results (already compact)

    Returns (new_messages, estimated_tokens_saved).
    """
    ai_positions = [i for i, m in enumerate(messages) if isinstance(m, AIMessage)]
    if len(ai_positions) <= _MASK_KEEP_ROUNDS:
        return messages, 0

    cutoff = ai_positions[-_MASK_KEEP_ROUNDS]  # mask everything before this index

    result = list(messages)
    saved = 0
    for i, m in enumerate(messages[:cutoff]):
        if not isinstance(m, ToolMessage):
            continue
        content = m.content or ""
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        if estimate_tokens(content) <= _MASK_MIN_TOKENS:
            continue
        # Skip if already a 1-liner summary
        if content.startswith("[") and "\n" not in content.strip():
            continue
        name, args = tool_meta.get(m.tool_call_id or "", ("", {}))
        masked = _mask_tool_result(name, args, content)
        saved += estimate_tokens(content) - estimate_tokens(masked)
        result[i] = ToolMessage(content=masked, tool_call_id=m.tool_call_id, name=m.name)

    return result, max(0, saved)


def _safety_drop(messages: list[BaseMessage]) -> tuple[list[BaseMessage], int]:
    """Absolute last resort: drop complete oldest rounds when context still exceeds limit.

    Should rarely fire after observation masking. Indicates either an extremely long
    run or pathologically large tool outputs that couldn't be masked enough.
    """
    total = _ctx_tokens(messages)
    if total <= _DROP_THRESHOLD or len(messages) <= 3:
        return messages, 0

    # Protect head: system message + task HumanMessage + optional WM HumanMessage
    head_count = 0
    seen_human = False
    for m in messages:
        head_count += 1
        if isinstance(m, HumanMessage):
            if not seen_human:
                seen_human = True  # first HumanMessage = task
            elif (m.content or "").startswith(_WM_TAG):
                pass  # WM message — also protect
            else:
                head_count -= 1  # not WM, stop protecting here
                break

    head = messages[:head_count]
    tail = list(messages[head_count:])
    saved = 0

    while tail and (total - saved) > _DROP_TARGET:
        rounds_left = sum(1 for m in tail if isinstance(m, AIMessage))
        if rounds_left <= 1:
            break
        if isinstance(tail[0], ToolMessage):
            saved += estimate_tokens(getattr(tail[0], "content", "") or "")
            tail = tail[1:]
            continue
        if not isinstance(tail[0], AIMessage):
            break
        ai_c = getattr(tail[0], "content", "") or ""
        if isinstance(ai_c, list):
            ai_c = " ".join(b.get("text", "") for b in ai_c if isinstance(b, dict))
        saved += estimate_tokens(ai_c) + sum(
            estimate_tokens(str(tc.get("args", {})))
            for tc in (getattr(tail[0], "tool_calls", None) or [])
        )
        tail = tail[1:]
        while tail and isinstance(tail[0], ToolMessage):
            saved += estimate_tokens(getattr(tail[0], "content", "") or "")
            tail = tail[1:]

    if not saved:
        return messages, 0
    note = HumanMessage(content=f"[safety drop: ~{saved:,} tokens of old history removed]")
    return head + [note] + tail, saved


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

    system = _system_prompt(task, toolset, plan_mode=ctx.plan_mode, workspace=ctx.workspace)
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

    wm = WorkingMemory(task=task)
    _tool_meta: dict[str, tuple[str, dict]] = {}  # tool_call_id → (name, args)

    history: list[dict] = []
    steps = 0
    total_saved = 0

    while steps < cap:
        # ── Context management pipeline (order matters) ──────────────────────
        # 1. Working memory — always-in-context structural state, never pruned
        messages = _inject_wm(messages, wm)
        # 2. Observation masking — primary compression, keeps AI reasoning intact
        messages, mask_saved = _apply_observation_masking(messages, _tool_meta)
        # 3. Safety drop — absolute fallback, should rarely fire after masking
        messages, drop_saved = _safety_drop(messages)
        saved = mask_saved + drop_saved
        if saved:
            total_saved += saved
            ctx_now = _ctx_tokens(messages)
            parts = []
            if mask_saved:
                parts.append(f"masked ~{mask_saved:,}")
            if drop_saved:
                parts.append(f"dropped ~{drop_saved:,}")
            print(f"[executor] ctx: {' + '.join(parts)} tokens → ~{ctx_now:,} remaining")
        # ─────────────────────────────────────────────────────────────────────

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
        _account_usage(step=step_label, model=model_name, messages=messages, resp=resp,
                       saved_by_trim=saved)
        messages.append(resp)

        calls = _extract_calls(resp, native_tools=native_tools)

        if not calls:
            return ExecutorResult(
                summary=(getattr(resp, "content", "") or "").strip() or "(no output)",
                steps_taken=steps,
                tool_calls=history,
                stopped_reason="done",
                messages=messages,
            )

        for call in calls:
            steps += 1
            name = call.get("name")
            args = call.get("args") or {}
            tcid = call.get("id") or f"call_{steps}"

            if not name:
                obs_text = "[error: malformed tool call — missing name]"
                history.append({"name": "?", "args": args, "ok": False,
                                "output_preview": obs_text})
                messages.append(
                    _tool_message(obs_text, tool_call_id="parseerr", name="parse_error")
                )
                continue

            # Store metadata so the masker can produce type-aware summaries later.
            _tool_meta[tcid] = (name, args)

            result = tools.dispatch(name, args, ctx)
            obs_text = result.to_model_text()

            # Update working memory with the structural facts from this result.
            wm.update_from_tool(name, args, obs_text, result.ok)

            history.append({
                "name": name,
                "args": args,
                "ok": result.ok,
                "output_preview": (result.output or result.error or "")[:200],
                "simulated": result.simulated,
            })
            print(
                f"[executor] {name}({list(args.keys())}) -> {'ok' if result.ok else 'fail'}"
                + (" [simulated]" if result.simulated else "")
            )
            messages.append(
                _tool_message(obs_text, tool_call_id=tcid, name=name, ai_message=resp)
            )

        if steps >= cap:
            # One final round: apply full context pipeline then ask for summary.
            try:
                messages = _inject_wm(messages, wm)
                messages, ms = _apply_observation_masking(messages, _tool_meta)
                messages, ds = _safety_drop(messages)
                final_saved = ms + ds
                final = config._invoke_with_retry(llm, messages)
                _account_usage(step=step_label, model=model_name, messages=messages,
                               resp=final, saved_by_trim=final_saved)
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
