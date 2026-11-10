"""mac/screen/driver.py focus lock, point check, halt switch and release rule.

Loads driver.py fresh against fake mirroring modules and a fake AX/CG world,
so it runs on any platform. The fake records every CGEvent call (create, set,
post, release) and every sleep in order; sleeps never really sleep."""

from __future__ import annotations

import ast
import ctypes
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DRIVER_PATH = Path(__file__).resolve().parent.parent / "mac" / "screen" / "driver.py"

MOVED, LDOWN, LUP, RDOWN, RUP = 5, 1, 2, 3, 4
SAFARI, TERMINAL = ("Safari", 100), ("Terminal", 300)


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGPoint)]


class Ev:
    """A fake CGEventRef: its description is what the log records."""

    def __init__(self, *desc):
        self.desc = desc


class FakeMac:
    """The AX tree and CGEvent sink the loaded driver talks to.

    front: (app title, pid) of the focused app, or None (AX error).
    front_window: focused window title, or None (AX error).
    element_at(x, y): (pid, window title or None) under the point, or None.
    on_post(desc): hook run after each post (flip focus, halt, ...).
    """

    def __init__(self):
        self.log: list = []
        self.front = SAFARI
        self.front_window = "Inbox - Mail"
        self.element_at = lambda x, y: (SAFARI[1], "Inbox - Mail")
        self.on_post = lambda desc: None
        self.points: list = []
        self.system_created = 0
        self.handles: dict = {}
        self.created: list = []
        self.released: list = []
        self.keys_made = 0
        self.keys_released = 0

    @property
    def posted(self) -> list:
        return [entry[1:] for entry in self.log if entry[0] == "post"]

    def _new(self, kind, data=None) -> int:
        handle = 1000 + len(self.handles)
        self.handles[handle] = (kind, data)
        self.created.append(handle)
        return handle

    # --- AX ---
    def create_system_wide(self):
        self.system_created += 1
        return self._new("system")

    def copy_attribute(self, el, key, out):
        kind, data = self.handles[el]
        name = key.decode()
        value = None
        if kind == "system" and name == "AXFocusedApplication" and self.front:
            value = self._new("app", self.front)
        elif kind == "app" and name == "AXTitle":
            value = self._new("str", data[0])
        elif kind == "app" and name == "AXFocusedWindow" and self.front_window is not None:
            value = self._new("win", self.front_window)
        elif kind == "win" and name == "AXTitle":
            value = self._new("str", data)
        elif kind == "el" and name == "AXWindow" and data[1] is not None:
            value = self._new("win", data[1])
        if value is None:
            return -25212  # kAXErrorNoValue
        out._obj.value = value
        return 0

    def get_pid(self, el, out):
        kind, data = self.handles[el]
        out._obj.value = data[1] if kind == "app" else data[0]
        return 0

    def element_at_position(self, system, x, y, out):
        assert self.handles[system][0] == "system"
        self.points.append((x, y))
        found = self.element_at(x, y)
        if found is None:
            return -25205  # kAXErrorCannotComplete
        out._obj.value = self._new("el", found)
        return 0

    def cfstr_value(self, handle):
        kind, data = self.handles[handle]
        assert kind == "str"
        return data

    def cfstr(self, s):
        self.keys_made += 1
        return s.encode()

    # --- CF ---
    def cf_release(self, obj):
        if isinstance(obj, Ev):
            self.log.append(("release",) + obj.desc)
        elif isinstance(obj, bytes):
            self.keys_released += 1
        else:
            self.released.append(obj)

    # --- CGEvent ---
    def mouse(self, source, kind, p, button):
        ev = Ev("mouse", kind, p.x, p.y, button)
        self.log.append(("new",) + ev.desc)
        return ev

    def keyboard(self, source, keycode, down):
        ev = Ev("key", keycode, down)
        self.log.append(("new",) + ev.desc)
        return ev

    def scroll(self, source, units, count, dy, dx, dz):
        ev = Ev("scroll", units, count, dy, dx, dz)
        self.log.append(("new",) + ev.desc)
        return ev

    def post(self, tap, ev):
        self.log.append(("post",) + ev.desc)
        self.on_post(ev.desc)


