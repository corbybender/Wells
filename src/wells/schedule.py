"""``wells schedule``: register a goal to run on a recurring interval via the
OS's native scheduler (Task Scheduler on Windows, cron on Linux/macOS) --
closing the "no unattended/cron mode" gap versus Claude Code's own scheduled
routines. Wells itself doesn't need to be running for a scheduled run to
fire; the OS scheduler invokes the ``wells`` command directly.

Schedules are tracked in ``~/.wells/schedules.json`` (the source of truth
Wells itself manages, so ``wells schedule list`` works even if the OS-side
registration was later removed by hand) and mirrored into the OS scheduler.

Interval spec (one internal format, translated per platform):
  * ``every<N>m`` / ``every<N>h`` -- periodic (e.g. ``every15m``, ``every2h``)
  * ``daily@HH:MM``               -- once a day at a specific time (24h clock)
  * a raw 5-field cron expression -- Linux/macOS only; Windows Task
    Scheduler has no cron-expression concept, so this is rejected there
    rather than silently mistranslated.

Each schedule gets a small wrapper script (``~/.wells/schedule-scripts/``)
that the OS scheduler actually invokes, instead of trying to embed the goal
text directly in a Task Scheduler/cron command line. A goal can contain
spaces, quotes, and newlines; getting that right via nested command-line
quoting (especially on Windows, where quoting rules differ between
CreateProcess, cmd.exe, and Task Scheduler's own parsing of ``/TR``) is
fragile and easy to get subtly wrong. A PowerShell here-string / bash
heredoc, written by Python directly (never re-parsed through a shell), sidesteps
that whole class of bug -- the wrapper script's only variable content is
one already-validated schedule name in its filename; everything
user-controlled (goal, workspace path) lives inside a heredoc/here-string
body that needs no escaping at all.
"""

from __future__ import annotations

import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

_REGISTRY = Path.home() / ".wells" / "schedules.json"
_LOG_DIR = Path.home() / ".wells" / "schedule-logs"
_SCRIPT_DIR = Path.home() / ".wells" / "schedule-scripts"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_EVERY_RE = re.compile(r"^every(\d+)(m|h)$")
_DAILY_RE = re.compile(r"^daily@(\d{1,2}):(\d{2})$")


# ---------------------------------------------------------------------------
# Registry (~/.wells/schedules.json)
# ---------------------------------------------------------------------------


def _load() -> list[dict]:
    if not _REGISTRY.exists():
        return []
    try:
        data = json.loads(_REGISTRY.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    _REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    _REGISTRY.write_text(json.dumps(items, indent=2), encoding="utf-8")


def list_schedules() -> list[dict]:
    """All registered schedules, in creation order."""
    return _load()


def by_name(name: str) -> dict | None:
    target = (name or "").strip().lower()
    return next((e for e in _load() if e["name"] == target), None)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_name(name: str) -> str | None:
    n = (name or "").strip().lower()
    if not n:
        return "Schedule name is required."
    if not _NAME_RE.match(n):
        return "Schedule name must be lowercase letters, digits, and hyphens."
    if len(n) > 64:
        return "Schedule name must be 64 characters or fewer."
    return None


def validate_interval(interval: str) -> str | None:
    s = (interval or "").strip()
    if not s:
        return "interval is required."
    if _EVERY_RE.match(s) or _DAILY_RE.match(s):
        return None
    if len(s.split()) == 5:
        if platform.system() == "Windows":
            return (
                "Raw cron expressions aren't supported on Windows Task "
                "Scheduler -- use 'every<N>m', 'every<N>h', or 'daily@HH:MM'."
            )
        return None
    return (
        "interval must be 'every<N>m' (e.g. every15m), 'every<N>h' (e.g. "
        "every2h), 'daily@HH:MM', or a 5-field cron expression (Linux/macOS only)."
    )


# ---------------------------------------------------------------------------
# The command a scheduled run actually executes
# ---------------------------------------------------------------------------


def _wells_command() -> list[str]:
    """Prefers the installed ``wells`` launcher on PATH; falls back to
    ``sys.executable -m wells.main`` for a source checkout with no launcher
    installed (e.g. CI, or before running the install script)."""
    found = shutil.which("wells")
    if found:
        return [found]
    return [sys.executable, "-m", "wells.main"]


def _write_wrapper_script(name: str, goal: str, workspace: str) -> Path:
    """Write the per-platform wrapper script the scheduler invokes.

    The goal/workspace live inside a heredoc (POSIX) or here-string
    (PowerShell) body -- never re-quoted onto a single command line -- so
    arbitrary goal text (spaces, quotes, newlines) is always safe.
    """
    _SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    wells_cmd = _wells_command()
    exe, extra_args = wells_cmd[0], wells_cmd[1:]

    if platform.system() == "Windows":
        path = _SCRIPT_DIR / f"{name}.ps1"
        args_ps = " ".join(f'"{a}"' for a in extra_args)
        script = (
            f'& "{exe}" {args_ps} --workspace "{workspace}" @\'\n'
            f"{goal}\n"
            "'@\n"
        )
    else:
        path = _SCRIPT_DIR / f"{name}.sh"
        args_sh = " ".join(shlex.quote(a) for a in extra_args)
        script = (
            "#!/bin/sh\n"
            "GOAL=$(cat <<'WELLS_GOAL_EOF'\n"
            f"{goal}\n"
            "WELLS_GOAL_EOF\n"
            ")\n"
            f'"{exe}" {args_sh} --workspace {shlex.quote(workspace)} --print "$GOAL"\n'
        )
    path.write_text(script, encoding="utf-8")
    return path


def _remove_wrapper_script(name: str) -> None:
    for ext in (".ps1", ".sh"):
        try:
            (_SCRIPT_DIR / f"{name}{ext}").unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Windows: Task Scheduler (schtasks)
# ---------------------------------------------------------------------------

_WIN_TASK_FOLDER = "Wells"


def _win_task_name(name: str) -> str:
    return f"{_WIN_TASK_FOLDER}\\{name}"


def _register_windows(name: str, script_path: Path, interval: str) -> tuple[bool, str]:
    tr = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script_path}"'
    task_name = _win_task_name(name)

    m = _EVERY_RE.match(interval)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        sc = "MINUTE" if unit == "m" else "HOURLY"
        args = ["schtasks", "/Create", "/TN", task_name, "/TR", tr, "/SC", sc, "/MO", str(n), "/F"]
    else:
        md = _DAILY_RE.match(interval)
        if not md:
            return False, f"unsupported interval on Windows: {interval!r}"
        hh, mm = md.group(1).zfill(2), md.group(2)
        args = ["schtasks", "/Create", "/TN", task_name, "/TR", tr, "/SC", "DAILY", "/ST", f"{hh}:{mm}", "/F"]

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return False, "schtasks.exe not found -- Task Scheduler isn't available on this system"
    except Exception as e:
        return False, f"schtasks failed: {e}"
    if r.returncode != 0:
        return False, f"schtasks failed: {(r.stderr or r.stdout).strip()}"
    return True, f"registered Task Scheduler task '{task_name}'"


