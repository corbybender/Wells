"""Tests for the ACP (Agent Client Protocol) stdio server.

The task runner is stubbed — no LLM calls happen here. Framing, dispatch,
session lifecycle, prompt turn streaming, unknown-method errors, and cancel
are exercised against an in-memory message sink.
"""

from __future__ import annotations

import io
import json
import threading

import pytest

from wells import acp_server as acp
from wells.acp_server import (
    AcpServer,
    read_message,
    run_task,
    write_message,
)


@pytest.fixture(autouse=True)
def _isolated_session_store(tmp_path, monkeypatch):
    """Never read or write the user's real ~/.wells/acp/sessions."""
    monkeypatch.setattr(acp, "SESSIONS_DIR", tmp_path / "acp-sessions")


@pytest.fixture(autouse=True)
def _reset_control():
    """Clear the global cancel flag after every test.

    The cancel tests set CONTROL.cancel(); without this reset the flag leaks
    into every later test in the session (background, codemode, executor),
    which all check cancelled() and abort. Global mutable control state
    demands teardown hygiene.
    """
    from wells.control import CONTROL

    CONTROL.reset()
    yield
    CONTROL.reset()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_server(task_runner=None) -> tuple[AcpServer, list[dict]]:
    sent: list[dict] = []
    server = AcpServer(send=sent.append,
                       task_runner=task_runner or (lambda goal, cwd: {"status": "complete", "summary": f"echo: {goal}"}))
    return server, sent


def request(server: AcpServer, method: str, params: dict | None = None, req_id: int | str = 1) -> None:
    server.handle_message({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})


def result_of(sent: list[dict], req_id: int | str):
    msgs = [m for m in sent if m.get("id") == req_id and "result" in m]
    assert msgs, f"no result for id {req_id} in {[m.get('method') or m.get('id') for m in sent]}"
    return msgs[-1]["result"]


def new_session(server: AcpServer, sent: list[dict], cwd: str = "Q:/wells") -> str:
    request(server, "session/new", {"cwd": cwd}, req_id="new")
    return result_of(sent, "new")["sessionId"]


