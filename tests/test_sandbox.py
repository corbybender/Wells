"""Tests for sandbox mode: policy gating, registration, guardrails.

Live-container tests are skipped unless a container runtime (Podman or
Docker) is on PATH *and* actually reachable -- neither is part of the base
dependency set, matching how test_browser.py handles Playwright.
"""

from __future__ import annotations

import pytest

from wells import safety, sandbox, tools


# ---------------------------------------------------------------------------
# Policy: sandbox behaves like auto (allowed), just a different mode label
# ---------------------------------------------------------------------------


def test_sandbox_is_a_valid_policy():
    assert safety.policy("sandbox") == "sandbox"


def test_unknown_policy_falls_back_to_auto():
    assert safety.policy("not-a-real-mode") == "auto"


def test_gate_allows_sandbox_like_auto():
    decision = safety.gate("run_command", "echo hi", safety="sandbox")
    assert decision.allowed
    assert decision.mode == "sandbox"
    assert not decision.simulated


def test_gate_sandbox_not_affected_by_missing_approver():
    # Unlike "approve", sandbox never degrades to dry-run for lack of an
    # approver -- it's a full-autonomy mode like "auto".
    decision = safety.gate("run_command", "echo hi", safety="sandbox", approver=None)
    assert decision.allowed


# ---------------------------------------------------------------------------
# run_command routing: sandbox mode without a reachable runtime must
# fail loudly and cleanly, never silently fall back to running on the host.
# ---------------------------------------------------------------------------


def test_run_command_sandbox_without_runtime_fails_clean(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "runtime_available", lambda: False)
    ctx = tools.ToolContext(workspace=str(tmp_path), safety="sandbox")
    r = tools.dispatch("run_command", {"command": "echo hi"}, ctx)
    assert not r.ok
    assert "docker" in r.error.lower()


def test_run_command_sandbox_respects_plan_mode(tmp_path):
    # plan_mode short-circuits before the sandbox/docker check entirely.
    ctx = tools.ToolContext(workspace=str(tmp_path), safety="sandbox", plan_mode=True)
    r = tools.dispatch("run_command", {"command": "echo hi"}, ctx)
    assert r.simulated
    assert "plan" in r.output.lower()


def test_run_command_auto_mode_never_touches_sandbox(monkeypatch, tmp_path):
    # Sanity: auto mode must not be routed through sandbox.runtime_available
    # at all -- everyday use never depends on Docker being installed.
    called = []
    monkeypatch.setattr(
        sandbox, "runtime_available", lambda: called.append(True) or False
    )
    ctx = tools.ToolContext(workspace=str(tmp_path), safety="auto")
    r = tools.dispatch("run_command", {"command": "echo hi"}, ctx)
    assert r.ok
    assert not called


# ---------------------------------------------------------------------------
# Runtime CLI availability probe
# ---------------------------------------------------------------------------