def _unregister_windows(name: str) -> tuple[bool, str]:
    task_name = _win_task_name(name)
    try:
        r = subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        return False, f"schtasks failed: {e}"
    if r.returncode != 0:
        return True, f"(Task Scheduler entry may already be gone: {(r.stderr or r.stdout).strip()[:120]})"
    return True, "Task Scheduler entry removed"


# ---------------------------------------------------------------------------
# Linux / macOS: cron
# ---------------------------------------------------------------------------

_CRON_MARKER = "# WELLS-SCHEDULE:"


def _interval_to_cron(interval: str) -> str | None:
    m = _EVERY_RE.match(interval)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return f"*/{n} * * * *" if unit == "m" else f"0 */{n} * * *"
    md = _DAILY_RE.match(interval)
    if md:
        hh, mm = int(md.group(1)), int(md.group(2))
        return f"{mm} {hh} * * *"
    if len(interval.split()) == 5:
        return interval
    return None


def _read_crontab() -> str:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return ""
    return r.stdout if r.returncode == 0 else ""


def _write_crontab(text: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["crontab", "-"], input=text, capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return False, "crontab is not available on this system"
    except Exception as e:
        return False, f"could not write crontab: {e}"
    if r.returncode != 0:
        return False, f"crontab failed: {r.stderr.strip()}"
    return True, ""


def _register_cron(name: str, script_path: Path, interval: str) -> tuple[bool, str]:
    cron_expr = _interval_to_cron(interval)
    if cron_expr is None:
        return False, f"unsupported interval: {interval!r}"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _LOG_DIR / f"{name}.log"
    line = (
        f"{cron_expr} sh {shlex.quote(str(script_path))} "
        f">> {shlex.quote(str(log_path))} 2>&1 {_CRON_MARKER}{name}"
    )
    existing = _read_crontab()
    new_crontab = existing.rstrip("\n")
    new_crontab = (new_crontab + "\n" if new_crontab else "") + line + "\n"
    ok, err = _write_crontab(new_crontab)
    if not ok:
        return False, err
    return True, f"registered a cron entry ({cron_expr}); logs at {log_path}"


def _unregister_cron(name: str) -> tuple[bool, str]:
    existing = _read_crontab()
    marker = f"{_CRON_MARKER}{name}"
    lines = existing.splitlines()
    kept = [ln for ln in lines if marker not in ln]
    if len(kept) == len(lines):
        return True, "(no cron entry found -- may already be removed)"
    new_crontab = "\n".join(kept) + ("\n" if kept else "")
    ok, err = _write_crontab(new_crontab)
    if not ok:
        return False, err
    return True, "cron entry removed"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_schedule(name: str, goal: str, interval: str, workspace: str) -> tuple[bool, str]:
    """Register a new schedule: validates, writes the wrapper script, and
    registers it with the OS scheduler. Returns ``(ok, message)``."""
    err = validate_name(name)
    if err:
        return False, err
    name = name.strip().lower()
    if not goal.strip():
        return False, "goal is required."
    err = validate_interval(interval)
    if err:
        return False, err
    interval = interval.strip()
    if by_name(name) is not None:
        return False, f"A schedule named {name!r} already exists. Remove it first."

    ws = str(Path(workspace).resolve())
    script_path = _write_wrapper_script(name, goal.strip(), ws)

    if platform.system() == "Windows":
        ok, msg = _register_windows(name, script_path, interval)
    else:
        ok, msg = _register_cron(name, script_path, interval)
    if not ok:
        _remove_wrapper_script(name)
        return False, msg

    items = _load()
    items.append({
        "name": name,
        "goal": goal.strip(),
        "workspace": ws,
        "interval": interval,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save(items)
    return True, f"Scheduled '{name}' ({interval}) -- {msg}"


def remove_schedule(name: str) -> tuple[bool, str]:
    """Unregister a schedule from both the OS scheduler and the registry."""
    name = (name or "").strip().lower()
    entry = by_name(name)
    if entry is None:
        return False, f"Unknown schedule {name!r}."

    if platform.system() == "Windows":
        ok, msg = _unregister_windows(name)
    else:
        ok, msg = _unregister_cron(name)
    _remove_wrapper_script(name)

    items = [e for e in _load() if e["name"] != name]
    _save(items)
    return True, f"Removed schedule {name!r}. {msg}" if ok else f"Removed from registry, but: {msg}"
