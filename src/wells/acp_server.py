"""Agent Client Protocol (ACP) server for the Wells coding-agent harness.

Lets editor integrations (Zed, JetBrains, anything speaking ACP) drive Wells
over stdio JSON-RPC 2.0 with LSP-style ``Content-Length`` framing.

Start the server:

    wells-acp           # console script (wired by the parent session)
    python -m wells.acp_server

Protocol surface (agentclientprotocol.com, protocol version 1):

    initialize                -> agent capabilities
    initialize/:id            -> notification, ignored
    session/new               -> Session (primary/default mode)
    session/prompt            -> runs the task, streams session/update
                                notifications, replies with a stop reason
    session/cancel            -> cooperatively cancels the running prompt
    fs/read_text_file         -> unsupported (returns a JSON-RPC error)

The ACP layer is deliberately thin: the prompt path delegates to
:func:`wells.main._run_goal` — the exact function behind ``wells -p
--output-format json`` — so editors get the same pipeline, hooks, session
persistence, and JSON outcome the headless CLI produces.

Only the standard library is used (json, sys, threading, contextlib).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import uuid
from typing import Any, BinaryIO, Callable, TextIO

PROTOCOL_VERSION = 1

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

# Header separator for LSP-style framing (always \r\n per the framing spec).
HEADER_SEP = b"\r\n\r\n"


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def read_message(stream: BinaryIO) -> dict | None:
    """Read one Content-Length-framed JSON message from a binary stream.

    Returns None on EOF. Raises ValueError on a malformed frame.
    """
    header = b""
    while HEADER_SEP not in header:
        chunk = stream.read(1)
        if not chunk:
            return None
        header += chunk
        # Sanity cap to avoid unbounded memory on garbage input.
        if len(header) > 64 * 1024:
            raise ValueError("frame header too large")
    header_text = header.decode("ascii", errors="replace")
    content_length = None
    for line in header_text.split("\r\n"):
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            content_length = int(value.strip())
    if content_length is None:
        raise ValueError("missing Content-Length header")
    body = stream.read(content_length)
    if len(body) < content_length:
        raise ValueError("truncated frame body")
    return json.loads(body.decode("utf-8"))


def write_message(stream: BinaryIO, message: dict) -> None:
    """Write one Content-Length-framed JSON message to a binary stream."""
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    frame = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data
    stream.write(frame)
    stream.flush()


# ---------------------------------------------------------------------------
# Task runner (the seam tests monkeypatch)
# ---------------------------------------------------------------------------


def run_task(goal: str, cwd: str | None = None) -> dict:
    """Run one Wells task via the headless one-shot code path.

    Delegates to :func:`wells.main._run_goal` with ``output_format="json"``
    (the ``wells -p --output-format json`` plumbing) and captures the single
    JSON payload it prints to stdout. ``_run_goal`` signals completion via
    ``sys.exit``; that is intercepted here so the server loop keeps running.
    """
    import contextlib
    import io as _io

    from wells import config
    from wells.main import _run_goal

    previous_root = config.WORKSPACE_ROOT
    if cwd:
        config.WORKSPACE_ROOT = cwd
    buffer = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            _run_goal(goal, output_format="json")
    except SystemExit:
        pass
    finally:
        config.WORKSPACE_ROOT = previous_root

    text = buffer.getvalue().strip()
    if not text:
        return {"status": "error", "error": "no output from task runner"}
    try:
        # Exactly one JSON object is expected; tolerate stray lines.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return json.loads(text.splitlines()[-1])
    except json.JSONDecodeError:
        return {"status": "error", "error": f"unparseable output: {text[:400]}"}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class AcpServer:
    """ACP JSON-RPC dispatcher.

    ``send`` is called with each outgoing message (responses and
    notifications); the stdio loop wires it to framed writes, tests wire it
    to an in-memory list. Thread-safe: prompt handling runs on a worker
    thread so the read loop stays live for ``session/cancel``.
    """

    def __init__(self, send: Callable[[dict], None] | None = None,
                 task_runner: Callable[[str, str | None], dict] = run_task):
        self._send_fn = send
        self._task_runner = task_runner
        self._lock = threading.Lock()
        # sessionId -> {"thread": Thread, "done": Event, "cancelled": bool}
        self._running: dict[str, dict] = {}
        self.sessions: dict[str, str] = {}  # sessionId -> cwd
        self._update_counter = 0

    # -- outgoing ----------------------------------------------------------

    def _send(self, message: dict) -> None:
        if self._send_fn is not None:
            with self._lock:
                self._send_fn(message)

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _respond(self, request_id: Any, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _respond_error(self, request_id: Any, code: int, message: str,
                       data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def _session_update(self, session_id: str, update: dict) -> None:
        with self._lock:
            self._update_counter += 1
            update_id = self._update_counter
        self._notify("session/update", {
            "sessionId": session_id,
            "update": {"updateId": update_id, **update},
        })

    # -- incoming ----------------------------------------------------------

    def handle_message(self, message: dict) -> None:
        """Process one decoded JSON-RPC message (request or notification)."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._respond_error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
            return
        method = message.get("method")
        request_id = message.get("id")  # None => notification
        params = message.get("params") or {}

        if request_id is None:
            # Notifications: initialize/:id is the only one we know; unknown
            # notifications are silently dropped per JSON-RPC.
            return

        try:
            handler = {
                "initialize": self._on_initialize,
                "session/new": self._on_session_new,
                "session/prompt": self._on_session_prompt,
                "session/cancel": self._on_session_cancel,
                "fs/read_text_file": self._on_read_text_file,
            }.get(method)
            if handler is None:
                self._respond_error(request_id, METHOD_NOT_FOUND,
                                    f"method not found: {method}")
                return
            handler(request_id, params)
        except Exception as exc:  # never crash the loop
            self._respond_error(request_id, INTERNAL_ERROR,
                                f"{type(exc).__name__}: {exc}")

    # -- method handlers ---------------------------------------------------

    def _on_initialize(self, request_id: Any, params: dict) -> None:
        self._respond(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {"loadSession": False},
            "authMethods": [],
        })

    def _on_session_new(self, request_id: Any, params: dict) -> None:
        cwd = params.get("cwd") or ""
        session_id = str(uuid.uuid4())
        self.sessions[session_id] = cwd
        self._respond(request_id, {
            "sessionId": session_id,
            "mode": {"kind": "primary", "name": "default"},
            "mcpServers": [],
        })

    def _on_session_prompt(self, request_id: Any, params: dict) -> None:
        session_id = params.get("sessionId") or ""
        if session_id not in self.sessions:
            self._respond_error(request_id, INVALID_REQUEST,
                                f"unknown session: {session_id!r}")
            return
        prompt = params.get("prompt") or {}
        goal = self._prompt_text(prompt)
        cwd = self.sessions.get(session_id) or None

        entry: dict = {"done": threading.Event(), "cancelled": False}
        self._running[session_id] = entry

        def _work() -> None:
            from wells.control import CONTROL

            try:
                CONTROL.reset()
                self._session_update(session_id, {"sessionUpdate": "turn",
                                                  "state": "start"})
                try:
                    result = self._task_runner(goal, cwd)
                except SystemExit:
                    result = {"status": "error", "error": "task runner exited"}
                if CONTROL.cancelled() or entry["cancelled"]:
                    entry["cancelled"] = True
                text = self._result_text(result)
                if text:
                    self._session_update(session_id, {
                        "sessionUpdate": "agent_message",
                        "content": [{"type": "text", "text": text}],
                    })
                stop_reason = "cancelled" if entry["cancelled"] else "end_turn"
                self._session_update(session_id, {"sessionUpdate": "turn",
                                                  "state": "complete",
                                                  "stopReason": stop_reason})
                self._respond(request_id, {"stopReason": stop_reason})
            except Exception as exc:
                self._session_update(session_id, {"sessionUpdate": "turn",
                                                  "state": "complete",
                                                  "stopReason": "end_turn"})
                self._respond_error(request_id, INTERNAL_ERROR,
                                    f"{type(exc).__name__}: {exc}")
            finally:
                entry["done"].set()
                with self._lock:
                    self._running.pop(session_id, None)

        threading.Thread(target=_work, name=f"acp-prompt-{session_id[:8]}",
                         daemon=True).start()

    def _on_session_cancel(self, request_id: Any, params: dict) -> None:
        session_id = params.get("sessionId") or ""
        entry = self._running.get(session_id)
        if entry is not None:
            entry["cancelled"] = True
        try:
            from wells.control import CONTROL

            CONTROL.cancel()
        except Exception:
            pass  # control bus unavailable; entry flag still stops the turn
        self._respond(request_id, {})

    def _on_read_text_file(self, request_id: Any, params: dict) -> None:
        self._respond_error(request_id, METHOD_NOT_FOUND,
                            "fs/read_text_file is not supported")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _prompt_text(prompt: dict) -> str:
        """Flatten an ACP Prompt (content blocks) into plain text."""
        blocks = prompt.get("content") or []
        parts = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p).strip()

    @staticmethod
    def _result_text(result: dict) -> str:
        """Human-facing text for the final agent message of a turn."""
        if not isinstance(result, dict):
            return str(result)
        parts = []
        for key in ("summary", "review_result", "git_summary"):
            value = (result.get(key) or "").strip() if isinstance(result.get(key), str) else ""
            if value:
                parts.append(value)
        if result.get("status") == "error" and result.get("error"):
            parts.append(f"error: {result['error']}")
        return "\n\n".join(parts)

    def wait_for_prompts(self, timeout: float = 10.0) -> None:
        """Block until every in-flight prompt worker finishes (test helper)."""
        import time as _time

        end = _time.monotonic() + timeout
        while _time.monotonic() < end:
            with self._lock:
                entries = [e["done"] for e in self._running.values()]
            if not entries or all(e.is_set() for e in entries):
                return
            _time.sleep(0.01)


# ---------------------------------------------------------------------------
# Stdio entry point
# ---------------------------------------------------------------------------


def serve(stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> None:
    """Run the ACP server loop over Content-Length-framed stdio."""
    in_stream = stdin if stdin is not None else sys.stdin.buffer
    out_stream = stdout if stdout is not None else sys.stdout.buffer

    def _write(message: dict) -> None:
        try:
            write_message(out_stream, message)
        except (OSError, ValueError):
            pass  # client went away; loop exits on next read

    server = AcpServer(send=_write)
    while True:
        try:
            message = read_message(in_stream)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            server._respond_error(None, PARSE_ERROR, f"bad frame: {exc}")
            continue
        except OSError:
            break
        if message is None:
            break  # EOF — client closed the pipe
        try:
            server.handle_message(message)
        except Exception:
            # handle_message already guards itself; this is belt-and-braces.
            continue


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
