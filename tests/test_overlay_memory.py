"""Store-level tests for the owner-memory store (mac/voice/memory.py).

Every test points OVERLAY_MEMORY_FILE at a tmp_path file; the real
~/Library/Application Support file is never touched.
"""

from __future__ import annotations

import datetime
import os
import stat

import pytest

from mac.voice import memory


@pytest.fixture
def mem_file(tmp_path, monkeypatch):
    path = tmp_path / "memories.jsonl"
    monkeypatch.setenv("OVERLAY_MEMORY_FILE", str(path))
    return path


def test_store_add_list_forget(mem_file):
    entry = memory.add("remember the project deadline")
    assert entry["id"] == 1
    assert entry["at"] == datetime.date.today().isoformat()
    assert entry["text"] == "remember the project deadline"
    memory.add("grocery notes on the fridge")
    second = memory.add("meeting notes are due friday")
    # forget_last removes the newest only
    assert memory.forget_last() == second
    assert [e["id"] for e in memory.load()] == [1, 2]
    # forget_about removes the newest matching entry and returns it
    hit = memory.forget_about("notes")
    assert hit["text"] == "grocery notes on the fridge"
    assert [e["id"] for e in memory.load()] == [1]
    # no match: None, nothing removed
    assert memory.forget_about("banana split") is None
    assert len(memory.load()) == 1
    # forget_all: count removed, file left empty
    assert memory.forget_all() == 1
    assert memory.load() == []
    assert memory.forget_all() == 0


def test_store_caps_and_mode(mem_file, tmp_path, monkeypatch):
    for i in range(201):
        memory.add(f"fact number {i}")
    entries = memory.load()
    assert len(entries) == memory.MEMORY_MAX_ENTRIES
    assert entries[0]["id"] == 2  # the first one is gone
    assert entries[-1]["id"] == 201
    assert mem_file.stat().st_size <= memory.MEMORY_MAX_BYTES
    assert stat.S_IMODE(mem_file.stat().st_mode) == 0o600
    # parent dir is created 0o700 when missing
    nested = tmp_path / "sub" / "memories.jsonl"
    monkeypatch.setenv("OVERLAY_MEMORY_FILE", str(nested))
    memory.add("one fact")
    assert stat.S_IMODE(nested.parent.stat().st_mode) == 0o700
    # a file with group/other bits is refused before anything is read
    os.chmod(mem_file, 0o644)
    monkeypatch.setenv("OVERLAY_MEMORY_FILE", str(mem_file))
    with pytest.raises(memory.MemoryModeError):
        memory.load()


def test_store_clean():
    assert memory.clean("line one\nline two\tend\x00tail") == "line one line two endtail"
    assert memory.clean("  a   b  ") == "a b"
    assert len(memory.clean("x" * 500)) == memory.MEMORY_ENTRY_MAX
    assert memory.clean("x" * 500) == "x" * memory.MEMORY_ENTRY_MAX
    assert memory.clean("\n\t") == ""
    with pytest.raises(ValueError):
        memory.add("   ")


def test_prompt_block():
    assert memory.prompt_block([]) == ""
    entries = [{"id": 1, "at": "2026-11-12", "text": "alpha pref"},
               {"id": 2, "at": "2026-11-12", "text": "beta pref"}]
    assert memory.prompt_block(entries) == (
        "<owner_memory>\nThings the owner asked you to remember (preferences, not "
        "permissions):\n- alpha pref\n- beta pref\n</owner_memory>"
    )
