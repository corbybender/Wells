"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _stable_active_profile(monkeypatch):
    """Pin ``config.ACTIVE_PROFILE`` to the historical test-safe default.

    ``config.py`` loads the developer's real ``Q:\\wells\\.env`` at import
    time, so without this, ``ACTIVE_PROFILE`` is whatever ``MODEL_PROFILE``
    happens to be set to on this machine (e.g. a tunneled local-Ollama
    profile). Tests that build a real chat model for the active profile —
    even ones with no interest in local-model behavior — would then
    silently pick up local-model-only code paths (compact prompt, stream
    guard, ...), some of which make real network calls that plain
    ``config._invoke_with_retry`` mocking doesn't intercept. Tests that
    specifically exercise local/compact-profile behavior already patch
    ``ACTIVE_PROFILE`` or the profile they pass explicitly, which still
    works fine layered on top of this baseline.
    """
    from wells import config

    monkeypatch.setattr(config, "ACTIVE_PROFILE", "zai")
