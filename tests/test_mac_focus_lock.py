"""mac/screen/driver.py focus lock: keyboard events carrying require_app /
require_window are refused (FocusMismatch) before their key-down when the
focused app/window differs; nothing posts after a refusal. Loads driver.py
fresh against fake mirroring modules, so it runs on any platform."""

from __future__ import annotations

import ctypes
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DRIVER_PATH = Path(__file__).resolve().parent.parent / "mac" / "screen" / "driver.py"


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGPoint)]


def _assert_pairs(posted: list) -> None:
    """Every key-down has its key-up, same keycode, nothing dangling."""
    assert len(posted) % 2 == 0, posted
    for i in range(0, len(posted), 2):
        down, up = posted[i], posted[i + 1]
        assert down[1] is True and up[1] is False, posted
        assert down[0] == up[0], posted


@pytest.fixture
def load_driver(monkeypatch):
    def _load(probe_returns: list) -> tuple:
        mirroring = types.ModuleType("mac.mirroring.driver")
        mirroring.UNICODE_CHUNK = 20
        mirroring.kCGEventLeftMouseDown = 1
        mirroring.kCGEventLeftMouseUp = 2
        mirroring.kCGEventMouseMoved = 5
        mirroring.kCGHIDEventTap = 0
        window = types.ModuleType("mac.mirroring.window")
        window.AX = MagicMock()
        window.CF = MagicMock()
        window.CG = MagicMock()
        window.CGPoint = CGPoint
        window.CGRect = CGRect
        window._cfstr = lambda s: s.encode("utf-8")
        window._cfstr_value = lambda v: v
        pkg = types.ModuleType("mac.mirroring")
        pkg.driver = mirroring
        pkg.window = window
        for name, mod in (("mac.mirroring", pkg),
                          ("mac.mirroring.driver", mirroring),
                          ("mac.mirroring.window", window)):
            monkeypatch.setitem(sys.modules, name, mod)
        # driver.py CDLLs ImageIO at import: fake it while loading
        monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: MagicMock())
        spec = importlib.util.spec_from_file_location("_focus_lock_driver",
                                                      DRIVER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        posted: list = []
        calls: list = []
        seq = list(probe_returns)

        def probe():
            calls.append(1)
            return seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]

        mod._real_focus_probe = mod._focus_probe  # kept for the probe's own test
        mod._focus_probe = probe
        mod._main_bounds = lambda: CGRect(CGPoint(0, 0), CGPoint(100, 100))
        mod.time = types.SimpleNamespace(sleep=lambda s: None)
        mod.AX.CGEventCreateKeyboardEvent = (
            lambda source, keycode, down: (keycode, down))
        mod.AX.CGEventPost = lambda tap, ev: posted.append(ev)
        return mod, posted, calls
    return _load


def test_focus_match_posts_all_chunks(load_driver):
    mod, posted, calls = load_driver([("Terminal", "zsh — 80x24")])
    text = "a" * 45  # UNICODE_CHUNK=20 code units -> 3 chunks
    mod.send_events([{"type": "text", "text": text, "require_app": "Terminal"}])
    assert len(posted) == 6  # 3 chunks x (down, up)
    assert len(calls) == 3   # one probe per chunk
    _assert_pairs(posted)


def test_focus_loss_mid_text_is_partial(load_driver):
    mod, posted, _ = load_driver([("Terminal", None), ("iTerm2", None)])
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([{"type": "text", "text": "b" * 45,
                          "require_app": "Terminal"}])
    assert exc.value.partial is True
    assert exc.value.index == 0
    assert exc.value.frontmost_app == "iTerm2"
    assert len(posted) == 2  # exactly the first chunk's down+up
    _assert_pairs(posted)


def test_focus_window_missing_refuses_before_first_chunk(load_driver):
    mod, posted, _ = load_driver([("Terminal", None)])
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([{"type": "text", "text": "hello",
                          "require_app": "Terminal",
                          "require_window": "zsh"}])
    assert posted == []  # refused: zero posts
    assert exc.value.partial is False
    assert exc.value.index == 0


def test_focus_window_substring_match_case_sensitive(load_driver):
    mod, posted, _ = load_driver([("Terminal", "zsh — 80x24")])
    mod.send_events([{"type": "text", "text": "ls",
                      "require_app": "Terminal",
                      "require_window": "zsh"}])
    assert len(posted) == 2
    _assert_pairs(posted)
    mod2, posted2, _ = load_driver([("Terminal", "zsh — 80x24")])
    with pytest.raises(mod2.FocusMismatch):
        mod2.send_events([{"type": "text", "text": "ls",
                           "require_app": "Terminal",
                           "require_window": "ZSH"}])
    assert posted2 == []


