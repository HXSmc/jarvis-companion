"""Tests for the allowlisted command runner.

Runs on the Mac: psutil-backed commands are tested for real, Windows-only
paths (PowerShell CIM, wake key, suspend) are mocked.
"""

from __future__ import annotations

import time

import psutil
import pytest

from windows.agent import commands
from windows.agent import main as agent_main


# --- refusal: the one security property this module exists for -------------

def test_refuses_non_allowlisted_command_without_execution(monkeypatch):
    fired = []
    sentinel_handlers = {
        name: (lambda payload, n=name: fired.append(n)) for name in commands.ALLOWED
    }
    monkeypatch.setattr(commands, "_HANDLERS", sentinel_handlers)
    ok, data = commands.run_command("format c:")
    assert ok is False
    assert data == "refused"
    assert fired == []  # nothing dispatched

    # Same refusal over the wire envelope: `{id, ok: false, data: "refused"}`.
    reply = agent_main.handle_message('{"id": 7, "type": "command", "payload": {"cmd": "shutdown /s"}}')
    assert reply == {"id": 7, "ok": False, "data": "refused"}
    assert fired == []


def test_refuses_none_and_empty_command_names():
    assert commands.run_command(None) == (False, "refused")
    assert commands.run_command("") == (False, "refused")


def test_allowlist_is_exactly_the_seven_commands():
    assert commands.ALLOWED == (
        "specs", "uptime", "disk", "processes", "wake", "sleep", "list_dir",
    )
    assert set(commands._HANDLERS) == set(commands.ALLOWED)


# --- happy paths -----------------------------------------------------------

def test_specs_happy_path_on_mac(monkeypatch):
    monkeypatch.setattr(commands, "IS_WINDOWS", False)
    ok, data = commands.run_command("specs")
    assert ok is True
    assert data["ram_total_gb"] == pytest.approx(
        psutil.virtual_memory().total / 1024**3, abs=0.2
    )
    assert data["cpu_threads"] == psutil.cpu_count(logical=True)
    assert "motherboard" not in data  # PowerShell fallback is Windows-gated


def test_specs_powershell_fallback_on_windows(monkeypatch):
    monkeypatch.setattr(commands, "IS_WINDOWS", True)
    calls = []

    def fake_ps(cmd):
        calls.append(cmd)
        return "Contoso  X99 Pro"

    monkeypatch.setattr(commands, "_powershell", fake_ps)
    ok, data = commands.run_command("specs")
    assert ok is True
    assert calls and "Win32_BaseBoard" in calls[0]
    assert data["motherboard"] == "Contoso X99 Pro"


def test_uptime_happy_path():
    ok, data = commands.run_command("uptime")
    assert ok is True
    expected = int(time.time() - psutil.boot_time())
    assert abs(data["uptime_sec"] - expected) <= 5


def test_disk_happy_path():
    ok, data = commands.run_command("disk")
    assert ok is True
    assert data["total_gb"] > 0
    assert 0 <= data["used_percent"] <= 100
    # On APFS used+free != total (purgeable space), so only sanity-check bounds.
    assert data["used_gb"] <= data["total_gb"]


def test_processes_happy_path():
    ok, data = commands.run_command("processes")
    assert ok is True
    assert data["count"] >= 1
    assert 1 <= len(data["top"]) <= commands.PROCESS_LIMIT
    for entry in data["top"]:
        assert isinstance(entry["pid"], int)
        assert entry["name"]
    rss = [e["rss_mb"] for e in data["top"]]
    assert rss == sorted(rss, reverse=True)


def test_wake_happy_path_windows_mocked(monkeypatch):
    monkeypatch.setattr(commands, "IS_WINDOWS", True)
    fired = []
    monkeypatch.setattr(commands, "_send_wake_key", lambda: fired.append("wake"))
    ok, data = commands.run_command("wake")
    assert ok is True
    assert data == "wake sent"
    assert fired == ["wake"]


def test_sleep_happy_path_windows_mocked(monkeypatch):
    monkeypatch.setattr(commands, "IS_WINDOWS", True)
    fired = []
    monkeypatch.setattr(commands, "_suspend", lambda: fired.append("sleep"))
    ok, data = commands.run_command("sleep")
    assert ok is True
    assert data == "sleep requested"
    assert fired == ["sleep"]


def test_list_dir_happy_path_default_home(monkeypatch, tmp_path):
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setattr(commands, "_home", lambda: str(tmp_path))
    ok, data = commands.run_command("list_dir")
    assert ok is True
    assert data["path"] == str(tmp_path)
    assert data["entries"] == ["a.txt", "b.txt"]


def test_list_dir_rejects_paths_outside_home(monkeypatch):
    monkeypatch.setattr(commands, "_home", lambda: "/Users/someone")
    ok, data = commands.run_command("list_dir", "/etc")
    assert ok is False
    assert data.startswith("error:")


# --- envelope handling -----------------------------------------------------

def test_handle_message_replies_to_command_envelope():
    reply = agent_main.handle_message('{"id": "x1", "type": "command", "payload": "uptime"}')
    assert reply["id"] == "x1"
    assert reply["ok"] is True
    assert "uptime_sec" in reply["data"]


def test_handle_message_ignores_non_command_and_garbage():
    assert agent_main.handle_message('{"id": 1, "type": "wake_event", "payload": {}}') is None
    assert agent_main.handle_message("not json") is None
