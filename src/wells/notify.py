"""notify: run-completion notifications (desktop + generic webhook).

Solves a real gap the scheduling feature (``wells schedule``) opened up: a
scheduled run happens completely unattended — with no notification, the
only way to know it finished (or failed) is to go check a log file by
hand. Two lightweight, zero-new-dependency channels:

  * **Desktop notification** — shells out to each OS's own native
    mechanism (a balloon tip via .NET's ``System.Windows.Forms`` on
    Windows, ``osascript`` on macOS, ``notify-send`` on Linux). No extra
    package required on any platform; a missing/unsupported mechanism
    just means no popup, never a broken run.
  * **Webhook** — a plain POST of ``{"text": ..., "event": ...}`` to a
    URL. That exact shape is what Slack's Incoming Webhooks expect, so
    pointing ``WELLS_NOTIFY_WEBHOOK_URL`` at a Slack webhook URL works
    out of the box with zero Slack-specific code; any other service that
    accepts a JSON POST works too.

Off by default (``WELLS_NOTIFY=0``) — firing a notification on every
interactive ``wells "<goal>"`` call would be noisy. Turn it on for
scheduled/headless runs, where "did it finish" isn't otherwise visible.
"""

from __future__ import annotations

import os
import platform
import subprocess

_STATUS_ICON = {"complete": "✓", "incomplete": "◐", "error": "✗", "cancelled": "⊘"}


def enabled() -> bool:
    """Whether notifications fire at all (``WELLS_NOTIFY`` != 0; off by default)."""
    return os.environ.get("WELLS_NOTIFY", "0").strip().lower() not in ("0", "false", "no", "off", "")


def _ps_escape(text: str) -> str:
    """Escape a string for embedding in a single-quoted PowerShell literal."""
    return text.replace("'", "''")


def _osa_escape(text: str) -> str:
    """Escape a string for embedding in a double-quoted AppleScript literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def desktop_notify(title: str, message: str) -> bool:
    """Best-effort native desktop notification. Returns whether a mechanism
    was attempted (not necessarily whether the user saw it) — never raises."""
    system = platform.system()
    try:
        if system == "Windows":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                "$n.Visible = $true; "
                f"$n.ShowBalloonTip(10000, '{_ps_escape(title)}', '{_ps_escape(message)}', "
                "[System.Windows.Forms.ToolTipIcon]::Info); "
                "Start-Sleep -Seconds 1; "
                "$n.Dispose()"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, timeout=15,
            )
            return True
        if system == "Darwin":
            osa = (
                f'display notification "{_osa_escape(message)}" '
                f'with title "{_osa_escape(title)}"'
            )
            subprocess.run(["osascript", "-e", osa], capture_output=True, timeout=10)
            return True
        # Linux and anything else — notify-send is the de-facto standard on
        # desktop distros (part of libnotify-bin); silently no-op if absent.
        subprocess.run(["notify-send", title, message], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def webhook_notify(summary: str, *, event: str = "", detail: str = "") -> bool:
    """POST a Slack-compatible {"text": ...} payload to WELLS_NOTIFY_WEBHOOK_URL.

    No-op (returns False) when the URL isn't configured. Never raises —
    a network hiccup on the notification must not affect the run it's
    reporting on, which has already finished by the time this is called.
    """
    url = os.environ.get("WELLS_NOTIFY_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        import httpx

        payload: dict = {"text": summary}
        if event:
            payload["event"] = event
        if detail:
            payload["detail"] = detail
        httpx.post(url, json=payload, timeout=10)
        return True
    except Exception:
        return False


def notify_run_complete(
    *,
    goal: str,
    status: str,
    workspace: str,
    duration_seconds: int = 0,
    cost: float | None = None,
) -> None:
    """Fire every configured notification channel for a finished run.

    ``status`` is one of ``complete`` / ``incomplete`` / ``error`` /
    ``cancelled``. No-op entirely when ``WELLS_NOTIFY`` isn't set — safe
    for callers to invoke unconditionally at the end of every run.
    """
    if not enabled():
        return
    from wells import pricing

    cost_str = pricing.fmt(cost) if cost else ""
    icon = _STATUS_ICON.get(status, "")
    goal_preview = next(iter(goal.strip().splitlines()), "")[:100] or "(no goal)"

    title = f"Wells: {status}" + (f" ({cost_str})" if cost_str else "")
    message = f"{goal_preview}\n{workspace} · {duration_seconds}s"
    desktop_notify(title, message)

    webhook_notify(
        f"{icon} Wells run {status}: {goal_preview}".strip(),
        event=status,
        detail=(
            f"workspace={workspace} duration={duration_seconds}s "
            f"cost={cost_str or 'n/a'}"
        ),
    )