def test_focus_batch_stops_at_refused_event(load_driver):
    mod, posted, _ = load_driver([("Terminal", None), ("Notes", None)])
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([
            {"type": "text", "text": "ok", "require_app": "Terminal"},
            {"type": "key", "key": "return", "mods": [],
             "require_app": "Terminal"},
            {"type": "text", "text": "after", "require_app": "Terminal"},
        ])
    assert exc.value.index == 1
    assert len(posted) == 2  # first event's chunk posted, nothing after
    _assert_pairs(posted)


def test_focus_lock_absent_probe_never_called(load_driver):
    mod, posted, calls = load_driver([("Nobody", None)])
    mod.send_events([{"type": "key", "key": "return", "mods": [],
                      "require_app": None}])
    assert calls == []       # no require_app -> no probe
    assert len(posted) == 2
    _assert_pairs(posted)


def test_focus_unknown_probe_fails_closed(load_driver):
    mod, posted, _ = load_driver([(None, None)])
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([{"type": "key", "key": "return", "mods": [],
                          "require_app": "Terminal"}])
    assert posted == []  # a refused _key posts no key-down
    assert exc.value.frontmost_app is None
    assert exc.value.index == 0


def _fake_ax(mod, attrs: dict, titles: dict) -> list:
    """Wire the loaded module's AX/CF/_cfstr_value to a fake element tree.
    attrs: (element, attribute) -> copied value (missing -> AX error -25212)."""
    released: list = []

    def copy(el, key, out_ref):
        value = attrs.get((el, key.decode()))  # fake _cfstr returns bytes
        if value is None:
            return -25212  # kAXErrorNoValue
        out_ref._obj.value = value
        return 0

    mod.AX.AXUIElementCreateSystemWide = lambda: 1
    mod.AX.AXUIElementCopyAttributeValue = copy
    mod.CF.CFRelease = released.append
    assert "_cfstr_value" in vars(mod)  # driver must import it, not rely on this fake
    mod._cfstr_value = titles.get
    return released


def test_focus_probe_reads_titles_and_releases_everything(load_driver):
    mod, _, _ = load_driver([])
    released = _fake_ax(
        mod,
        {(1, "AXFocusedApplication"): 2, (2, "AXTitle"): 10,
         (2, "AXFocusedWindow"): 3, (3, "AXTitle"): 11},
        {10: "Terminal", 11: "zsh — 80x24"})
    assert mod._real_focus_probe() == ("Terminal", "zsh — 80x24")
    mod.AX.AXUIElementSetMessagingTimeout.assert_called_once_with(1, 1.0)
    handles = [r for r in released if isinstance(r, int)]
    assert sorted(handles) == [1, 2, 3, 10, 11]  # each copied/created object once
    keys = [r for r in released if not isinstance(r, int)]
    assert len(keys) == 4  # every _cfstr attribute key released too


def test_focus_probe_ax_error_is_none(load_driver):
    mod, _, _ = load_driver([])
    released = _fake_ax(mod, {}, {})
    assert mod._real_focus_probe() == (None, None)
    assert [r for r in released if isinstance(r, int)] == [1]


def test_focus_settles_after_earlier_event_before_first_check(load_driver):
    """[tap, locked text]: the settle sleep comes after the tap posts and
    before the first focus check / keyboard post."""
    mod, posted, calls = load_driver([("Terminal", None)])
    mod.time = types.SimpleNamespace(sleep=lambda s: posted.append(("sleep", s)))
    mod._focus_probe = lambda: (posted.append("probe"), ("Terminal", None))[1]
    mod.send_events([{"type": "tap", "x": 0.5, "y": 0.5},
                     {"type": "text", "text": "ls", "require_app": "Terminal"}])
    settle = posted.index(("sleep", mod.FOCUS_SETTLE_S))
    first_key = next(i for i, p in enumerate(posted) if p in ((0, True), (0, False)))
    mouse_posts = [i for i, p in enumerate(posted)
                   if not isinstance(p, (tuple, str))]  # mouse CGEvents are MagicMocks
    assert mouse_posts and max(mouse_posts) < settle < posted.index("probe") < first_key


def test_focus_no_settle_for_first_or_unlocked_event(load_driver):
    mod, posted, _ = load_driver([("Terminal", None)])
    mod.time = types.SimpleNamespace(sleep=lambda s: posted.append(("sleep", s)))
    mod.send_events([{"type": "text", "text": "a", "require_app": "Terminal"},
                     {"type": "key", "key": "return", "mods": []}])
    assert ("sleep", mod.FOCUS_SETTLE_S) not in posted
