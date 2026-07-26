"""Tests for wells.spend_guard: cross-run daily spend cap."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from wells import spend_guard


@pytest.fixture(autouse=True)
def _isolated_spend_file(tmp_path, monkeypatch):
    path = tmp_path / "spend.json"
    monkeypatch.setenv("WELLS_SPEND_FILE", str(path))
    monkeypatch.delenv("WELLS_DAILY_BUDGET", raising=False)
    return path


def test_today_spend_zero_when_no_file():
    assert spend_guard.today_spend() == 0.0


def test_add_spend_accumulates():
    spend_guard.add_spend(0.60)
    total = spend_guard.add_spend(0.50)
    assert total == pytest.approx(1.10)
    assert spend_guard.today_spend() == pytest.approx(1.10)


def test_add_spend_ignores_none_and_nonpositive():
    assert spend_guard.add_spend(None) == 0.0
    assert spend_guard.add_spend(0) == 0.0
    assert spend_guard.add_spend(-5) == 0.0
    assert spend_guard.today_spend() == 0.0


def test_daily_budget_default_zero_unlimited(monkeypatch):
    monkeypatch.delenv("WELLS_DAILY_BUDGET", raising=False)
    assert spend_guard.daily_budget() == 0.0


def test_daily_budget_reads_env(monkeypatch):
    monkeypatch.setenv("WELLS_DAILY_BUDGET", "2.50")
    assert spend_guard.daily_budget() == pytest.approx(2.50)


def test_daily_budget_invalid_value_treated_as_zero(monkeypatch):
    monkeypatch.setenv("WELLS_DAILY_BUDGET", "not-a-number")
    assert spend_guard.daily_budget() == 0.0


def test_budget_exceeded_false_when_unlimited(monkeypatch):
    monkeypatch.delenv("WELLS_DAILY_BUDGET", raising=False)
    spend_guard.add_spend(999.0)
    assert spend_guard.budget_exceeded() is False


def test_budget_exceeded_true_once_reached(monkeypatch):
    monkeypatch.setenv("WELLS_DAILY_BUDGET", "1.00")
    spend_guard.add_spend(0.60)
    assert spend_guard.budget_exceeded() is False
    spend_guard.add_spend(0.50)
    assert spend_guard.budget_exceeded() is True


def test_budget_exceeded_at_exact_boundary(monkeypatch):
    monkeypatch.setenv("WELLS_DAILY_BUDGET", "1.00")
    spend_guard.add_spend(1.00)
    assert spend_guard.budget_exceeded() is True


def test_remaining_budget_none_when_unlimited(monkeypatch):
    monkeypatch.delenv("WELLS_DAILY_BUDGET", raising=False)
    assert spend_guard.remaining_budget() is None


def test_remaining_budget_computed(monkeypatch):
    monkeypatch.setenv("WELLS_DAILY_BUDGET", "5.00")
    spend_guard.add_spend(2.00)
    assert spend_guard.remaining_budget() == pytest.approx(3.00)


def test_remaining_budget_floors_at_zero(monkeypatch):
    monkeypatch.setenv("WELLS_DAILY_BUDGET", "1.00")
    spend_guard.add_spend(5.00)
    assert spend_guard.remaining_budget() == 0.0


def test_budget_message_format(monkeypatch):
    monkeypatch.setenv("WELLS_DAILY_BUDGET", "1.00")
    spend_guard.add_spend(1.00)
    msg = spend_guard.budget_message()
    assert "$1.00" in msg
    assert "WELLS_DAILY_BUDGET" in msg


def test_day_rollover_resets_total(_isolated_spend_file):
    stale = {"date": "2000-01-01", "spent": 500.0}
    _isolated_spend_file.parent.mkdir(parents=True, exist_ok=True)
    _isolated_spend_file.write_text(json.dumps(stale), encoding="utf-8")
    assert spend_guard.today_spend() == 0.0


def test_corrupt_file_treated_as_zero(_isolated_spend_file):
    _isolated_spend_file.parent.mkdir(parents=True, exist_ok=True)
    _isolated_spend_file.write_text("not json{{{", encoding="utf-8")
    assert spend_guard.today_spend() == 0.0


def test_non_dict_file_treated_as_zero(_isolated_spend_file):
    _isolated_spend_file.parent.mkdir(parents=True, exist_ok=True)
    _isolated_spend_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert spend_guard.today_spend() == 0.0


def test_add_spend_persists_across_calls(_isolated_spend_file, monkeypatch):
    spend_guard.add_spend(0.25)
    data = json.loads(_isolated_spend_file.read_text(encoding="utf-8"))
    assert data["spent"] == pytest.approx(0.25)
    assert data["date"] == time.strftime("%Y-%m-%d")