@pytest.fixture
def load(monkeypatch):
    def _load():
        mirroring = types.ModuleType("mac.mirroring.driver")
        mirroring.UNICODE_CHUNK = 20
        mirroring.kCGEventLeftMouseDown = LDOWN
        mirroring.kCGEventLeftMouseUp = LUP
        mirroring.kCGEventMouseMoved = MOVED
        mirroring.kCGHIDEventTap = 0
        window = types.ModuleType("mac.mirroring.window")
        window.AX, window.CF, window.CG = MagicMock(), MagicMock(), MagicMock()
        window.CGPoint, window.CGRect = CGPoint, CGRect
        window._cfstr = lambda s: s.encode()
        window._cfstr_value = lambda v: None
        pkg = types.ModuleType("mac.mirroring")
        pkg.driver, pkg.window = mirroring, window
        for name, mod in (("mac.mirroring", pkg), ("mac.mirroring.driver", mirroring),
                          ("mac.mirroring.window", window)):
            monkeypatch.setitem(sys.modules, name, mod)
        monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: MagicMock())  # driver CDLLs ImageIO
        spec = importlib.util.spec_from_file_location("_screen_lock_driver", DRIVER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        mac = FakeMac()
        mod._main_bounds = lambda: CGRect(CGPoint(0, 0), CGPoint(100, 100))
        mod.time = types.SimpleNamespace(sleep=lambda s: mac.log.append(("sleep", s)))
        mod._cfstr = mac.cfstr
        mod._cfstr_value = mac.cfstr_value
        mod.AX.AXUIElementCreateSystemWide = mac.create_system_wide
        mod.AX.AXUIElementCopyAttributeValue = mac.copy_attribute
        mod.AX.AXUIElementGetPid = mac.get_pid
        mod.AX.AXUIElementCopyElementAtPosition = mac.element_at_position
        mod.CF.CFRelease = mac.cf_release
        mod.AX.CGEventCreateMouseEvent = mac.mouse
        mod.AX.CGEventCreateKeyboardEvent = mac.keyboard
        mod.AX.CGEventCreateScrollWheelEvent2 = mac.scroll
        mod.AX.CGEventSetIntegerValueField = lambda ev, field, value: mac.log.append(("field", field, value))
        mod.AX.CGEventSetFlags = lambda ev, mask: mac.log.append(("flags", mask))
        mod.AX.CGEventKeyboardSetUnicodeString = lambda ev, n, buf: mac.log.append(("unicode", n, buf.raw))
        mod.AX.CGEventPost = mac.post
        return mod, mac
    return _load


def assert_pairs(posted: list) -> None:
    """Every posted mouse-down / key-down has its up before anything else is
    pressed, and nothing is left held at the end."""
    held = None
    for desc in posted:
        if desc[0] == "mouse" and desc[1] in (LDOWN, RDOWN):
            assert held is None, posted
            held = ("mouse", desc[1] + 1)
        elif desc[0] == "mouse" and desc[1] in (LUP, RUP):
            assert held == ("mouse", desc[1]), posted
            held = None
        elif desc[0] == "key" and desc[2]:
            assert held is None, posted
            held = ("key", desc[1])
        elif desc[0] == "key":
            assert held == ("key", desc[1]), posted
            held = None
    assert held is None, posted


def no_title_in(exc, *titles) -> None:
    dump = repr(exc) + str(exc) + repr(vars(exc))
    for title in titles:
        assert title not in dump


def tap(x=0.5, y=0.5, **lock) -> dict:
    return {"type": "tap", "x": x, "y": y, **lock}


def tap_posts(x, y) -> list:
    return [("mouse", MOVED, x, y, 0), ("mouse", LDOWN, x, y, 0), ("mouse", LUP, x, y, 0)]


LOCK = {"require_app": "Safari"}


# a
def test_locked_tap_match_posts_move_down_up(load):
    mod, mac = load()
    mod.send_events([tap(**LOCK)])
    assert mac.posted == tap_posts(50.0, 50.0)
    assert mac.points == [(50.0, 50.0), (50.0, 50.0)]  # the posted point, checked before move and down


# b
def test_app_mismatch_refuses_before_anything(load):
    mod, mac = load()
    mac.front = TERMINAL
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([tap(**LOCK)])
    e = exc.value
    assert (e.reason, e.index, e.partial, e.expected_app, e.actual_app) == ("app", 0, False, "Safari", "Terminal")
    assert mac.posted == []


# c
def test_window_mismatch_refuses_and_keeps_no_title(load):
    mod, mac = load()
    mac.front_window = "Drafts"
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([tap(require_app="Safari", require_window="Inbox")])
    assert (exc.value.reason, exc.value.window_matched) == ("window", False)
    assert mac.posted == []
    no_title_in(exc.value, "Drafts", "Inbox")


# d
def test_point_over_another_app_refuses(load):
    mod, mac = load()
    mac.element_at = lambda x, y: (TERMINAL[1], "zsh")
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([tap(**LOCK)])
    assert (exc.value.reason, exc.value.index, exc.value.actual_app) == ("point", 0, "Safari")
    assert mac.posted == []


def test_point_over_another_window_of_the_app_refuses(load):
    mod, mac = load()
    mac.element_at = lambda x, y: (SAFARI[1], "Other window")
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([tap(require_app="Safari", require_window="Inbox")])
    assert (exc.value.reason, exc.value.window_matched) == ("point", True)
    assert mac.posted == []
    no_title_in(exc.value, "Other window", "Inbox")


# e
def test_focus_change_mid_batch_stops_at_that_event(load):
    mod, mac = load()

    def flip(desc):
        if desc[:2] == ("mouse", LUP):
            mac.front = TERMINAL
    mac.on_post = flip
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([tap(0.1, 0.1, **LOCK), tap(0.2, 0.2, **LOCK), tap(0.3, 0.3, **LOCK)])
    assert (exc.value.index, exc.value.partial, exc.value.reason) == (1, False, "app")
    assert mac.posted == tap_posts(10.0, 10.0)


# f
def test_focus_change_between_move_and_down_posts_only_the_move(load):
    mod, mac = load()

    def flip(desc):
        if desc[:2] == ("mouse", MOVED):
            mac.front = TERMINAL
    mac.on_post = flip
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([tap(**LOCK)])
    assert (exc.value.index, exc.value.partial) == (0, True)
    assert mac.posted == [("mouse", MOVED, 50.0, 50.0, 0)]


# g
def test_halt_refuses_until_resume(load):
    mod, mac = load()
    mod.halt()
    assert mod.is_halted()
    with pytest.raises(mod.InputHalted) as exc:
        mod.send_events([tap()])  # unlocked callers are halted too
    assert (exc.value.index, exc.value.partial) == (0, False)
    assert mac.posted == []
    mod.resume()
    assert not mod.is_halted()
    mod.send_events([tap()])
    assert mac.posted == tap_posts(50.0, 50.0)


def test_halt_works_while_a_batch_holds_the_lock(load):
    mod, _ = load()
    with mod._LOCK:
        mod.halt()  # a threading.Event, never waits on _LOCK
    assert mod.is_halted()


def test_halt_during_event_one_stops_events_two_and_three(load):
    mod, mac = load()

    def halt_on_down(desc):
        if desc[:2] == ("mouse", LDOWN):
            mod.halt()
    mac.on_post = halt_on_down
    with pytest.raises(mod.InputHalted) as exc:
        mod.send_events([tap(0.1, 0.1), tap(0.2, 0.2), tap(0.3, 0.3)])
    assert exc.value.index == 1
    assert mac.posted == tap_posts(10.0, 10.0)  # the down still got its up
    assert_pairs(mac.posted)


# h
def test_halt_between_key_down_and_up_still_posts_the_up(load):
    mod, mac = load()

    def halt_on_key_down(desc):
        if desc[0] == "key" and desc[2]:
            mod.halt()
    mac.on_post = halt_on_key_down
    with pytest.raises(mod.InputHalted) as exc:
        mod.send_events([{"type": "key", "key": "return", "mods": []},
                         {"type": "key", "key": "tab", "mods": []}])
    assert exc.value.index == 1
    assert mac.posted == [("key", 36, True), ("key", 36, False)]
    assert_pairs(mac.posted)


def test_halt_mid_text_releases_the_chunk_and_stops(load):
    mod, mac = load()

    def halt_on_key_down(desc):
        if desc[0] == "key" and desc[2]:
            mod.halt()
    mac.on_post = halt_on_key_down
    with pytest.raises(mod.InputHalted) as exc:
        mod.send_events([{"type": "text", "text": "a" * 45}])  # 3 chunks
    assert (exc.value.index, exc.value.partial) == (0, True)
    assert mac.posted == [("key", 0, True), ("key", 0, False)]


def test_halt_mid_drag_releases_the_button(load):
    mod, mac = load()
    moves = []

    def halt_on_third_held_move(desc):
        if desc[:2] == ("mouse", MOVED) and any(d[:2] == ("mouse", LDOWN) for d in mac.posted):
            moves.append(desc)
            if len(moves) == 3:
                mod.halt()
    mac.on_post = halt_on_third_held_move
    with pytest.raises(mod.InputHalted):
        mod.send_events([{"type": "drag", "x": 0.1, "y": 0.1, "dx": 0.2, "dy": 0.2}])
    assert mac.posted[-1] == ("mouse", LUP) + moves[-1][2:]  # released where the pointer is
    assert sum(d[:2] == ("mouse", LUP) for d in mac.posted) == 1
    assert_pairs(mac.posted)


# i
def test_drag_refused_at_end_point_releases_at_last_move(load):
    mod, mac = load()
    mac.element_at = lambda x, y: (SAFARI[1] if x < 79 else TERMINAL[1], None)
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([{"type": "drag", "x": 0.2, "y": 0.2, "dx": 0.6, "dy": 0.0, **LOCK}])
    assert (exc.value.reason, exc.value.partial, exc.value.index) == ("point", True, 0)
    last = 20.0 + (80.0 - 20.0) * (10 / 11)
    assert mac.posted[0] == ("mouse", MOVED, 20.0, 20.0, 0)
    assert mac.posted[1] == ("mouse", LDOWN, 20.0, 20.0, 0)
    assert len(mac.posted) == 2 + 10 + 1
    assert mac.posted[-1] == ("mouse", LUP, last, 20.0, 0)
    assert mac.points[-1] == (80.0, 20.0)  # the end point was what got checked
    assert_pairs(mac.posted)


def test_locked_drag_match_releases_at_end_point(load):
    mod, mac = load()
    mod.send_events([{"type": "drag", "x": 0.2, "y": 0.2, "dx": 0.6, "dy": 0.0, **LOCK}])
    assert mac.posted[-1] == ("mouse", LUP, 80.0, 20.0, 0)
    assert len(mac.posted) == 13
    assert mac.points == [(20.0, 20.0), (20.0, 20.0), (80.0, 20.0)]


# j
def test_text_focus_loss_after_first_chunk_is_partial(load):
    mod, mac = load()
    key_ups = []

    def flip_after_first_text_chunk(desc):
        if desc == ("key", 0, False):
            key_ups.append(desc)
            mac.front = TERMINAL
    mac.on_post = flip_after_first_text_chunk
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([{"type": "key", "key": "tab", "mods": [], **LOCK},
                         {"type": "text", "text": "b" * 45, **LOCK}])
    assert (exc.value.index, exc.value.partial, exc.value.reason) == (1, True, "app")
    assert mac.posted == [("key", 48, True), ("key", 48, False), ("key", 0, True), ("key", 0, False)]
    assert mac.points == []  # keyboard events have no point check
    assert_pairs(mac.posted)


# k
def _golden() -> list:
    """The exact CGEvent call sequence (with cadence sleeps) the ci-ipa driver
    produced for ALL_TYPES; verified once against that driver file."""
    def mouse(kind, x, y, btn=0):
        d = ("mouse", kind, x, y, btn)
        return [("new",) + d, ("post",) + d, ("release",) + d]

    def key(code, down, extra=()):
        d = ("key", code, down)
        return [("new",) + d, *extra, ("post",) + d, ("release",) + d, ("sleep", 0.02)]

    out = mouse(MOVED, 10.0, 20.0)
    out += mouse(MOVED, 50.0, 50.0) + [("sleep", 0.15)] + mouse(LDOWN, 50.0, 50.0) + [("sleep", 0.05)]
    out += mouse(LUP, 50.0, 50.0)
    out += mouse(MOVED, 30.0, 30.0) + [("sleep", 0.15), ("new", "mouse", LDOWN, 30.0, 30.0, 0), ("field", 1, 2),
                                         ("post", "mouse", LDOWN, 30.0, 30.0, 0),
                                         ("release", "mouse", LDOWN, 30.0, 30.0, 0), ("sleep", 0.05)]
    out += mouse(LUP, 30.0, 30.0)
    out += mouse(MOVED, 40.0, 40.0) + [("sleep", 0.15)] + mouse(RDOWN, 40.0, 40.0, 1) + [("sleep", 0.05)]
    out += mouse(RUP, 40.0, 40.0, 1)
    a, b = 0.1 * 100, (0.1 + 0.2) * 100  # same float arithmetic as to_points
    out += mouse(MOVED, a, a) + [("sleep", 0.1)] + mouse(LDOWN, a, a)
    for i in range(1, 11):
        f = i / 11
        out += mouse(MOVED, a + (b - a) * f, a + (b - a) * f) + [("sleep", 0.03)]
    out += mouse(LUP, b, b)
    s = ("scroll", 1, 2, 5, -3, 0)
    out += [("new",) + s, ("post",) + s, ("release",) + s]
    hi = "hi".encode("utf-16-le")
    out += key(0, True, [("unicode", 2, hi)]) + key(0, False, [("unicode", 2, hi)])
    out += key(48, True, [("flags", 0x100000)]) + key(48, False)
    return out


NONE_LOCK = {"require_app": None, "require_window": None}  # what model_dump() sends
ALL_TYPES = [
    {"type": "move", "x": 0.1, "y": 0.2, **NONE_LOCK},
    {"type": "tap", "x": 0.5, "y": 0.5, **NONE_LOCK},
    {"type": "dblclick", "x": 0.3, "y": 0.3, **NONE_LOCK},
    {"type": "right", "x": 0.4, "y": 0.4, **NONE_LOCK},
    {"type": "drag", "x": 0.1, "y": 0.1, "dx": 0.2, "dy": 0.2, **NONE_LOCK},
    {"type": "scroll", "dy": 0.5, "dx": -0.3, **NONE_LOCK},
    {"type": "text", "text": "hi", **NONE_LOCK},
    {"type": "key", "key": "tab", "mods": ["cmd"], **NONE_LOCK},
]


def test_unlocked_batch_is_byte_for_byte_unchanged(load):
    mod, mac = load()
    mod.send_events(ALL_TYPES)
    assert mac.log == _golden()
    assert mac.system_created == 0 and mac.points == []  # no AX probe at all


# l
def test_locked_scroll_checks_point_moves_then_scrolls(load):
    mod, mac = load()
    mod.send_events([{"type": "scroll", "dy": 0.5, "dx": None, "x": 0.25, "y": 0.75, **LOCK}])
    assert mac.posted == [("mouse", MOVED, 25.0, 75.0, 0), ("scroll", 1, 2, 5, 0, 0)]
    assert mac.points == [(25.0, 75.0), (25.0, 75.0)]


def test_locked_scroll_over_another_app_refuses(load):
    mod, mac = load()
    mac.element_at = lambda x, y: (TERMINAL[1], None)
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([{"type": "scroll", "dy": 0.5, "x": 0.25, "y": 0.75, **LOCK}])
    assert exc.value.reason == "point"
    assert mac.posted == []


def test_locked_scroll_without_point_posts_nothing(load):
    mod, mac = load()
    with pytest.raises(ValueError):
        mod.send_events([{"type": "scroll", "dy": 0.5, "x": None, "y": None, **LOCK}])
    assert mac.posted == []


# m
def test_scroll_and_drag_accept_none_deltas(load):
    mod, mac = load()
    mod.send_events([{"type": "scroll", "dy": 0.5, "dx": None},
                     {"type": "drag", "x": 0.1, "y": 0.1, "dx": None, "dy": 0.2}])
    assert mac.posted[0] == ("scroll", 1, 2, 5, 0, 0)
    assert mac.posted[-1] == ("mouse", LUP, 0.1 * 100, (0.1 + 0.2) * 100, 0)


# n
def test_locked_mode_refuses_unlocked_event_before_any_post(load):
    mod, mac = load()
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([tap(**LOCK), tap()], locked=True)
    assert (exc.value.reason, exc.value.index, exc.value.partial) == ("unlocked", 1, False)
    assert mac.posted == [] and mac.system_created == 0
    mod.send_events([tap(**LOCK), tap(**LOCK)], locked=True)
    assert mac.posted == tap_posts(50.0, 50.0) * 2


# o
@pytest.mark.parametrize("break_it", ["front_app", "front_window", "point", "point_window"])
def test_any_ax_failure_fails_closed(load, break_it):
    mod, mac = load()
    if break_it == "front_app":
        mac.front = None
    elif break_it == "front_window":
        mac.front_window = None
    elif break_it == "point":
        mac.element_at = lambda x, y: None
    else:
        mac.element_at = lambda x, y: (SAFARI[1], None)
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([tap(require_app="Safari", require_window="Inbox")])
    assert exc.value.reason == "probe_failed"
    assert mac.posted == []
    assert sorted(mac.created) == sorted(mac.released)


def test_system_wide_element_failure_fails_closed(load):
    mod, mac = load()
    mod.AX.AXUIElementCreateSystemWide = lambda: None
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([{"type": "key", "key": "return", "mods": [], **LOCK}])
    assert exc.value.reason == "probe_failed"
    assert mac.posted == []


# p
@pytest.mark.parametrize("element_pid", [SAFARI[1], TERMINAL[1]])
def test_every_ax_object_is_released_once(load, element_pid):
    mod, mac = load()
    mac.element_at = lambda x, y: (element_pid, "Inbox - Mail")
    try:
        mod.send_events([tap(require_app="Safari", require_window="Inbox")])
    except mod.FocusMismatch:
        assert element_pid == TERMINAL[1]
    assert mac.created  # the probe really ran
    assert sorted(mac.created) == sorted(mac.released)  # each created/copied AX object released once
    assert mac.keys_made == mac.keys_released  # every attribute-name CFString too


# criterion 3: only two CGEventPost call sites, and they are the two doors
def test_cgeventpost_has_exactly_two_doors():
    tree = ast.parse(DRIVER_PATH.read_text())
    owners = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr == "CGEventPost":
                    owners.append(fn.name)
    assert sorted(owners) == ["_guarded_post", "_post_release"]
    assert DRIVER_PATH.read_text().count("CGEventPost(") == 2


# criterion 6, end to end: a real refusal rendered as the route's 409 body
@pytest.mark.parametrize("break_it", ["focused_window", "point_window"])
def test_refusal_body_never_carries_a_window_title(load, break_it):
    import json

    from gateway import screen

    mod, mac = load()
    if break_it == "focused_window":
        mac.front_window = "SECRET-TITLE"
    else:
        mac.element_at = lambda x, y: (SAFARI[1], "SECRET-TITLE")
        mac.front_window = "WANTED doc"
    with pytest.raises(mod.FocusMismatch) as exc:
        mod.send_events([tap(require_app="Safari", require_window="WANTED")])
    body = json.dumps(screen._refusal_detail(exc.value))
    assert "SECRET-TITLE" not in body and "WANTED" not in body
    assert json.loads(body)["error"] == "focus_mismatch"
