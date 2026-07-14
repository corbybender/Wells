"""Tests for the tool-call log used by /log (logger.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from wells import logger


def test_tail_tool_log_missing_file_returns_empty(tmp_path: Path):
    with patch.object(logger, "_get_tool_log_path", return_value=tmp_path / "tools.log"):
        assert logger.tail_tool_log() == []


def test_tail_tool_log_splits_and_limits_entries(tmp_path: Path):
    # Mirrors the real format written by log_tool_result: each record is
    # "<asctime> === <STATUS> <name> args=<repr> ===\n<full text>\n".
    log_path = tmp_path / "tools.log"
    log_path.write_text(
        "2026-07-14 12:00:00,100 === OK list_dir args={'path': '.'} ===\n"
        "first entry body\n"
        "2026-07-14 12:00:00,200 === FAIL run_command args={'command': 'pip install x'} ===\n"
        "second entry body\n"
        "2026-07-14 12:00:00,300 === OK read_file args={'path': 'a.py'} ===\n"
        "third entry body\n",
        encoding="utf-8",
    )
    with patch.object(logger, "_get_tool_log_path", return_value=log_path):
        all_entries = logger.tail_tool_log(10)
        assert len(all_entries) == 3
        assert "first entry body" in all_entries[0]
        assert "second entry body" in all_entries[1]
        assert "third entry body" in all_entries[2]

        last_one = logger.tail_tool_log(1)
        assert len(last_one) == 1
        assert "third entry body" in last_one[0]
        assert "second entry body" not in last_one[0]
