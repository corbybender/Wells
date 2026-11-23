"""Agent Client Protocol (ACP) server for the Wells coding-agent harness.

Lets editor integrations (Zed, JetBrains, anything speaking ACP) drive Wells
over stdio JSON-RPC 2.0 with LSP-style ``Content-Length`` framing.

Start the server:

    wells-acp           # console script
    python -m wells.acp_server

Protocol surface (agentclientprotocol.com, protocol version 1):

    initialize                -> agent capabilities
    initialize/:id            -> notification, ignored
    session/new               -> Session (primary/default mode)
    session/load              -> replays a saved session's history
    session/prompt            -> runs the task, streams session/update
                                 notifications, replies with a stop reason
    session/cancel            -> cooperatively cancels the running prompt
    fs/read_text_file         -> reads a file for the client (optional line
                                 and limit slicing, line numbers 1-based)

What a prompt streams
    While the task runs, the harness's event bus (:mod:`wells.control`) is
    forwarded to the client: every model text delta arrives as an
    ``agent_message_chunk``, and every tool call as a ``tool_call`` followed
    by a ``tool_call_update`` carrying its result. The final message is
    still sent as ``agent_message`` when the turn ends; a client that
    rendered the chunks may ignore it, per the spec.

Sessions on disk
    Each session's transcript (prompt and final message per turn, plus its
    working directory) is kept under ``~/.wells/acp/sessions/`` so
    ``session/load`` can restore it after the server process is restarted,
    which is what ``loadSession`` advertises. A follow-up prompt in the same
    session also hands the earlier turns to the harness as context, so
    "now add tests for that" builds on the previous turn instead of starting
    from nothing (``WELLS_ACP_CONTEXT=0`` turns that off).

The ACP layer is deliberately thin: the prompt path delegates to
:func:`wells.main._run_goal` — the exact function behind ``wells -p
--output-format json`` — so editors get the same pipeline, hooks, session
persistence, and JSON outcome the headless CLI produces.

Only the standard library is used.
"""

from __future__ import annotations

import collections
import contextlib
import io
import itertools
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Callable, TextIO

PROTOCOL_VERSION = 1

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Not a protocol fault: a request that was well-formed but cannot be served
# ("file not found", "no such session").
SERVER_ERROR = -32000

# Header separator for LSP-style framing (always \r\n per the framing spec).
HEADER_SEP = b"\r\n\r\n"

# Where session transcripts are kept so an editor can resume after a
# restart. Same per-user home as wells.sessions.
SESSIONS_DIR = Path.home() / ".wells" / "acp" / "sessions"
# A session id becomes a file name, so only plain identifiers are accepted.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# fs/read_text_file: never hand the client a monster file.
_READ_MAX_BYTES = 10 * 1024 * 1024
# How much of a tool call's arguments and result is forwarded, and how much
# of each earlier turn is handed back as context on a follow-up prompt.
_ARG_CHARS = 2_000
_TOOL_OUTPUT_CHARS = 4_000
_CONTEXT_TURNS = 6
_CONTEXT_CHARS = 600

