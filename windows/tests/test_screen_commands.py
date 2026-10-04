"""Tests for the screen-control commands (SC3).

Runs on the Mac: ImageGrab / SendInput / startfile are all mocked — only the
struct math, clamps and name validation run for real.
"""

from __future__ import annotations

import base64
import sys
import types

from windows.agent import commands


def test_allowlist_gains_the_three_screen_commands():
    assert {"screen_frame", "screen_input", "open"} <= set(commands.ALLOWED)
    assert set(commands._HANDLERS) == set(commands.ALLOWED)


class _FakeImage:
    """Just enough PIL.Image: grab->thumbnail->save(jpeg) with recorded args."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.thumbnail_target = None
        self.saved = None

    def thumbnail(self, target):
        self.thumbnail_target = target
        tw = min(self.width, target[0])
        self.height = int(self.height * tw / self.width)
        self.width = tw

    def save(self, buf, format, quality):
        self.saved = (format, quality)
        buf.write(b"fake-jpeg")


def test_screen_frame_returns_base64_and_dims(monkeypatch):
    img = _FakeImage(3840, 2160)
    fake_pil = types.ModuleType("PIL")
    fake_pil.ImageGrab = types.SimpleNamespace(grab=lambda: img)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)  # Pillow is win32-only, absent on the Mac
    ok, data = commands.run_command("screen_frame", {"w": 1170, "q": 55})
    assert ok is True
    assert img.thumbnail_target == (1170, 10**9)
    assert img.saved == ("JPEG", 55)
    assert data["jpeg"] == base64.b64encode(b"fake-jpeg").decode()
    assert data["w"] == 1170
    assert data["h"] == 658  # 2160 * 1170/3840, aspect kept


def test_screen_input_builds_scaled_wheel_unicode_and_mod_structs(monkeypatch):
    batches = []
    monkeypatch.setattr(commands, "_send_inputs", lambda structs: batches.append(structs) or len(structs))
    ok, data = commands.run_command(
        "screen_input",
        {
            "events": [
                {"type": "move", "x": 0.5, "y": 0.25},
                {"type": "scroll", "dy": -0.5},
                {"type": "text", "text": "A"},
                {"type": "key", "key": "return", "mods": ["ctrl"]},
            ]
        },
    )
    assert ok is True
    structs = batches[0]

    move = structs[0]
    assert move.type == commands.INPUT_MOUSE
    assert move.u.mi.dx == 32767 and move.u.mi.dy == 16383  # int(n * 65535)
    assert move.u.mi.dwFlags == commands.MOUSEEVENTF_MOVE | commands.MOUSEEVENTF_ABSOLUTE

    wheel = structs[1]
    assert wheel.u.mi.dwFlags == commands.MOUSEEVENTF_WHEEL
    assert wheel.u.mi.mouseData == -60  # int(dy * 120), one notch

    kd, ku = structs[2], structs[3]  # text: KEYEVENTF_UNICODE down/up per UTF-16 unit
    assert kd.type == ku.type == commands.INPUT_KEYBOARD
    assert kd.u.ki.wVk == 0 and kd.u.ki.wScan == ord("A")
    assert kd.u.ki.dwFlags == commands.KEYEVENTF_UNICODE
    assert ku.u.ki.dwFlags == commands.KEYEVENTF_UNICODE | commands.KEYEVENTF_KEYUP

    # key + mods: ctrl down, return down, return up, ctrl up — mods wrap the key
    vks = [s.u.ki.wVk for s in structs[4:]]
    assert vks == [
        commands.VK_MODS["ctrl"],
        commands.VK["return"],
        commands.VK["return"],
        commands.VK_MODS["ctrl"],
    ]
    flags = [s.u.ki.dwFlags for s in structs[4:]]
    assert flags == [0, 0, commands.KEYEVENTF_KEYUP, commands.KEYEVENTF_KEYUP]


def test_open_dispatches_to_startfile_only(monkeypatch):
    opened = []
    monkeypatch.setattr(commands.os, "startfile", opened.append, raising=False)
    ok, data = commands.run_command("open", {"name": "notepad.exe"})
    assert ok is True
    assert opened == ["notepad.exe"]
    assert data == "opened notepad.exe"


def test_open_rejects_injection_names(monkeypatch):
    fired = []
    monkeypatch.setattr(commands.os, "startfile", fired.append, raising=False)
    for name in ("a;b", "a|b", "../x", "a\\b", "x; rm -rf"):
        ok, data = commands.run_command("open", {"name": name})
        assert ok is False, name
        assert data == "error: invalid app name", name
    assert fired == []  # nothing dispatched
