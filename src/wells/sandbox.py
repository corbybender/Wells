"""Sandbox: a disposable per-workspace container for shell execution.

``sandbox`` is a fifth, fully opt-in ``HARNESS_SAFETY`` mode alongside the
existing auto/approve/dryrun/plan. It changes nothing about how those modes
behave: pick `/mode sandbox` (or set ``HARNESS_SAFETY=sandbox``) explicitly
for a run and Wells stays exactly as autonomous as ``auto`` — it just runs
shell commands inside a disposable container instead of directly on your
machine. Every other mode is completely untouched; a user who never asks
for sandboxing never launches a container runtime at all, same as Claude
Code / Codex / OpenCode running directly on the host today.

**Runtime-agnostic, not Docker-specific.** Every call here is a plain OCI
`run`/`exec`/`stop` — no Docker-only feature is used — so this works
identically against whichever CLI resolves:

  * **Podman** — daemonless on Linux, a much smaller VM than Docker Desktop
    on Mac/Windows, Apache-2.0 licensed (no Docker Desktop commercial-use
    terms to worry about). The lightest cross-platform option and the
    recommended default for anyone without an existing Docker setup.
  * **Docker** (Engine on Linux, or Desktop on Mac/Windows) — the more
    widely-installed option; used automatically if Podman isn't found.
  * Anything else that ships a `docker`-compatible CLI (Colima, OrbStack,
    Rancher Desktop) — set ``WELLS_SANDBOX_RUNTIME`` to whatever binary
    name it installs.

``WELLS_SANDBOX_RUNTIME`` pins the choice (``docker`` or ``podman``);
unset/``auto`` prefers Podman, falling back to Docker.

Why only shell commands, not file reads/writes/edits too? The workspace is
bind-mounted into the container at ``/workspace``, so both sides see
identical bytes — Wells' own path confinement (:mod:`wells.safety`) already
keeps every read/write/edit inside the workspace regardless of mode. The
actual risk a container isolates is *arbitrary process execution*:
installing something, reaching out to the network, running an untrusted
script — that's what ``run_command`` and CodeAct's ``run_code`` are
redirected into the container for.

Lifecycle: one container per workspace, launched lazily on first sandboxed
command and reused for the rest of the process (so state persists across a
session the way a real terminal would) — ``--rm`` so it self-removes on
stop, torn down explicitly via :func:`teardown` or at process exit.

:mod:`wells.codeact`'s ``run_code`` is sandboxed too, via
:func:`run_python_stdin`: the snippet is piped over
``<runtime> exec -i <cid> python3 -`` rather than written to a shared file,
matching CodeAct's host-path behavior of keeping the snippet out of the
workspace/repo map entirely.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import time
import uuid

_DEFAULT_IMAGE = "python:3.12-slim"

# workspace (resolved string path) -> running container id.
_containers: dict[str, str] = {}


def _runtime_bin() -> str | None:
    """Resolve which container CLI to use: ``WELLS_SANDBOX_RUNTIME``, or
    auto-detect (Podman preferred — lighter and license-friendlier — falling
    back to Docker). None if neither is on PATH.

    Not cached: cheap (a couple of PATH lookups) and needs to reflect
    ``WELLS_SANDBOX_RUNTIME``/PATH changes made after process start (e.g.
    via `/config`), not whatever resolved first.
    """
    pinned = os.environ.get("WELLS_SANDBOX_RUNTIME", "").strip().lower()
    if pinned:
        return pinned if shutil.which(pinned) else None
    return "podman" if shutil.which("podman") else ("docker" if shutil.which("docker") else None)


def enabled() -> bool:
    """Whether sandbox mode is even reachable — a container CLI must be on PATH.

    (Whether its daemon/machine is actually up is checked separately by
    :func:`runtime_available`, since that's a live probe worth re-checking
    rather than caching.)
    """
    return _runtime_bin() is not None


def runtime_available() -> bool:
    """True if a container CLI is installed AND its daemon responds.

    This is a *cheap* reachability check (``<runtime> info``) used both in
    production (before every sandbox command) and at test-collection time
    (the ``requires_docker`` skip-guard). It deliberately does NOT attempt
    to run a container: on a constrained CI runner that pulls an image and
    spawns containerd/crun daemons that the subprocess timeout can't fully
    reap, starving the runner. A broken OCI runtime that answers ``info``
    but fails every ``run`` is handled downstream — production surfaces
    the real error from :func:`ensure_container`, and the live-container
    tests skip on that ``RuntimeError`` instead of erroring.
    """
    runtime = _runtime_bin()
    if runtime is None:
        return False
    try:
        r = subprocess.run([runtime, "info"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _image() -> str:
    return os.environ.get("WELLS_SANDBOX_IMAGE", "").strip() or _DEFAULT_IMAGE


def ensure_container(workspace: str) -> str:
    """Return a running container id for ``workspace``, launching one if needed."""
    key = str(workspace)
    cid = _containers.get(key)
    if cid:
        return cid
    runtime = _runtime_bin()
    if runtime is None:
        raise RuntimeError(
            "No container runtime found (checked podman, docker). Install "
            "Podman (lightweight, recommended) or Docker, or set "
            "WELLS_SANDBOX_RUNTIME to the CLI name."
        )
    name = f"wells-sandbox-{uuid.uuid4().hex[:12]}"
    cmd = [
        runtime, "run", "-d", "--rm",
        "--name", name,
        "-v", f"{workspace}:/workspace:Z",
        "-w", "/workspace",
        _image(),
        "sleep", "infinity",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"Failed to start sandbox container ({runtime}): {r.stderr.strip()}")
    cid = r.stdout.strip()
    _containers[key] = cid
    return cid


def _popen_poll_loop(
    proc: subprocess.Popen, label: str, timeout: float
) -> subprocess.CompletedProcess:
    """Shared Popen-and-poll loop for both :func:`run_shell` and
    :func:`run_python_stdin`. Mirrors :func:`wells.tools._run_shell`'s
    approach (not a plain blocking ``subprocess.run``) so Escape/``/stop``
    can still interrupt a long-running sandboxed call — cancelling here
    kills the local ``exec`` client process; the containerized process
    itself is reaped when the container is torn down.
    """
    from wells.control import CONTROL, kill_process_tree
    from wells.tools import ShellCancelled

    CONTROL.track_proc(proc)
    t0 = time.monotonic()
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=0.25)
                return subprocess.CompletedProcess(label, proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                pass
            if CONTROL.cancelled():
                kill_process_tree(proc)
                raise ShellCancelled()
            if time.monotonic() - t0 >= timeout:
                kill_process_tree(proc)
                raise subprocess.TimeoutExpired(label, timeout)
    except BaseException:
        kill_process_tree(proc)
        raise
    finally:
        CONTROL.untrack_proc(proc)


def run_shell(command: str, workspace: str, timeout: float) -> subprocess.CompletedProcess:
    """Run ``command`` inside ``workspace``'s sandbox container via ``<runtime> exec``."""
    cid = ensure_container(workspace)
    args = [_runtime_bin(), "exec", cid, "sh", "-c", command]
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    return _popen_poll_loop(proc, command, timeout)


