"""Tests for wells.notify: run-completion desktop + webhook notifications."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from wells import notify


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("WELLS_NOTIFY", raising=False)
    monkeypatch.delenv("WELLS_NOTIFY_WEBHOOK_URL", raising=False)


# ---------------------------------------------------------------------------
# enabled()
# ---------------------------------------------------------------------------


def test_enabled_off_by_default():
    assert notify.enabled() is False


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", ""])
def test_enabled_false_values(monkeypatch, value):
    monkeypatch.setenv("WELLS_NOTIFY", value)
    assert notify.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_enabled_true_values(monkeypatch, value):
    monkeypatch.setenv("WELLS_NOTIFY", value)
    assert notify.enabled() is True


# ---------------------------------------------------------------------------
# desktop_notify()
# ---------------------------------------------------------------------------


def test_desktop_notify_windows_invokes_powershell():
    with (
        patch.object(notify.platform, "system", return_value="Windows"),
        patch.object(notify.subprocess, "run") as mock_run,
    ):
        result = notify.desktop_notify("Wells: complete", "did the thing")
    assert result is True
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "powershell"
    assert "ShowBalloonTip" in args[-1]


def test_desktop_notify_escapes_single_quotes_for_powershell():
    with (
        patch.object(notify.platform, "system", return_value="Windows"),
        patch.object(notify.subprocess, "run") as mock_run,
    ):
        notify.desktop_notify("it's done", "can't fail")
    script = mock_run.call_args[0][0][-1]
    assert "it''s done" in script
    assert "can''t fail" in script


def test_desktop_notify_macos_uses_osascript():
    with (
        patch.object(notify.platform, "system", return_value="Darwin"),
        patch.object(notify.subprocess, "run") as mock_run,
    ):
        result = notify.desktop_notify("title", "message")
    assert result is True
    args = mock_run.call_args[0][0]
    assert args[0] == "osascript"


def test_desktop_notify_linux_uses_notify_send():
    with (
        patch.object(notify.platform, "system", return_value="Linux"),
        patch.object(notify.subprocess, "run") as mock_run,
    ):
        result = notify.desktop_notify("title", "message")
    assert result is True
    args = mock_run.call_args[0][0]
    assert args[0] == "notify-send"


def test_desktop_notify_never_raises_on_subprocess_failure():
    with (
        patch.object(notify.platform, "system", return_value="Windows"),
        patch.object(notify.subprocess, "run", side_effect=OSError("boom")),
    ):
        result = notify.desktop_notify("title", "message")
    assert result is False


# ---------------------------------------------------------------------------
# webhook_notify()
# ---------------------------------------------------------------------------


def test_webhook_notify_noop_without_url(monkeypatch):
    monkeypatch.delenv("WELLS_NOTIFY_WEBHOOK_URL", raising=False)
    assert notify.webhook_notify("summary") is False


def test_webhook_notify_posts_expected_payload(monkeypatch):
    monkeypatch.setenv("WELLS_NOTIFY_WEBHOOK_URL", "https://example.com/hook")
    mock_httpx = MagicMock()
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = notify.webhook_notify("run done", event="complete", detail="workspace=/x")
    assert result is True
    mock_httpx.post.assert_called_once()
    _, kwargs = mock_httpx.post.call_args
    assert kwargs["json"] == {
        "text": "run done",
        "event": "complete",
        "detail": "workspace=/x",
    }


def test_webhook_notify_never_raises_on_network_failure(monkeypatch):
    monkeypatch.setenv("WELLS_NOTIFY_WEBHOOK_URL", "https://example.com/hook")
    mock_httpx = MagicMock()
    mock_httpx.post.side_effect = OSError("network down")
    with patch.dict("sys.modules", {"httpx": mock_httpx}):
        result = notify.webhook_notify("run done")
    assert result is False


# ---------------------------------------------------------------------------
# notify_run_complete()
# ---------------------------------------------------------------------------


def test_notify_run_complete_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("WELLS_NOTIFY", raising=False)
    with (
        patch.object(notify, "desktop_notify") as mock_desktop,
        patch.object(notify, "webhook_notify") as mock_webhook,
    ):
        notify.notify_run_complete(goal="do it", status="complete", workspace="/ws")
    mock_desktop.assert_not_called()
    mock_webhook.assert_not_called()


def test_notify_run_complete_fires_both_channels_when_enabled(monkeypatch):
    monkeypatch.setenv("WELLS_NOTIFY", "1")
    with (
        patch.object(notify, "desktop_notify") as mock_desktop,
        patch.object(notify, "webhook_notify") as mock_webhook,
    ):
        notify.notify_run_complete(
            goal="fix the bug\nsecond line",
            status="complete",
            workspace="/ws",
            duration_seconds=42,
            cost=0.0123,
        )
    mock_desktop.assert_called_once()
    title, message = mock_desktop.call_args[0]
    assert "complete" in title
    assert "fix the bug" in message
    assert "second line" not in message  # only first line previewed
    assert "/ws" in message
    assert "42s" in message

    mock_webhook.assert_called_once()
    _, kwargs = mock_webhook.call_args
    assert kwargs["event"] == "complete"
    assert "workspace=/ws" in kwargs["detail"]
    assert "duration=42s" in kwargs["detail"]


def test_notify_run_complete_truncates_long_goal(monkeypatch):
    monkeypatch.setenv("WELLS_NOTIFY", "1")
    long_goal = "x" * 500
    with patch.object(notify, "desktop_notify") as mock_desktop, patch.object(notify, "webhook_notify"):
        notify.notify_run_complete(goal=long_goal, status="error", workspace="/ws")
    _, message = mock_desktop.call_args[0]
    goal_line = message.splitlines()[0]
    assert len(goal_line) == 100


def test_notify_run_complete_handles_blank_goal(monkeypatch):
    monkeypatch.setenv("WELLS_NOTIFY", "1")
    with patch.object(notify, "desktop_notify") as mock_desktop, patch.object(notify, "webhook_notify"):
        notify.notify_run_complete(goal="   ", status="cancelled", workspace="/ws")
    _, message = mock_desktop.call_args[0]
    assert "(no goal)" in message