_TOOL_KINDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("edit", frozenset({"write_file", "edit_file"})),
    ("execute", frozenset({"run_command", "run_tests", "run_code", "program"})),
    ("read", frozenset({"read_file", "list_dir"})),
    (
        "search",
        frozenset(
            {
                "grep", "glob", "find_symbol", "find_references",
                "find_callers", "list_symbols", "semantic_search",
            }
        ),
    ),
)


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
# Small helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _clamped(value: Any) -> Any:
    """Copy tool arguments with long strings cut down to a sane size."""
    if isinstance(value, str):
        return _truncate(value, _ARG_CHARS)
    if isinstance(value, dict):
        return {k: _clamped(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clamped(v) for v in value]
    return value


def _tool_kind(name: str) -> str:
    for kind, names in _TOOL_KINDS:
        if name in names:
            return kind
    return "other"


def _tool_title(source: Any, name: str, args: Any) -> str:
    """One line a person can scan: who ran what, on what."""
    label = str(source or "").strip()
    head = f"{label} · {name}" if label else name
    args = args if isinstance(args, dict) else {}
    for key in ("path", "file", "pattern", "symbol", "query", "command", "code"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return f"{head} {_truncate(' '.join(value.split()), 80)}"
    return head


def _context_enabled() -> bool:
    return os.environ.get("WELLS_ACP_CONTEXT", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ---------------------------------------------------------------------------
# Task runner (the seam tests monkeypatch)
# ---------------------------------------------------------------------------


def run_task(goal: str, cwd: str | None = None, resume_context: str | None = None) -> dict:
    """Run one Wells task via the headless one-shot code path.

    Delegates to :func:`wells.main._run_goal` with ``output_format="json"``
    (the ``wells -p --output-format json`` plumbing) and captures the single
    JSON payload it prints to stdout. ``_run_goal`` signals completion via
    ``sys.exit``; that is intercepted here so the server loop keeps running.

    ``resume_context`` is text the harness prepends to the goal, used to
    carry earlier turns of the session into a follow-up prompt. Note that
    ``config.WORKSPACE_ROOT`` is a module global, so two prompts running at
    once in different sessions would race on it; the server runs one turn
    per session and that is the supported model.
    """
    from wells import config
    from wells.main import _run_goal

    previous_root = config.WORKSPACE_ROOT
    if cwd:
        config.WORKSPACE_ROOT = cwd
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            if resume_context:
                _run_goal(goal, resume_context=resume_context, output_format="json")
            else:
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

    ``sessions_dir`` is where transcripts are persisted for ``session/load``
    (default :data:`SESSIONS_DIR`).
    """

    def __init__(self, send: Callable[[dict], None] | None = None,
                 task_runner: Callable[..., dict] = run_task,
                 sessions_dir: str | Path | None = None):
        self._send_fn = send
        self._task_runner = task_runner
        self._lock = threading.Lock()          # serialises writes to the client
        self._state_lock = threading.RLock()   # guards sessions / _history
        # sessionId -> {"thread": Thread, "done": Event, "cancelled": bool}
        self._running: dict[str, dict] = {}
        self.sessions: dict[str, str] = {}  # sessionId -> cwd
        # sessionId -> [{"prompt", "text", "stopReason", "status", "wellsSessionId"}]
        self._history: dict[str, list[dict]] = {}
        self._sessions_dir = Path(sessions_dir) if sessions_dir is not None else SESSIONS_DIR
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

    def _session_notification(self, session_id: str, update: dict) -> dict:
        with self._lock:
            self._update_counter += 1
            update_id = self._update_counter
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": {"updateId": update_id, **update}},
        }

    def _session_update(self, session_id: str, update: dict) -> None:
        self._send(self._session_notification(session_id, update))

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
        if not isinstance(params, dict):
            self._respond_error(request_id, INVALID_PARAMS, "params must be an object")
            return

        try:
            handler = {
                "initialize": self._on_initialize,
                "session/new": self._on_session_new,
                "session/load": self._on_session_load,
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
            "agentCapabilities": {"loadSession": True},
            "authMethods": [],
        })

    def _on_session_new(self, request_id: Any, params: dict) -> None:
        cwd = params.get("cwd") or ""
        session_id = str(uuid.uuid4())
        with self._state_lock:
            self.sessions[session_id] = cwd
            self._history[session_id] = []
        self._save(session_id)
        self._respond(request_id, {
            "sessionId": session_id,
            "mode": {"kind": "primary", "name": "default"},
            "mcpServers": [],
        })

    def _on_session_load(self, request_id: Any, params: dict) -> None:
        """Restore a session written by an earlier server process.

        The response is the transcript as the notifications a client would
        have seen live, so it can rebuild the conversation; the session then
        accepts further prompts as if the server had never restarted.
        """
        session_id = str(params.get("sessionId") or "")
        if not self._load(session_id):
            self._respond_error(request_id, SERVER_ERROR,
                                f"session not found: {session_id!r}")
            return
        cwd = params.get("cwd")
        if cwd:
            with self._state_lock:
                self.sessions[session_id] = str(cwd)
        history = []
        for turn in self._history.get(session_id, []):
            if turn.get("prompt"):
                history.append(self._session_notification(session_id, {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": turn["prompt"]},
                }))
            if turn.get("text"):
                history.append(self._session_notification(session_id, {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": turn["text"]},
                }))
        self._respond(request_id, {"history": history})

    def _on_session_prompt(self, request_id: Any, params: dict) -> None:
        session_id = str(params.get("sessionId") or "")
        # Tolerate a client that prompts a saved session without loading it
        # first: adopting it from disk is harmless and keeps the turn usable.
        if session_id not in self.sessions:
            self._load(session_id)
        if session_id not in self.sessions:
            self._respond_error(request_id, INVALID_REQUEST,
                                f"unknown session: {session_id!r}")
            return
        prompt = params.get("prompt") or {}
        goal = self._prompt_text(prompt)
        cwd = self.sessions.get(session_id) or None
        resume = self._resume_context(session_id) if _context_enabled() else None

        entry: dict = {"done": threading.Event(), "cancelled": False}
        self._running[session_id] = entry

        def _work() -> None:
            from wells.control import CONTROL

            try:
                CONTROL.reset()
                self._session_update(session_id, {"sessionUpdate": "turn",
                                                  "state": "start"})
                try:
                    with self._streaming(session_id):
                        if resume:
                            result = self._task_runner(goal, cwd, resume_context=resume)
                        else:
                            result = self._task_runner(goal, cwd)
                except SystemExit:
                    result = {"status": "error", "error": "task runner exited"}
                if CONTROL.cancelled() or entry["cancelled"]:
                    entry["cancelled"] = True
                text = self._result_text(result)
                stop_reason = "cancelled" if entry["cancelled"] else "end_turn"
                self._record_turn(session_id, goal, text, stop_reason, result)
                if text:
                    self._session_update(session_id, {
                        "sessionUpdate": "agent_message",
                        "content": [{"type": "text", "text": text}],
                    })
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
        """Read a text file on the client's behalf.

        ``line`` is 1-based (0 is treated as 1) and ``limit`` is the number
        of lines to return; both optional. The request comes from the
        client, not from the model, so any absolute path the user's editor
        asks for is fair game.
        """
        session_id = str(params.get("sessionId") or "")
        if session_id not in self.sessions:
            self._load(session_id)
        if session_id not in self.sessions:
            self._respond_error(request_id, INVALID_REQUEST,
                                f"unknown session: {session_id!r}")
            return
        path = params.get("path")
        if not isinstance(path, str) or not path.strip():
            self._respond_error(request_id, INVALID_PARAMS,
                                "fs/read_text_file requires a path")
            return
        if not os.path.isabs(path):
            self._respond_error(request_id, INVALID_PARAMS,
                                f"path must be absolute: {path!r}")
            return
        line, limit = params.get("line"), params.get("limit")
        for name, value in (("line", line), ("limit", limit)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                self._respond_error(request_id, INVALID_PARAMS,
                                    f"{name} must be a non-negative integer")
                return
        try:
            if not os.path.isfile(path):
                self._respond_error(request_id, SERVER_ERROR, f"file not found: {path}")
                return
            if os.path.getsize(path) > _READ_MAX_BYTES:
                self._respond_error(
                    request_id, SERVER_ERROR,
                    f"file is larger than {_READ_MAX_BYTES} bytes: {path}")
                return
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
                text = fh.read()
        except OSError as exc:
            self._respond_error(request_id, SERVER_ERROR,
                                f"cannot read {path}: {exc.strerror or exc}")
            return
        if line is not None or limit is not None:
            lines = text.splitlines(keepends=True)
            start = max(line or 1, 1) - 1
            if start > 0 and start >= len(lines):
                self._respond_error(
                    request_id, SERVER_ERROR,
                    f"line {line} is out of range: the file has {len(lines)} line(s)")
                return
            text = "".join(lines[start:] if limit is None else lines[start:start + limit])
        self._respond(request_id, {"text": text})

    # -- streaming the run -------------------------------------------------

    @contextlib.contextmanager
    def _streaming(self, session_id: str):
        """Forward the harness event bus to the client for one turn.

        The main channel carries the primary agent's model text deltas
        (``llm_chunk``); subagents run quiet and never reach it. The verbose
        channel is the always-on one, and it is where tool starts and ends
        are published with their arguments and results. Listeners are
        cleared again afterwards: nothing else in a headless process owns
        them.
        """
        open_calls: dict[str, collections.deque] = {}
        unfinished: list[str] = []
        ids = itertools.count(1)
        state = threading.Lock()

        def _main(ev) -> None:
            if ev.kind == "llm_chunk" and ev.text:
                self._session_update(session_id, {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": ev.text},
                })

        def _verbose(ev) -> None:
            data = ev.data or {}
            if ev.kind == "tool_start" and data.get("name"):
                name = str(data["name"])
                with state:
                    call_id = f"call-{next(ids)}"
                    open_calls.setdefault(name, collections.deque()).append(call_id)
                    unfinished.append(call_id)
                self._session_update(session_id, {
                    "sessionUpdate": "tool_call",
                    "toolCallId": call_id,
                    "title": _tool_title(data.get("source"), name, data.get("args")),
                    "kind": _tool_kind(name),
                    "status": "pending",
                    "rawInput": _clamped(data.get("args")),
                })
            elif ev.kind == "tool_end":
                name = str(data.get("name") or "")
                with state:
                    queue = open_calls.get(name)
                    call_id = queue.popleft() if queue else None
                    if call_id in unfinished:
                        unfinished.remove(call_id)
                if call_id is None:
                    return  # began before this turn started listening
                update: dict[str, Any] = {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "completed" if data.get("ok") else "failed",
                }
                output = str(data.get("output") or "")
                if output:
                    update["rawOutput"] = _truncate(output, _TOOL_OUTPUT_CHARS)
                self._session_update(session_id, update)

        from wells.control import CONTROL

        CONTROL.set_listener(_main)
        CONTROL.set_verbose_listener(_verbose)
        try:
            yield
        finally:
            CONTROL.set_listener(None)
            CONTROL.set_verbose_listener(None)
            # A call answered from the harness's read cache announces itself
            # but never ends; close it so the client has no spinner left.
            for call_id in list(unfinished):
                self._session_update(session_id, {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "completed",
                })

    # -- session state -----------------------------------------------------

    def _resume_context(self, session_id: str) -> str | None:
        """Earlier turns of this session, as context for the next prompt."""
        with self._state_lock:
            turns = [t for t in self._history.get(session_id, []) if t.get("prompt")]
        if not turns:
            return None
        lines = ["=== EARLIER TURNS IN THIS EDITOR SESSION ==="]
        for n, turn in enumerate(turns[-_CONTEXT_TURNS:], 1):
            lines.append(f"Turn {n} request: {turn['prompt'].strip()}")
            if turn.get("text"):
                lines.append(f"Turn {n} result: {_truncate(turn['text'].strip(), _CONTEXT_CHARS)}")
        lines.append(
            "The request below continues the conversation above: build on those "
            "results, do not redo them."
        )
        lines.append("=" * 40)
        return "\n".join(lines)

    def _record_turn(self, session_id: str, goal: str, text: str,
                     stop_reason: str, result: Any) -> None:
        turn = {
            "prompt": goal,
            "text": text,
            "stopReason": stop_reason,
            "status": result.get("status") if isinstance(result, dict) else None,
            "wellsSessionId": result.get("session_id") if isinstance(result, dict) else None,
        }
        with self._state_lock:
            self._history.setdefault(session_id, []).append(turn)
        self._save(session_id)

    def _save(self, session_id: str) -> None:
        """Best-effort: a session that cannot be written still works in memory."""
        if not _SESSION_ID_RE.match(session_id):
            return
        with self._state_lock:
            payload = {
                "sessionId": session_id,
                "cwd": self.sessions.get(session_id, ""),
                "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "turns": list(self._history.get(session_id, [])),
            }
        path = self._sessions_dir / f"{session_id}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{session_id}.json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)  # atomic: a crash never leaves a half-written session
        except OSError:
            pass

    def _load(self, session_id: str) -> bool:
        """Adopt a session written by an earlier server process, if there is one."""
        if session_id in self.sessions:
            return True
        if not _SESSION_ID_RE.match(session_id):
            return False
        try:
            data = json.loads(
                (self._sessions_dir / f"{session_id}.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            return False
        with self._state_lock:
            self.sessions[session_id] = str(data.get("cwd") or "")
            self._history[session_id] = [
                t for t in data.get("turns") or [] if isinstance(t, dict)
            ]
        return True

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
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                entries = [e["done"] for e in self._running.values()]
            if not entries or all(e.is_set() for e in entries):
                return
            time.sleep(0.01)


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