def run_python_stdin(code: str, workspace: str, timeout: float) -> subprocess.CompletedProcess:
    """Run ``code`` as a Python script inside ``workspace``'s sandbox container.

    Piped over stdin (``<runtime> exec -i <cid> python3 -``) rather than
    written to a shared file — keeps the snippet out of the bind-mounted
    workspace/repo map, matching CodeAct's host-path behavior.
    """
    cid = ensure_container(workspace)
    args = [_runtime_bin(), "exec", "-i", cid, "python3", "-"]
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    try:
        proc.stdin.write(code)
        proc.stdin.close()
    except Exception:
        pass
    finally:
        # Already closed above — clearing the handle stops Popen.communicate()'s
        # internal stdin.flush() (called unconditionally on a truthy self.stdin)
        # from raising ValueError on the closed file, which would otherwise be
        # treated as a real failure by _popen_poll_loop and kill the process.
        proc.stdin = None
    return _popen_poll_loop(proc, "run_code", timeout)


def teardown(workspace: str | None = None) -> None:
    """Stop tracked container(s) — one workspace, or all when ``workspace`` is None."""
    runtime = _runtime_bin()
    keys = [str(workspace)] if workspace is not None else list(_containers)
    for key in keys:
        cid = _containers.pop(key, None)
        if cid and runtime:
            try:
                subprocess.run(
                    [runtime, "stop", "-t", "2", cid], capture_output=True, timeout=15,
                )
            except Exception:
                pass


atexit.register(teardown)