def prompt_blocks(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def test_framing_roundtrip() -> None:
    stream = io.BytesIO()
    write_message(stream, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    stream.seek(0)
    assert read_message(stream) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_framing_utf8_multibyte() -> None:
    stream = io.BytesIO()
    write_message(stream, {"text": "héllo — ✓"})
    stream.seek(0)
    assert read_message(stream)["text"] == "héllo — ✓"


def test_framing_eof_returns_none() -> None:
    assert read_message(io.BytesIO(b"")) is None


def test_framing_missing_header_raises() -> None:
    with pytest.raises(ValueError):
        read_message(io.BytesIO(b"Garbage: 1\r\n\r\n{}"))


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def test_initialize() -> None:
    server, sent = make_server()
    request(server, "initialize", {"clientCapabilities": {}})
    res = result_of(sent, 1)
    assert res["protocolVersion"] == 1
    assert res["agentCapabilities"] == {"loadSession": True}
    assert res["authMethods"] == []


def test_initialize_notification_no_response() -> None:
    server, sent = make_server()
    server.handle_message({"jsonrpc": "2.0", "method": "initialize/abc",
                           "params": {"protocolVersion": 1}})
    assert sent == []


# ---------------------------------------------------------------------------
# session/new + session/prompt
# ---------------------------------------------------------------------------


def test_session_new() -> None:
    server, sent = make_server()
    request(server, "session/new", {"cwd": "Q:/wells"}, req_id=7)
    res = result_of(sent, 7)
    assert res["sessionId"]
    assert res["mode"] == {"kind": "primary", "name": "default"}
    assert res["mcpServers"] == []


def test_session_prompt_runs_task_and_streams_turn() -> None:
    calls: list[tuple[str, str | None]] = []

    def stub(goal: str, cwd: str | None) -> dict:
        calls.append((goal, cwd))
        return {"status": "complete", "summary": "did the thing"}

    server, sent = make_server(stub)
    sid = new_session(server, sent)
    request(server, "session/prompt",
            {"sessionId": sid, "prompt": prompt_blocks("hello world")}, req_id=9)
    server.wait_for_prompts()

    assert calls == [("hello world", "Q:/wells")]
    res = result_of(sent, 9)
    assert res == {"stopReason": "end_turn"}

    updates = [m["params"]["update"]
               for m in sent if m.get("method") == "session/update"]
    kinds = [(u["sessionUpdate"], u.get("state")) for u in updates]
    assert ("turn", "start") in kinds
    assert ("turn", "complete") in kinds
    # turn start precedes complete
    assert kinds.index(("turn", "start")) < kinds.index(("turn", "complete"))
    # final agent message carries the summary text
    agent = [u for u in updates if u["sessionUpdate"] == "agent_message"]
    assert agent and agent[-1]["content"][0]["text"] == "did the thing"
    # every update references the session and a fresh updateId
    for m in sent:
        if m.get("method") == "session/update":
            assert m["params"]["sessionId"] == sid
            assert isinstance(m["params"]["update"]["updateId"], int)


def test_session_prompt_multiblock_prompt_joined() -> None:
    seen: list[str] = []

    def stub(goal: str, cwd: str | None) -> dict:
        seen.append(goal)
        return {"status": "complete", "summary": "ok"}

    server, sent = make_server(stub)
    sid = new_session(server, sent)
    request(server, "session/prompt",
            {"sessionId": sid,
             "prompt": {"content": [
                 {"type": "text", "text": "part one"},
                 {"type": "text", "text": "part two"},
             ]}}, req_id=3)
    server.wait_for_prompts()
    assert seen == ["part one\npart two"]
    assert result_of(sent, 3) == {"stopReason": "end_turn"}


def test_session_prompt_unknown_session_is_error() -> None:
    server, sent = make_server()
    request(server, "session/prompt",
            {"sessionId": "nope", "prompt": prompt_blocks("x")}, req_id=4)
    errs = [m for m in sent if m.get("id") == 4 and "error" in m]
    assert errs and errs[0]["error"]["code"] == -32600


def test_session_prompt_runner_exception_returns_error_not_crash() -> None:
    def boom(goal: str, cwd: str | None) -> dict:
        raise RuntimeError("model exploded")

    server, sent = make_server(boom)
    sid = new_session(server, sent)
    request(server, "session/prompt",
            {"sessionId": sid, "prompt": prompt_blocks("x")}, req_id=5)
    server.wait_for_prompts()
    errs = [m for m in sent if m.get("id") == 5 and "error" in m]
    assert errs and errs[0]["error"]["code"] == -32603
    assert "model exploded" in errs[0]["error"]["message"]
    # turn still completes so the editor UI doesn't hang
    updates = [m["params"]["update"] for m in sent
               if m.get("method") == "session/update"]
    assert any(u.get("sessionUpdate") == "turn" and u.get("state") == "complete"
               for u in updates)


# ---------------------------------------------------------------------------
# session/cancel
# ---------------------------------------------------------------------------


def test_session_cancel_sets_flag_and_stop_reason() -> None:
    started = threading.Event()

    def slow(goal: str, cwd: str | None) -> dict:
        started.set()
        # Simulate a long run; the cancel flag makes the worker report
        # "cancelled" once the (stubbed) run returns.
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.01)
        return {"status": "complete", "summary": "late"}

    server, sent = make_server(slow)
    sid = new_session(server, sent)
    request(server, "session/prompt",
            {"sessionId": sid, "prompt": prompt_blocks("long task")}, req_id=11)

    assert started.wait(timeout=5)
    request(server, "session/cancel", {"sessionId": sid}, req_id=12)
    assert result_of(sent, 12) == {}

    server.wait_for_prompts(timeout=10)
    assert result_of(sent, 11) == {"stopReason": "cancelled"}
    updates = [m["params"]["update"] for m in sent
               if m.get("method") == "session/update"]
    complete = [u for u in updates if u.get("sessionUpdate") == "turn"
                and u.get("state") == "complete"]
    assert complete and complete[-1]["stopReason"] == "cancelled"


def test_session_cancel_without_running_prompt_is_noop() -> None:
    server, sent = make_server()
    sid = new_session(server, sent)
    request(server, "session/cancel", {"sessionId": sid}, req_id=13)
    assert result_of(sent, 13) == {}


# ---------------------------------------------------------------------------
# Unknown methods + malformed input
# ---------------------------------------------------------------------------


def test_unknown_method_returns_32601() -> None:
    server, sent = make_server()
    # A real ACP method this server does not implement.
    request(server, "session/set_mode", {"sessionId": "x"}, req_id=21)
    errs = [m for m in sent if m.get("id") == 21 and "error" in m]
    assert errs
    assert errs[0]["error"]["code"] == -32601
    assert "session/set_mode" in errs[0]["error"]["message"]


def test_params_must_be_an_object() -> None:
    server, sent = make_server()
    server.handle_message(
        {"jsonrpc": "2.0", "id": 23, "method": "session/new", "params": ["Q:/wells"]}
    )
    errs = [m for m in sent if m.get("id") == 23 and "error" in m]
    assert errs and errs[0]["error"]["code"] == -32602


def test_non_jsonrpc_message_rejected() -> None:
    server, sent = make_server()
    server.handle_message({"id": 1, "method": "initialize"})
    errs = [m for m in sent if m.get("id") is None and "error" in m]
    assert errs and errs[0]["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# fs/read_text_file
# ---------------------------------------------------------------------------


def _error_of(sent: list[dict], req_id) -> dict:
    errs = [m for m in sent if m.get("id") == req_id and "error" in m]
    assert errs, f"no error response for id {req_id}"
    return errs[0]["error"]


def _read(server: AcpServer, sent: list[dict], sid: str, req_id, **params) -> None:
    request(
        server, "fs/read_text_file", {"sessionId": sid, **params}, req_id=req_id
    )


def test_read_text_file_whole_file(tmp_path) -> None:
    target = tmp_path / "notes.txt"
    target.write_bytes("one\r\ntwo\nthree\nhéllo — ✓\n".encode("utf-8"))
    server, sent = make_server()
    sid = new_session(server, sent)
    _read(server, sent, sid, 31, path=str(target))
    # Bytes come back as they are: line endings untouched, UTF-8 decoded.
    assert result_of(sent, 31) == {"text": "one\r\ntwo\nthree\nhéllo — ✓\n"}


def test_read_text_file_line_and_limit(tmp_path) -> None:
    target = tmp_path / "five.txt"
    target.write_bytes(b"l1\nl2\nl3\nl4\nl5\n")  # exact bytes, any platform
    server, sent = make_server()
    sid = new_session(server, sent)
    _read(server, sent, sid, 41, path=str(target), line=2, limit=2)
    assert result_of(sent, 41) == {"text": "l2\nl3\n"}
    _read(server, sent, sid, 42, path=str(target), line=4)  # to the end
    assert result_of(sent, 42) == {"text": "l4\nl5\n"}
    _read(server, sent, sid, 43, path=str(target), line=0)  # 0 counts as line 1
    assert result_of(sent, 43) == {"text": "l1\nl2\nl3\nl4\nl5\n"}
    _read(server, sent, sid, 44, path=str(target), limit=1)
    assert result_of(sent, 44) == {"text": "l1\n"}


def test_read_text_file_rejects_bad_requests(tmp_path) -> None:
    target = tmp_path / "three.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")
    server, sent = make_server()
    sid = new_session(server, sent)

    _read(server, sent, sid, 51, path="relative.txt")
    assert _error_of(sent, 51)["code"] == -32602
    _read(server, sent, sid, 52, path=str(target), line=-1)
    assert _error_of(sent, 52)["code"] == -32602
    _read(server, sent, sid, 53, path=str(target), limit="2")
    assert _error_of(sent, 53)["code"] == -32602
    _read(server, sent, sid, 54, path=str(target), line=True)
    assert _error_of(sent, 54)["code"] == -32602
    _read(server, sent, sid, 55)
    assert _error_of(sent, 55)["code"] == -32602

    _read(server, sent, sid, 56, path=str(tmp_path / "missing.txt"))
    assert _error_of(sent, 56)["code"] == -32000
    assert "not found" in _error_of(sent, 56)["message"]
    _read(server, sent, sid, 57, path=str(target), line=4)
    assert _error_of(sent, 57)["code"] == -32000
    assert "out of range" in _error_of(sent, 57)["message"]

    _read(server, sent, "nope", 58, path=str(target))
    assert _error_of(sent, 58)["code"] == -32600


# ---------------------------------------------------------------------------
# Streaming: model text deltas and tool calls
# ---------------------------------------------------------------------------


def _updates(sent: list[dict], kind: str) -> list[dict]:
    return [
        m["params"]["update"]
        for m in sent
        if m.get("method") == "session/update"
        and m["params"]["update"].get("sessionUpdate") == kind
    ]


def test_turn_streams_text_deltas_and_tool_calls() -> None:
    from wells.control import CONTROL

    def busy(goal: str, cwd: str | None) -> dict:
        assert CONTROL.emit("llm_chunk", "Hel")
        assert CONTROL.emit("llm_chunk", "lo")
        CONTROL.emit_verbose(
            "tool_start", "", name="read_file", args={"path": "src/app.py"},
            source="coder",
        )
        CONTROL.emit_verbose(
            "tool_end", name="read_file", ok=True, output="contents of app"
        )
        CONTROL.emit_verbose(
            "tool_start", "", name="run_tests", args={"command": "pytest -q"},
            source="tester",
        )
        CONTROL.emit_verbose("tool_end", name="run_tests", ok=False, output="1 failed")
        return {"status": "complete", "summary": "did the thing"}

    server, sent = make_server(busy)
    sid = new_session(server, sent)
    request(server, "session/prompt",
            {"sessionId": sid, "prompt": prompt_blocks("go")}, req_id=61)
    server.wait_for_prompts()

    chunks = [u["content"]["text"] for u in _updates(sent, "agent_message_chunk")]
    assert chunks == ["Hel", "lo"]

    calls = _updates(sent, "tool_call")
    assert [(c["title"], c["kind"], c["status"]) for c in calls] == [
        ("coder · read_file src/app.py", "read", "pending"),
        ("tester · run_tests pytest -q", "execute", "pending"),
    ]
    assert calls[0]["rawInput"] == {"path": "src/app.py"}
    assert all(isinstance(c["toolCallId"], str) for c in calls)

    done = _updates(sent, "tool_call_update")
    assert [(u["toolCallId"], u["status"]) for u in done] == [
        (calls[0]["toolCallId"], "completed"),
        (calls[1]["toolCallId"], "failed"),
    ]
    assert done[0]["rawOutput"] == "contents of app"
    assert done[1]["rawOutput"] == "1 failed"

    # The complete message is still delivered at the end of the turn.
    final = _updates(sent, "agent_message")
    assert final[-1]["content"][0]["text"] == "did the thing"
    assert result_of(sent, 61) == {"stopReason": "end_turn"}

    # The bus is released afterwards: nothing is left listening.
    assert CONTROL.emit("probe") is False


def test_turn_closes_a_tool_call_that_never_ended() -> None:
    """A call answered from the harness's read cache is announced but never
    ends; the client must not be left with a spinner."""
    from wells.control import CONTROL

    def cached(goal: str, cwd: str | None) -> dict:
        CONTROL.emit_verbose(
            "tool_start", "", name="list_dir", args={"path": "."}, source="coder"
        )
        return {"status": "complete", "summary": "ok"}

    server, sent = make_server(cached)
    sid = new_session(server, sent)
    request(server, "session/prompt",
            {"sessionId": sid, "prompt": prompt_blocks("go")}, req_id=62)
    server.wait_for_prompts()
    call = _updates(sent, "tool_call")[0]
    closed = _updates(sent, "tool_call_update")
    assert len(closed) == 1
    assert closed[0]["toolCallId"] == call["toolCallId"]
    assert closed[0]["status"] == "completed"


def test_long_tool_arguments_and_output_are_clamped(monkeypatch) -> None:
    from wells.control import CONTROL

    monkeypatch.setattr(acp, "_ARG_CHARS", 10)
    monkeypatch.setattr(acp, "_TOOL_OUTPUT_CHARS", 10)

    def big(goal: str, cwd: str | None) -> dict:
        CONTROL.emit_verbose(
            "tool_start", "", name="write_file",
            args={"path": "x.txt", "content": "C" * 500}, source="coder",
        )
        CONTROL.emit_verbose("tool_end", name="write_file", ok=True, output="R" * 500)
        return {"status": "complete", "summary": "ok"}

    server, sent = make_server(big)
    sid = new_session(server, sent)
    request(server, "session/prompt",
            {"sessionId": sid, "prompt": prompt_blocks("go")}, req_id=63)
    server.wait_for_prompts()
    raw = _updates(sent, "tool_call")[0]["rawInput"]
    assert raw["path"] == "x.txt" and raw["content"] == "C" * 10 + "…"
    assert _updates(sent, "tool_call_update")[0]["rawOutput"] == "R" * 10 + "…"


# ---------------------------------------------------------------------------
# session/load: transcripts survive a server restart, follow-ups carry context
# ---------------------------------------------------------------------------


def test_session_load_replays_history_after_restart(tmp_path) -> None:
    seen: list[str | None] = []

    def first_process(goal: str, cwd: str | None) -> dict:
        seen.append(cwd)
        return {"status": "complete", "summary": "did the thing"}

    a, sent_a = make_server(first_process)
    sid = new_session(a, sent_a, cwd=str(tmp_path))
    request(a, "session/prompt", {"sessionId": sid, "prompt": prompt_blocks("hello")},
            req_id=71)
    a.wait_for_prompts()

    # A new server process: nothing is in memory any more.
    b, sent = make_server(lambda goal, cwd, resume_context=None: {"status": "complete"})
    request(b, "session/load", {"sessionId": sid}, req_id=72)
    res = result_of(sent, 72)
    replayed = [n["params"]["update"] for n in res["history"]]
    assert [(u["sessionUpdate"], u["content"]["text"]) for u in replayed] == [
        ("user_message_chunk", "hello"),
        ("agent_message_chunk", "did the thing"),
    ]
    for n in res["history"]:
        assert n["jsonrpc"] == "2.0" and n["method"] == "session/update"
        assert n["params"]["sessionId"] == sid

    # The restored session keeps its working directory and takes a new prompt.
    request(b, "session/prompt", {"sessionId": sid, "prompt": prompt_blocks("again")},
            req_id=73)
    b.wait_for_prompts()
    assert result_of(sent, 73) == {"stopReason": "end_turn"}
    assert b.sessions[sid] == str(tmp_path)


def test_session_load_honours_a_new_cwd(tmp_path) -> None:
    a, sent_a = make_server()
    sid = new_session(a, sent_a, cwd="Q:/old")
    b, sent = make_server()
    request(b, "session/load", {"sessionId": sid, "cwd": str(tmp_path)}, req_id=74)
    assert result_of(sent, 74) == {"history": []}  # fresh session: nothing yet
    assert b.sessions[sid] == str(tmp_path)


def test_session_load_unknown_or_malformed_session_is_an_error(tmp_path) -> None:
    server, sent = make_server()
    request(server, "session/load", {"sessionId": "not-a-saved-session"}, req_id=75)
    assert _error_of(sent, 75)["code"] == -32000
    # An id is used as a file name: anything path-like is refused outright,
    # without touching the filesystem outside the session store.
    request(server, "session/load", {"sessionId": "../../etc/passwd"}, req_id=76)
    assert _error_of(sent, 76)["code"] == -32000
    assert not (tmp_path / "acp-sessions").exists()  # nothing was written or read


def test_corrupt_session_file_is_ignored(tmp_path, monkeypatch) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "abc.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(acp, "SESSIONS_DIR", store)
    server, sent = make_server()
    request(server, "session/load", {"sessionId": "abc"}, req_id=77)
    assert _error_of(sent, 77)["code"] == -32000


def test_follow_up_prompt_carries_the_earlier_turns(monkeypatch) -> None:
    seen: list[str | None] = []

    def runner(goal: str, cwd: str | None, resume_context: str | None = None) -> dict:
        seen.append(resume_context)
        return {"status": "complete", "summary": f"done: {goal}"}

    server, sent = make_server(runner)
    sid = new_session(server, sent)
    request(server, "session/prompt", {"sessionId": sid, "prompt": prompt_blocks("first")},
            req_id=81)
    server.wait_for_prompts()
    request(server, "session/prompt", {"sessionId": sid, "prompt": prompt_blocks("second")},
            req_id=82)
    server.wait_for_prompts()

    assert seen[0] is None  # nothing came before the first turn
    assert seen[1] is not None and "first" in seen[1] and "done: first" in seen[1]
    assert "second" not in seen[1]  # the new prompt is passed separately

    monkeypatch.setenv("WELLS_ACP_CONTEXT", "0")
    request(server, "session/prompt", {"sessionId": sid, "prompt": prompt_blocks("third")},
            req_id=83)
    server.wait_for_prompts()
    assert seen[2] is None  # opt-out honoured


def test_run_task_passes_resume_context_through(monkeypatch) -> None:
    import wells.main as main_mod

    def fake_run_goal(goal, *, resume_context=None, output_format="text",
                      image_paths=None):
        assert resume_context == "earlier turns"
        print(json.dumps({"status": "complete"}))
        raise SystemExit(0)

    monkeypatch.setattr(main_mod, "_run_goal", fake_run_goal)
    assert run_task("do it", resume_context="earlier turns")["status"] == "complete"


# ---------------------------------------------------------------------------
# run_task plumbing (headless code-path wrapper)
# ---------------------------------------------------------------------------


def test_run_task_captures_json_payload(monkeypatch) -> None:
    import wells.main as main_mod

    def fake_run_goal(goal, *, resume_context=None, output_format="text",
                      image_paths=None):
        assert output_format == "json"
        print(json.dumps({"status": "complete", "summary": f"ran {goal}"}))
        raise SystemExit(0)

    monkeypatch.setattr(main_mod, "_run_goal", fake_run_goal)
    result = run_task("do it", cwd=None)
    assert result["status"] == "complete"
    assert result["summary"] == "ran do it"


def test_run_task_runner_error(monkeypatch) -> None:
    import wells.main as main_mod

    def fake_run_goal(goal, **kwargs):
        print(json.dumps({"status": "error", "error": "model not configured"}))
        raise SystemExit(1)

    monkeypatch.setattr(main_mod, "_run_goal", fake_run_goal)
    result = run_task("x")
    assert result["status"] == "error"
    assert "model" in result["error"]


# ---------------------------------------------------------------------------
# serve() loop end-to-end over BytesIO
# ---------------------------------------------------------------------------


def test_serve_loop_end_to_end() -> None:
    import wells.acp_server as acp

    outgoing = io.BytesIO()

    def stub(goal: str, cwd: str | None) -> dict:
        return {"status": "complete", "summary": f"echo: {goal}"}

    incoming = io.BytesIO()
    for msg in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "session/new",
         "params": {"cwd": "Q:/wells"}},
    ):
        write_message(incoming, msg)
    incoming.seek(0)

    real_AcpServer = acp.AcpServer

    class LoopServer(real_AcpServer):
        def __init__(self, send=None, task_runner=None):
            super().__init__(send=send, task_runner=stub)

    monkeypatch_server = LoopServer

    # Run serve in a thread; it exits on EOF once the input is drained.
    original_server_cls = acp.AcpServer
    acp.AcpServer = monkeypatch_server
    try:
        t = threading.Thread(
            target=lambda: acp.serve(incoming, outgoing), daemon=True)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive()
    finally:
        acp.AcpServer = original_server_cls

    outgoing.seek(0)
    replies = []
    while True:
        msg = read_message(outgoing)
        if msg is None:
            break
        replies.append(msg)
    init = next(m for m in replies if m.get("id") == 1)
    assert init["result"]["protocolVersion"] == 1
    session = next(m for m in replies if m.get("id") == 2)
    assert session["result"]["mode"]["kind"] == "primary"
