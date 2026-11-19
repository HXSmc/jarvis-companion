"""Tests for mac.mirroring.driver's swipe: the moves while the button is held
must be kCGEventLeftMouseDragged (a held kCGEventMouseMoved reads as a hover),
and the CLI posts nothing unless Mirroring is frontmost and the process is
Accessibility-trusted. Runs off macOS: mac.mirroring.window is a stand-in
module and every CG call is faked (same approach as test_mac_screen_lock)."""

from __future__ import annotations

import ctypes
import importlib.util
import sys
import types
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock

import pytest

DRIVER_PATH = Path(__file__).resolve().parent.parent / "mac" / "mirroring" / "driver.py"
MOVED, LDOWN, LUP, DRAGGED = 5, 1, 2, 6
NAMES = {MOVED: "MOVED", LDOWN: "LDOWN", LUP: "LUP", DRAGGED: "DRAGGED"}


class CGPoint:
    def __init__(self, x=0.0, y=0.0):
        self.x, self.y = x, y


@pytest.fixture
def load(monkeypatch):
    def _load(frontmost_pid=42, trusted=True):
        win_mod = types.ModuleType("mac.mirroring.window")
        win_mod.AX, win_mod.CF, win_mod.CG = MagicMock(), MagicMock(), MagicMock()
        win_mod.CGPoint, win_mod.CGRect = CGPoint, MagicMock()
        MirrorWindow = namedtuple("MirrorWindow", "window_id pid frame")
        win = MirrorWindow(7, 42, (0.0, 0.0, 390.0, 844.0))
        win_mod.MirrorWindow = MirrorWindow
        win_mod.FAKE_WINDOW = MirrorWindow(0, 0, (0.0, 0.0, 390.0, 844.0))
        win_mod.MirroringWindowNotFoundError = type("NotFound", (Exception,), {})
        win_mod.find_mirroring_window = lambda: win
        win_mod.to_screen = lambda frame, x, y: (frame[0] + x, frame[1] + y)
        win_mod._enum_windows = lambda: [{"layer": 0, "pid": frontmost_pid}]
        pkg = types.ModuleType("mac.mirroring")
        pkg.window = win_mod
        for name, mod in (("mac.mirroring", pkg), ("mac.mirroring.window", win_mod)):
            monkeypatch.setitem(sys.modules, name, mod)
        monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: MagicMock())
        spec = importlib.util.spec_from_file_location("_mirroring_swipe_driver", DRIVER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        log = []
        mod.time = types.SimpleNamespace(sleep=lambda s: None)
        origin = CGPoint(11, 22)
        mod.AX.CGEventCreate = lambda src: "current"
        mod.AX.CGEventGetLocation = lambda ev: origin
        mod.AX.CGEventCreateMouseEvent = lambda src, kind, p, btn: (kind, p.x, p.y, btn)
        mod.AX.CGEventPost = lambda tap, ev: log.append(("post", tap) + ev)
        mod.AX.CGWarpMouseCursorPosition = lambda p: log.append(("warp", p.x, p.y))
        mod.AX.AXIsProcessTrusted = lambda: trusted
        return mod, log
    return _load


def test_swipe_posts_dragged_while_held(load):
    mod, log = load()
    mod.swipe(100, 200, 300, 200)
    posts = [e for e in log if e[0] == "post"]
    assert all(e[1] == 0 for e in posts)  # every post at tap kCGHIDEventTap
    seq = [f"{NAMES[e[2]]}@({e[3]:g},{e[4]:g})" for e in posts]
    assert seq == ["MOVED@(100,200)", "LDOWN@(100,200)", "DRAGGED@(150,200)",
                   "DRAGGED@(200,200)", "DRAGGED@(250,200)", "DRAGGED@(300,200)",
                   "LUP@(300,200)"]
    assert [e[2] for e in posts] == [5, 1, 6, 6, 6, 6, 2]
    down = seq.index("LDOWN@(100,200)")
    assert not any(s.startswith("MOVED") for s in seq[down:])
    assert log[len(posts):] == [("warp", 11, 22)]


def test_swipe_cli_refuses_when_not_frontmost(load):
    mod, log = load(frontmost_pid=99)
    assert mod.main(["--swipe", "10,10,20,20"]) == 3
    assert [e for e in log if e[0] == "post"] == []


def test_swipe_cli_refuses_when_untrusted(load):
    mod, log = load(trusted=False)
    assert mod.main(["--swipe", "10,10,20,20"]) == 3
    assert [e for e in log if e[0] == "post"] == []
