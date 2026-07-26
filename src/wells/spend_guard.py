"""spend_guard: a cross-run daily spend cap.

``MAX_RUN_TOKENS`` already hard-caps *one* run — but nothing caps
cumulative spend across a day of repeated or scheduled runs. That's a real
gap the scheduling feature (``wells schedule``) opened up: a goal that
fires every 15 minutes unattended, if it gets stuck retrying or is just
more expensive than expected, can quietly burn real money with no
guardrail at all between runs.

This tracks a simple rolling **daily** total in ``~/.wells/spend.json``
(global — not per-workspace, since a personal budget is naturally "how
much do I spend today across everything," not per-project) and refuses to
*start* a new run once ``WELLS_DAILY_BUDGET`` (dollars) is reached. Unset
or ``0`` (the default) means no cap — this is fully opt-in.

Not a token-level circuit breaker mid-run — the check happens once, at
run start. A single run's cost still isn't bounded by this (that's what
``MAX_RUN_TOKENS`` is for); this stops the *next* run from starting once
the day's budget is spent.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_SPEND_FILE = Path.home() / ".wells" / "spend.json"


def _spend_file() -> Path:
    override = os.environ.get("WELLS_SPEND_FILE", "").strip()
    return Path(override) if override else _SPEND_FILE


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load() -> dict:
    path = _spend_file()
    if not path.exists():
        return {"date": _today(), "spent": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"date": _today(), "spent": 0.0}
    if not isinstance(data, dict) or data.get("date") != _today():
        return {"date": _today(), "spent": 0.0}  # a new day rolls the total over
    try:
        return {"date": data["date"], "spent": float(data.get("spent", 0.0))}
    except Exception:
        return {"date": _today(), "spent": 0.0}


def _save(data: dict) -> None:
    path = _spend_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass  # best-effort -- losing a spend-tracking write must never break a run


def today_spend() -> float:
    """Dollars spent so far today, across every workspace/run."""
    return _load()["spent"]


def add_spend(dollars: float) -> float:
    """Add ``dollars`` to today's running total; returns the new total.

    Never raises — called from the end of every run (headless and TUI),
    so a filesystem hiccup here must not surface as a run failure.
    """
    if dollars is None or dollars <= 0:
        return today_spend()
    data = _load()
    data["spent"] = data.get("spent", 0.0) + dollars
    _save(data)
    return data["spent"]


def daily_budget() -> float:
    """The configured daily cap in dollars (0 = unlimited, the default)."""
    try:
        return float(os.environ.get("WELLS_DAILY_BUDGET", "0") or "0")
    except ValueError:
        return 0.0


def budget_exceeded() -> bool:
    """True when a budget is configured AND today's spend has reached it."""
    b = daily_budget()
    return b > 0 and today_spend() >= b


def remaining_budget() -> float | None:
    """Dollars left today, or None when no budget is configured."""
    b = daily_budget()
    if b <= 0:
        return None
    return max(0.0, b - today_spend())


def budget_message() -> str:
    """Human-readable refusal message when the budget is already spent."""
    return (
        f"Daily budget of ${daily_budget():.2f} already reached "
        f"(${today_spend():.2f} spent today). Increase WELLS_DAILY_BUDGET, "
        f"or wait until tomorrow ({_today()} resets at midnight local time)."
    )
