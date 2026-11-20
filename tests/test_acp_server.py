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

from wells.acp_server import (
    AcpServer,
    read_message,
    run_task,
    write_message,
)


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
    assert res["agentCapabilities"] == {"loadSession": False}
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
    request(server, "session/load", {"sessionId": "x"}, req_id=21)
    errs = [m for m in sent if m.get("id") == 21 and "error" in m]
    assert errs
    assert errs[0]["error"]["code"] == -32601
    assert "session/load" in errs[0]["error"]["message"]


def test_fs_read_text_file_unsupported() -> None:
    server, sent = make_server()
    request(server, "fs/read_text_file", {"path": "x.txt"}, req_id=22)
    errs = [m for m in sent if m.get("id") == 22 and "error" in m]
    assert errs and errs[0]["error"]["code"] == -32601


def test_non_jsonrpc_message_rejected() -> None:
    server, sent = make_server()
    server.handle_message({"id": 1, "method": "initialize"})
    errs = [m for m in sent if m.get("id") is None and "error" in m]
    assert errs and errs[0]["error"]["code"] == -32600


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