def test_enabled_false_without_any_runtime_cli(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    assert not sandbox.enabled()


def test_runtime_bin_prefers_podman(monkeypatch):
    monkeypatch.delenv("WELLS_SANDBOX_RUNTIME", raising=False)
    monkeypatch.setattr(
        sandbox.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    assert sandbox._runtime_bin() == "podman"


def test_runtime_bin_falls_back_to_docker(monkeypatch):
    monkeypatch.delenv("WELLS_SANDBOX_RUNTIME", raising=False)
    monkeypatch.setattr(
        sandbox.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    assert sandbox._runtime_bin() == "docker"


def test_runtime_bin_respects_pin(monkeypatch):
    monkeypatch.setenv("WELLS_SANDBOX_RUNTIME", "docker")
    monkeypatch.setattr(
        sandbox.shutil, "which", lambda name: f"/usr/bin/{name}"
    )
    assert sandbox._runtime_bin() == "docker"


# ---------------------------------------------------------------------------
# Live container tests
# ---------------------------------------------------------------------------


def _runtime_up() -> bool:
    try:
        return sandbox.runtime_available()
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _runtime_up(),
    reason="No container runtime reachable (start Podman or Docker to run these)",
)


# ---------------------------------------------------------------------------
# runtime_available probe: catches a broken OCI runtime, not just a missing
# binary. Regression for the CI failure where Podman's `crun` returned
# `unknown version specified` — `podman info` passed but every `podman run`
# failed, so live-container tests errored instead of skipping.
# ---------------------------------------------------------------------------


def test_runtime_available_caches_result(monkeypatch):
    """The probe (which runs a container) must not fire on every call."""
    sandbox._probe_cache.clear()
    calls = []
    orig = sandbox._probe_runtime

    def counting(runtime):
        calls.append(runtime)
        return orig(runtime)

    monkeypatch.setattr(sandbox, "_probe_runtime", counting)
    sandbox.runtime_available()
    sandbox.runtime_available()
    sandbox.runtime_available()
    assert len(calls) == 1  # second/third hit the cache


def test_runtime_available_detects_broken_oci_runtime(monkeypatch):
    """`info` succeeding but `run` failing (broken crun) → not available.

    Simulates the exact CI breakage: the runtime binary exists and its
    daemon answers `info`, but actually starting a container fails."""
    sandbox._probe_cache.clear()
    monkeypatch.setattr(sandbox, "_runtime_bin", lambda: "podman")

    class _FakeRun:
        def __init__(self, args):
            self.args = args

        @property
        def returncode(self):
            # `info` and `image inspect` pass; `run` fails (broken crun).
            if "info" in self.args or "inspect" in self.args:
                return 0
            return 1

    def fake_run(cmd, **kwargs):
        return _FakeRun(cmd)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.runtime_available() is False


def test_runtime_available_falls_back_when_image_absent(monkeypatch):
    """If the configured image isn't local, skip the run-probe and trust info."""
    sandbox._probe_cache.clear()
    monkeypatch.setattr(sandbox, "_runtime_bin", lambda: "podman")

    class _FakeRun:
        def __init__(self, args):
            self.args = args

        @property
        def returncode(self):
            if "info" in self.args:
                return 0
            if "inspect" in self.args:
                return 1  # image not present locally
            return 1  # a `run` shouldn't even happen here

    run_calls = []

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return _FakeRun(cmd)

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    assert sandbox.runtime_available() is True
    # Must NOT have attempted `run` (no image to run) — only info + inspect.
    assert not any("run" in c for c in run_calls)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "data.txt").write_text("hello from host\n", encoding="utf-8")
    yield tmp_path
    sandbox.teardown(str(tmp_path))


@requires_docker
def test_run_shell_in_container_sees_bind_mounted_workspace(workspace):
    ctx = tools.ToolContext(workspace=str(workspace), safety="sandbox", shell_timeout=60)
    r = tools.dispatch("run_command", {"command": "cat data.txt"}, ctx)
    assert r.ok
    assert "hello from host" in r.output


@requires_docker
def test_run_shell_in_container_is_isolated_from_host_env(workspace):
    # The container is a different OS filesystem than the host -- a path
    # that exists on the host root shouldn't exist inside it.
    ctx = tools.ToolContext(workspace=str(workspace), safety="sandbox", shell_timeout=60)
    r = tools.dispatch("run_command", {"command": "cat /etc/os-release"}, ctx)
    assert r.ok
    assert "debian" in r.output.lower() or "ubuntu" in r.output.lower()


@requires_docker
def test_container_reused_across_calls(workspace):
    ctx = tools.ToolContext(workspace=str(workspace), safety="sandbox", shell_timeout=60)
    tools.dispatch("run_command", {"command": "echo hi"}, ctx)
    cid_1 = sandbox._containers.get(str(workspace))
    tools.dispatch("run_command", {"command": "echo hi again"}, ctx)
    cid_2 = sandbox._containers.get(str(workspace))
    assert cid_1 is not None
    assert cid_1 == cid_2
