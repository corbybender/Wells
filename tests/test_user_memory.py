"""Tests for global cross-project user memory: discovery, parsing, CRUD,
prompt injection, and the WELLS_USER_MEMORY toggle."""

from __future__ import annotations

from pathlib import Path

import pytest

from wells import user_memory as um


@pytest.fixture(autouse=True)
def isolated_memory_dir(tmp_path: Path, monkeypatch):
    """Point _memory_dir() at a per-test tmp_path instead of the real
    ~/.wells/memory/ -- these tests must never touch the developer's actual
    global memory store."""
    monkeypatch.setenv("WELLS_USER_MEMORY_DIR", str(tmp_path / "memory"))
    return tmp_path / "memory"


def _write(path: Path, name: str, description: str, entry_type: str, body: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {entry_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Discovery + parsing
# ---------------------------------------------------------------------------


def test_no_entries_by_default():
    assert um.memories().is_empty()


def test_discovers_entry(isolated_memory_dir: Path):
    _write(isolated_memory_dir, "terse-commits", "Prefers terse commits.", "feedback", "Keep it short.")
    idx = um.memories()
    e = idx.by_name("terse-commits")
    assert e is not None
    assert e.description == "Prefers terse commits."
    assert e.type == "feedback"
    assert "Keep it short." in e.body


def test_by_name_case_insensitive(isolated_memory_dir: Path):
    _write(isolated_memory_dir, "abc", "d", "user", "b")
    assert um.memories().by_name("ABC") is not None


def test_name_defaults_to_filename(isolated_memory_dir: Path):
    isolated_memory_dir.mkdir(parents=True)
    (isolated_memory_dir / "no-name-field.md").write_text(
        "---\ndescription: no name given.\n---\nBody.\n", encoding="utf-8"
    )
    assert um.memories().by_name("no-name-field") is not None


def test_reserved_memory_md_index_file_ignored(isolated_memory_dir: Path):
    """A MEMORY.md file (reserved for a future plain index) is not itself a
    discoverable entry, even though it matches *.md."""
    isolated_memory_dir.mkdir(parents=True)
    (isolated_memory_dir / "MEMORY.md").write_text("not an entry", encoding="utf-8")
    assert um.memories().is_empty()


def test_disabled_via_env(isolated_memory_dir: Path, monkeypatch):
    _write(isolated_memory_dir, "x", "d", "user", "b")
    monkeypatch.setenv("WELLS_USER_MEMORY", "0")
    assert um.memories().is_empty()
    assert not um.enabled()


def test_entries_sorted_oldest_first_by_mtime(isolated_memory_dir: Path):
    import time

    _write(isolated_memory_dir, "first", "d1", "user", "b1")
    time.sleep(0.02)
    _write(isolated_memory_dir, "second", "d2", "user", "b2")
    names = [e.name for e in um.memories().entries]
    assert names == ["first", "second"]


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def test_memory_block_empty_when_no_entries():
    assert um.memory_block() == ""


def test_memory_block_includes_full_body(isolated_memory_dir: Path):
    _write(isolated_memory_dir, "x", "desc", "feedback", "The actual standing fact.")
    block = um.memory_block()
    assert "USER MEMORY" in block
    assert "The actual standing fact." in block
    assert "x" in block


def test_inject_into_prompt_noop_when_empty():
    assert um.inject_into_prompt("BASE") == "BASE"


def test_inject_into_prompt_prepends_block(isolated_memory_dir: Path):
    _write(isolated_memory_dir, "x", "d", "user", "fact here")
    out = um.inject_into_prompt("BASE PROMPT")
    assert out.endswith("BASE PROMPT")
    assert "fact here" in out
    assert out.index("fact here") < out.index("BASE PROMPT")


def test_memory_block_budget_caps_total_size(isolated_memory_dir: Path, monkeypatch):
    monkeypatch.setattr(um, "_MAX_INJECT_CHARS", 200)
    for i in range(10):
        _write(isolated_memory_dir, f"entry-{i}", f"d{i}", "user", "x" * 100)
    block = um.memory_block()
    assert len(block) < 2000  # nowhere near all 10 entries fit
    assert "omitted" in block


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expect_err",
    [
        ("", True),
        ("Bad_Name", True),
        ("-leading", True),
        ("trailing-", True),
        ("double--hyphen", True),
        ("valid-name", False),
    ],
)
def test_validate_name(name, expect_err):
    err = um.validate_name(name)
    assert (err is not None) == expect_err


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_memory(isolated_memory_dir: Path):
    ok, msg = um.create_memory("x", "desc", "feedback", "body text")
    assert ok, msg
    e = um.memories().by_name("x")
    assert e is not None
    assert e.type == "feedback"
    assert e.body == "body text"


def test_create_memory_rejects_bad_name():
    ok, msg = um.create_memory("Bad Name", "d", "u", "b")
    assert not ok


def test_create_memory_refuses_duplicate(isolated_memory_dir: Path):
    um.create_memory("x", "d", "u", "b")
    ok, msg = um.create_memory("x", "d2", "u", "b2")
    assert not ok
    assert "already exists" in msg


def test_update_memory_description_only(isolated_memory_dir: Path):
    um.create_memory("x", "old desc", "user", "body")
    ok, msg = um.update_memory("x", description="new desc")
    assert ok, msg
    e = um.memories().by_name("x")
    assert e.description == "new desc"
    assert e.body == "body"  # unchanged


def test_update_memory_body(isolated_memory_dir: Path):
    um.create_memory("x", "d", "user", "old body")
    ok, msg = um.update_memory("x", body="new body")
    assert ok, msg
    assert um.memories().by_name("x").body == "new body"


def test_update_unknown_memory_fails():
    ok, msg = um.update_memory("nope", description="x")
    assert not ok


def test_delete_memory(isolated_memory_dir: Path):
    um.create_memory("x", "d", "u", "b")
    ok, msg = um.delete_memory("x")
    assert ok, msg
    assert um.memories().is_empty()


def test_delete_unknown_memory_fails():
    ok, msg = um.delete_memory("nope")
    assert not ok


def test_read_memory_raw(isolated_memory_dir: Path):
    um.create_memory("x", "d", "feedback", "body text")
    ok, raw = um.read_memory_raw("x")
    assert ok
    assert "name: x" in raw
    assert "type: feedback" in raw


def test_read_memory_raw_unknown():
    ok, raw = um.read_memory_raw("nope")
    assert not ok


# ---------------------------------------------------------------------------
# Wired into executor.py's system prompt (every agent role, not gated on
# skills/personas' compact-mode skip)
# ---------------------------------------------------------------------------


def test_injected_into_system_prompt_even_in_compact_mode(isolated_memory_dir: Path):
    from wells import executor

    um.create_memory("x", "d", "user", "a standing fact")
    out = executor._system_prompt("task", [], plan_mode=False, compact=True)
    assert "a standing fact" in out


def test_injected_into_system_prompt_normal_mode(isolated_memory_dir: Path):
    from wells import executor

    um.create_memory("x", "d", "user", "a standing fact")
    out = executor._system_prompt("task", [], plan_mode=False, compact=False)
    assert "a standing fact" in out
