"""Fast deterministic post-edit checks (the self-heal layer).

After the agent writes or edits a file, the harness — not the model — runs the
quickest available checker for that file type and injects any failure straight
into the agent's next observation. Broken code is caught in milliseconds
instead of a full tester round-trip (an LLM call) later.

Design constraints:
  * Fast only: per-file syntax/error checks, never whole-project builds.
  * Errors only: style findings are noise here (ruff runs with --select E9,F).
  * Never blocking: any checker failure/timeout degrades to "no report".
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

_TIMEOUT = 15  # seconds; a per-file check that takes longer isn't "fast"
_MAX_REPORT_LINES = 15


@lru_cache(maxsize=None)
def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(args: list[str], cwd: str) -> tuple[int, str]:
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=_TIMEOUT
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _trim(report: str) -> str:
    lines = [l for l in report.splitlines() if l.strip()]
    if len(lines) > _MAX_REPORT_LINES:
        lines = lines[:_MAX_REPORT_LINES] + [f"… {len(lines) - _MAX_REPORT_LINES} more lines"]
    return "\n".join(lines)


def _check_python(path: Path, workspace: str) -> str | None:
    if _has("ruff"):
        # E9 = syntax/runtime errors, F = pyflakes (undefined names, bad imports).
        code, out = _run(
            ["ruff", "check", "--select", "E9,F", "--no-cache", str(path)], workspace
        )
        return _trim(out) if code != 0 and out else None
    # Fallback: syntax check with the interpreter itself (always available).
    code, out = _run([sys.executable, "-m", "py_compile", str(path)], workspace)
    return _trim(out) if code != 0 and out else None


def _check_json(path: Path) -> str | None:
    try:
        json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return None
    except json.JSONDecodeError as e:
        return f"invalid JSON: {e}"


def _check_js(path: Path, workspace: str) -> str | None:
    if not _has("node"):
        return None
    code, out = _run(["node", "--check", str(path)], workspace)
    return _trim(out) if code != 0 and out else None


def quick_check(path: str, workspace: str) -> str | None:
    """Run the fastest available checker for ``path``.

    Returns a short error report when the file fails, else None. Never raises.
    """
    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path(workspace) / p
        if not p.exists():
            return None
        ext = p.suffix.lower()
        if ext in (".py", ".pyw"):
            return _check_python(p, workspace)
        if ext == ".json":
            return _check_json(p)
        if ext in (".js", ".mjs", ".cjs"):
            return _check_js(p, workspace)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Semantic checkers (opt-in, WELLS_SEMANTIC_CHECK=1) -- real type-checkers
# instead of syntax-only checks. These run whole-project (a type error is
# often only visible with cross-file context) so they're slower; the fast
# path above stays the default.
# ---------------------------------------------------------------------------

_SEMANTIC_TIMEOUT = 60
_MAX_SEMANTIC_LINES = 20


def _run_semantic(args: list[str], cwd: str) -> tuple[int, str]:
    # Resolve the command to its full path: on Windows, npm-installed CLIs
    # (tsc, etc.) are ``.cmd`` shims that CreateProcess can't launch by bare
    # name without shell=True. shutil.which already found it once for the
    # _has() gate above, so this is a cheap, harmless no-op on POSIX.
    cmd = list(args)
    resolved = shutil.which(cmd[0])
    if resolved:
        cmd[0] = resolved
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=_SEMANTIC_TIMEOUT
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _filter_for_file(report: str, path: Path) -> str:
    """Project-wide checkers report on every file; keep only lines about
    the one just edited so the agent isn't drowned in unrelated errors."""
    name = path.name
    return "\n".join(l for l in report.splitlines() if name in l)


def _trim_semantic(report: str) -> str | None:
    if not report.strip():
        return None
    lines = [l for l in report.splitlines() if l.strip()]
    if len(lines) > _MAX_SEMANTIC_LINES:
        lines = lines[:_MAX_SEMANTIC_LINES] + [f"… {len(lines) - _MAX_SEMANTIC_LINES} more lines"]
    return "\n".join(lines)


def _semantic_python(path: Path, workspace: str) -> str | None:
    if _has("pyright"):
        _code, out = _run_semantic(["pyright", "--outputjson", str(path)], workspace)
        try:
            data = json.loads(out)
        except Exception:
            return None
        errors = [
            d for d in data.get("generalDiagnostics", []) if d.get("severity") == "error"
        ]
        if not errors:
            return None
        lines = []
        for d in errors[:_MAX_SEMANTIC_LINES]:
            line_no = d.get("range", {}).get("start", {}).get("line", 0) + 1
            lines.append(f"{path.name}:{line_no}: {d.get('message', '')}")
        return "\n".join(lines)
    if _has("mypy"):
        code, out = _run_semantic(
            ["mypy", "--hide-error-context", "--no-color-output", "--no-error-summary", str(path)],
            workspace,
        )
        return _trim_semantic(out) if code != 0 else None
    return None


def _semantic_typescript(path: Path, workspace: str) -> str | None:
    if not _has("tsc"):
        return None
    tsconfig = Path(workspace) / "tsconfig.json"
    if tsconfig.exists():
        code, out = _run_semantic(["tsc", "--noEmit", "-p", workspace], workspace)
        if code == 0:
            return None
        return _trim_semantic(_filter_for_file(out, path))
    code, out = _run_semantic(["tsc", "--noEmit", str(path)], workspace)
    return _trim_semantic(out) if code != 0 else None


def _semantic_rust(path: Path, workspace: str) -> str | None:
    if not _has("cargo"):
        return None
    code, out = _run_semantic(["cargo", "check", "--quiet", "--message-format=short"], workspace)
    if code == 0:
        return None
    return _trim_semantic(_filter_for_file(out, path))


def _semantic_go(path: Path, workspace: str) -> str | None:
    if not _has("go"):
        return None
    code, out = _run_semantic(["go", "vet", "./..."], workspace)
    if code == 0:
        return None
    return _trim_semantic(_filter_for_file(out, path))


def semantic_check(path: str, workspace: str) -> str | None:
    """Run a real type-checker (project-aware) for ``path``, when available.

    Returns a short error report when the file fails, else None. Never
    raises -- a missing/misconfigured toolchain just means no report,
    same contract as ``quick_check``.
    """
    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path(workspace) / p
        if not p.exists():
            return None
        ext = p.suffix.lower()
        if ext in (".py", ".pyw"):
            return _semantic_python(p, workspace)
        if ext in (".ts", ".tsx"):
            return _semantic_typescript(p, workspace)
        if ext == ".rs":
            return _semantic_rust(p, workspace)
        if ext == ".go":
            return _semantic_go(p, workspace)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    return None
