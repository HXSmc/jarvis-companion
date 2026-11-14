"""Remote screen control driver: whole-display capture + synthetic input.

Whole-display sibling of mac/mirroring/driver.py (which targets the iPhone
Mirroring window only). Reuses the same framework handles and event engine:
CG/CF/AX CDLLs, CGPoint, prototypes and constants come from the mirroring
stack — no duplicated ctypes loading.

Focus lock (opt-in per event): an event carrying `require_app` (exact AX title
of the focused app) and optionally `require_window` (case-sensitive substring
of the focused window's AX title) is checked by `_guard` immediately before
each of its posts: the focused app/window must match and, for pointer events,
the AX element under the target point must belong to the focused app (and
window). Any AX failure refuses (fail closed). A fresh process's system-wide
AXFocusedApplication read returns kAXErrorCannotComplete until that process
has run one AX hit-test, so the first focus probe runs a read-only
AXUIElementCopyElementAtPosition first (`_warm_ax`, once per process); the
focused-app read then still retries kAXErrorCannotComplete, at most 3 tries,
and never posts in between. If that read still fails (Electron apps such as
VS Code), `_frontmost_app` asks the window server for the top layer-0 window's
owner and accepts it only if that app itself reports AXFrontmost true. A refusal raises FocusMismatch, which carries app names and booleans only, never a window title. Events
without `require_app` post exactly as before and never probe AX.
`send_events(..., locked=True)` refuses the whole batch before anything posts
unless every event carries `require_app`. The guarantee is
check-immediately-before-post, not atomicity: focus can still change in the
microseconds between the check and the post.

Halt: `halt()` makes every later post refuse (InputHalted) until `resume()`;
it is a threading.Event, so it works while a batch holds `_LOCK`.

Release rule: a posted mouse-down or key-down is always followed by its up,
even when a halt or a refusal arrives in between (a drag refused after its
down releases at its checked start point). Those release posts are the
only posts after a refusal; `_guarded_post` and `_post_release` are the only
two places that call CGEventPost.

System shortcuts: `_key` refuses log out (cmd+shift+Q) and lock screen
(ctrl+cmd+Q) always, and screenshots (cmd+shift+3/4/5) on a focus-locked
event, before any event is built (ShortcutDenied); the match folds case,
aliases, order and duplicates, and extra modifiers still match.

Pure ctypes against macOS system frameworks — no pyobjc, no new deps.
"""

from __future__ import annotations

import ctypes
import subprocess
import threading
import time

from mac.mirroring.driver import (UNICODE_CHUNK, kCGEventLeftMouseDown,
                                  kCGEventLeftMouseUp, kCGEventMouseMoved,
                                  kCGHIDEventTap)
from mac.mirroring.window import (AX, CF, CG, CGPoint, CGRect, _cfstr,
                                  _cfstr_value, _enum_windows)

# CG is not re-entrant-safe from multiple threads: one lock guards every
# capture and every event batch (gateway calls run in a threadpool).
_LOCK = threading.Lock()
# Not guarded by _LOCK: halt() must land while a batch holds the lock.
_HALT = threading.Event()
# Set by the CGEventPost doors; send_events reads it as the refusal's `partial`.
_event_posted = False

# --- extra prototypes (mouse/keyboard basics already set by mirroring) ---

CG.CGMainDisplayID.restype = ctypes.c_uint32
CG.CGDisplayBounds.restype = CGRect
CG.CGDisplayBounds.argtypes = [ctypes.c_uint32]
CG.CGDisplayIsAsleep.restype = ctypes.c_bool
CG.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
CG.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
CG.CGDisplayCreateImage.restype = ctypes.c_void_p
CG.CGDisplayCreateImage.argtypes = [ctypes.c_uint32]
CG.CGImageGetWidth.restype = ctypes.c_size_t
CG.CGImageGetWidth.argtypes = [ctypes.c_void_p]
CG.CGImageGetHeight.restype = ctypes.c_size_t
CG.CGImageGetHeight.argtypes = [ctypes.c_void_p]
CG.CGColorSpaceCreateDeviceRGB.restype = ctypes.c_void_p
CG.CGBitmapContextCreate.restype = ctypes.c_void_p
CG.CGBitmapContextCreate.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                     ctypes.c_size_t, ctypes.c_size_t,
                                     ctypes.c_size_t, ctypes.c_void_p,
                                     ctypes.c_uint32]
CG.CGContextDrawImage.argtypes = [ctypes.c_void_p, CGRect, ctypes.c_void_p]
CG.CGBitmapContextCreateImage.restype = ctypes.c_void_p
CG.CGBitmapContextCreateImage.argtypes = [ctypes.c_void_p]

CF.CFRelease.argtypes = [ctypes.c_void_p]
CF.CFDataCreateMutable.restype = ctypes.c_void_p
CF.CFDataCreateMutable.argtypes = [ctypes.c_void_p, ctypes.c_long]
CF.CFDataGetLength.restype = ctypes.c_long
CF.CFDataGetLength.argtypes = [ctypes.c_void_p]
CF.CFDataGetBytePtr.restype = ctypes.c_void_p
CF.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
CF.CFNumberCreate.restype = ctypes.c_void_p
CF.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
CF.CFDictionaryCreate.restype = ctypes.c_void_p
CF.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_long,
                                  ctypes.c_void_p, ctypes.c_void_p]

IIO = ctypes.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")
IIO.CGImageDestinationCreateWithData.restype = ctypes.c_void_p
IIO.CGImageDestinationCreateWithData.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.c_size_t, ctypes.c_void_p]
IIO.CGImageDestinationAddImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p]
IIO.CGImageDestinationFinalize.restype = ctypes.c_bool
IIO.CGImageDestinationFinalize.argtypes = [ctypes.c_void_p]

# Fixed-arity ONLY: plain CGEventCreateScrollWheelEvent is variadic, and
# variadic C through ctypes on arm64 corrupts registers/crashes.
AX.CGEventCreateScrollWheelEvent2.restype = ctypes.c_void_p
AX.CGEventCreateScrollWheelEvent2.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                              ctypes.c_uint32, ctypes.c_int32,
                                              ctypes.c_int32, ctypes.c_int32]
AX.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.c_int64]
AX.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
# Focus-lock probes (AXUIElement.h): AXError is int32, pid_t is int32, and
# AXUIElementCopyElementAtPosition takes two `float` (not CGFloat) coordinates.
AX.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
AX.AXUIElementCreateSystemWide.argtypes = []
AX.AXUIElementCopyElementAtPosition.restype = ctypes.c_int
AX.AXUIElementCopyElementAtPosition.argtypes = [ctypes.c_void_p, ctypes.c_float,
                                                ctypes.c_float,
                                                ctypes.POINTER(ctypes.c_void_p)]
AX.AXUIElementGetPid.restype = ctypes.c_int
AX.AXUIElementGetPid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
AX.AXUIElementCreateApplication.restype = ctypes.c_void_p
AX.AXUIElementCreateApplication.argtypes = [ctypes.c_int]

# mirroring/driver.py declares only the left-button constants
kCGEventRightMouseDown, kCGEventRightMouseUp = 3, 4
kCGMouseEventClickState = 1  # dblclick sets this field to 2
kCGScrollEventUnitLine = 1
kCGImageAlphaPremultipliedLast = 1
kCFNumberDoubleType = 6

# Full Quartz keycode table (HIToolbox Events.h) for the ScreenEvent `key`
# literal set — written out, no ranges or comprehension tricks.
KEYCODES = {
    "return": 36,
    "esc": 53,
    "delete": 51,
    "tab": 48,
    "space": 49,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pagedown": 121,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
    # US-layout character keys, so cmd+L / cmd+V / cmd+A etc. are possible.
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
    "j": 38, "k": 40, "n": 45, "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25,
    "7": 26, "8": 28, "0": 29,
    "=": 24, "-": 27, "]": 30, "[": 33, "'": 39, ";": 41, "\\": 42,
    ",": 43, "/": 44, ".": 47, "`": 50,
}

_FLAG_MASKS = {
    "cmd": 0x100000,   # kCGEventFlagMaskCommand
    "shift": 0x20000,  # kCGEventFlagMaskShift
    "alt": 0x80000,    # kCGEventFlagMaskAlternate
    "ctrl": 0x40000,   # kCGEventFlagMaskControl
}

# System-wide; macOS acts on these whatever app is in front. Table order
# matters: the first match names the refusal (ctrl+cmd+shift+Q -> log_out).
_DENY_ALWAYS = (
    ("log_out", "q", frozenset({"cmd", "shift"})),
    ("lock_screen", "q", frozenset({"ctrl", "cmd"})),
)
_DENY_LOCKED = (
    ("screenshot", "3", frozenset({"cmd", "shift"})),
    ("screenshot", "4", frozenset({"cmd", "shift"})),
    ("screenshot", "5", frozenset({"cmd", "shift"})),
)
# Matching only: _key still posts canonical names alone (KeyError otherwise).
_MOD_ALIASES = {"command": "cmd", "cmd": "cmd", "option": "alt", "opt": "alt",
                "alt": "alt", "control": "ctrl", "ctrl": "ctrl", "shift": "shift"}


def _system_shortcut(key, mods, locked: bool) -> str | None:
    """Name of the denied system shortcut this key event would fire, or None.
    Same key and the event's folded mod set is a superset of the entry's."""
    k = str(key).lower()
    folded = {_MOD_ALIASES.get(str(m).lower()) for m in (mods or ())}
    for name, dk, dmods in _DENY_ALWAYS + (_DENY_LOCKED if locked else ()):
        if k == dk and dmods <= folded:
            return name
    return None


def tcc_ok() -> bool:
    """Both TCC gates, one bool: Screen Recording AND Accessibility."""
    return bool(CG.CGPreflightScreenCaptureAccess() and AX.AXIsProcessTrusted())


def display_asleep() -> bool:
    return bool(CG.CGDisplayIsAsleep(CG.CGMainDisplayID()))


def to_points(nx: float, ny: float, bounds: CGRect) -> CGPoint:
    """Normalized 0..1 -> global Retina POINTS (CGEvent's coordinate space;
    CGDisplayCreateImage returns pixels — never mix the two)."""
    return CGPoint(bounds.origin.x + nx * bounds.size.x,
                   bounds.origin.y + ny * bounds.size.y)


def _main_bounds() -> CGRect:
    return CG.CGDisplayBounds(CG.CGMainDisplayID())


def _jpeg_bytes(img, q: int) -> tuple[bytes, int, int]:
    """CGImage -> (jpeg bytes, w, h) via ImageIO into a CFMutableData — the
    in-memory sibling of mirroring/driver._write_png's file path."""
    data = CF.CFDataCreateMutable(None, 0)
    qual_key = ctypes.c_void_p.in_dll(
        IIO, "kCGImageDestinationLossyCompressionQuality")
    qual_val = ctypes.c_void_p(CF.CFNumberCreate(
        None, kCFNumberDoubleType, ctypes.byref(ctypes.c_double(q / 100.0))))
    opts = CF.CFDictionaryCreate(
        None, ctypes.byref(qual_key), ctypes.byref(qual_val), 1, None, None)
    dest = IIO.CGImageDestinationCreateWithData(
        data, _cfstr("public.jpeg"), 1, opts)
    try:
        if not dest:
            raise OSError("CGImageDestinationCreateWithData failed")
        IIO.CGImageDestinationAddImage(dest, img, None)
        if not IIO.CGImageDestinationFinalize(dest):
            raise OSError("CGImageDestinationFinalize failed")
        n = CF.CFDataGetLength(data)
        buf = CF.CFDataGetBytePtr(data)
        return (ctypes.string_at(buf, n),
                CG.CGImageGetWidth(img), CG.CGImageGetHeight(img))
    finally:
        if dest:
            CF.CFRelease(dest)
        CF.CFRelease(opts)
        CF.CFRelease(qual_val)
        CF.CFRelease(data)  # bytes copied out via string_at above first


def capture_jpeg(w: int, q: int) -> tuple[bytes, int, int]:
    """Main display -> downscaled JPEG (bytes, out_w, out_h). Pixel space
    throughout (downscale via CGBitmapContext); wakes a sleeping display
    first so the frame isn't the black one."""
    if display_asleep():
        subprocess.run(["caffeinate", "-u", "-t", "2"])
    with _LOCK:
        img = CG.CGDisplayCreateImage(CG.CGMainDisplayID())
        if not img:
            raise OSError("CGDisplayCreateImage returned NULL "
                          "(screen recording permission?)")
        src_w = CG.CGImageGetWidth(img)
        src_h = CG.CGImageGetHeight(img)
        out_w = max(1, int(w))
        out_h = max(1, round(src_h * out_w / src_w))
        cs = CG.CGColorSpaceCreateDeviceRGB()
        ctx = CG.CGBitmapContextCreate(None, out_w, out_h, 8, out_w * 4, cs,
                                       kCGImageAlphaPremultipliedLast)
        if not ctx:
            raise OSError("CGBitmapContextCreate failed")
        CG.CGContextDrawImage(
            ctx, CGRect(CGPoint(0, 0), CGPoint(out_w, out_h)), img)
        scaled = CG.CGBitmapContextCreateImage(ctx)
        if not scaled:
            raise OSError("CGBitmapContextCreateImage failed")
        jpeg, jw, jh = _jpeg_bytes(scaled, q)
        CF.CFRelease(scaled)
        CF.CFRelease(ctx)
        CF.CFRelease(cs)
        CF.CFRelease(img)
        return jpeg, jw, jh


class FocusMismatch(Exception):
    """A focus-locked event was refused. `reason`: app | window | point |
    probe_failed | unlocked | invalid (empty require_app/require_window,
    require_window without require_app, locked scroll without x/y). Holds app names and booleans only, never a
    window title. `index`/`partial` are set by send_events."""

    def __init__(self, reason: str, expected_app: str | None = None,
                 actual_app: str | None = None,
                 window_matched: bool | None = None,
                 index: int = 0, partial: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.expected_app = expected_app
        self.actual_app = actual_app
        self.window_matched = window_matched
        self.index = index
        self.partial = partial


class ShortcutDenied(Exception):
    """A key event would fire a system-wide macOS shortcut (`shortcut`:
    log_out | lock_screen | screenshot); nothing was built or posted for it.
    No `reason` on purpose: it is not a focus mismatch. `index`/`partial`
    are set by send_events."""

    def __init__(self, shortcut: str, index: int = 0, partial: bool = False):
        super().__init__(shortcut)
        self.shortcut = shortcut
        self.index = index
        self.partial = partial


class InputHalted(Exception):
    """halt() is set: the event at `index` was refused (partial = it had
    already posted something, e.g. a released mouse-down)."""

    def __init__(self, index: int = 0, partial: bool = False):
        super().__init__("input halted")
        self.index = index
        self.partial = partial


def halt() -> None:
    """Refuse every post from now on (also mid-batch) until resume()."""
    _HALT.set()


def resume() -> None:
    _HALT.clear()


def is_halted() -> bool:
    return _HALT.is_set()


kAXErrorCannotComplete = -25204
_AX_TRIES = 3
_AX_RETRY_GAP = 0.05
# Set once a hit-test succeeded in this process (read under _LOCK only).
_ax_warm = False


def _ax_copy(el, name, tries: int = 1):
    """Copy one AX attribute value (caller releases it), releasing the
    CFString key (window._attr leaks both key and value). Up to `tries`
    attempts, retrying only kAXErrorCannotComplete; anything else, or the last
    try failing, returns None (the guard then refuses: fail closed)."""
    key = _cfstr(name)
    out = ctypes.c_void_p()
    try:
        for attempt in range(tries):
            err = AX.AXUIElementCopyAttributeValue(el, key, ctypes.byref(out))
            if err == 0:
                return out.value
            if err != kAXErrorCannotComplete or attempt == tries - 1:
                return None
            time.sleep(_AX_RETRY_GAP)
    finally:
        CF.CFRelease(key)


def _ax_title(el) -> str | None:
    v = _ax_copy(el, "AXTitle")
    if not v:
        return None
    try:
        return _cfstr_value(v)
    finally:
        CF.CFRelease(v)


def _ax_pid(el) -> int | None:
    pid = ctypes.c_int()
    if AX.AXUIElementGetPid(el, ctypes.byref(pid)) != 0:
        return None
    return pid.value


def _warm_ax(system) -> None:
    """Measured on a Mac: in a fresh process the system-wide
    AXFocusedApplication read returns kAXErrorCannotComplete (-25204) on every
    try until one AXUIElementCopyElementAtPosition has succeeded in that
    process. This is that hit-test: a pure query (no click, focus or pointer
    change) whose element is released unused. Its result never grants
    anything; the focus read after it decides, and fails closed."""
    global _ax_warm
    el = ctypes.c_void_p()
    if AX.AXUIElementCopyElementAtPosition(system, 0.0, 0.0, ctypes.byref(el)) == 0:
        _ax_warm = True
    if el.value:
        CF.CFRelease(el.value)


def _is_true(v) -> bool:
    """AX booleans are the kCFBooleanTrue / kCFBooleanFalse singletons."""
    return bool(v) and v == ctypes.c_void_p.in_dll(CF, "kCFBooleanTrue").value


def _frontmost_app():
    """App element of the frontmost app when the system-wide read fails, or
    None. Measured on a Mac: with VS Code (Electron) in front the system-wide
    AXFocusedApplication read fails while Accessibility is trusted. Two
    independent sources must agree, else None (fail closed): the window
    server's top on-screen layer-0 window owner (live, front-to-back order),
    and that app's own AXFrontmost == true. Caller releases the element."""
    pid = next((w["pid"] for w in _enum_windows() if w["layer"] == 0), None)
    if pid is None:
        return None
    app = AX.AXUIElementCreateApplication(pid)
    if not app:
        return None
    front = _ax_copy(app, "AXFrontmost", tries=_AX_TRIES)
    try:
        if _ax_pid(app) == pid and front and _is_true(front):
            return app
    finally:
        if front:
            CF.CFRelease(front)
    CF.CFRelease(app)
    return None


def _focus_probe(system, want_window: bool):
    """(focused app title, its pid, focused window title or None). The pid is
    read while the app element is still valid, so nothing outlives its release."""
    if not _ax_warm:
        _warm_ax(system)
    app = _ax_copy(system, "AXFocusedApplication", tries=_AX_TRIES) or _frontmost_app()
    if not app:
        return (None, None, None)
    try:
        title, pid, window = _ax_title(app), _ax_pid(app), None
        if want_window:
            win = _ax_copy(app, "AXFocusedWindow")
            if win:
                try:
                    window = _ax_title(win)
                finally:
                    CF.CFRelease(win)
        return (title, pid, window)
    finally:
        CF.CFRelease(app)


def _point_probe(system, p: CGPoint, want_window: bool):
    """(pid, window title or None) of the AX element under `p`: the same
    global top-left point the event is posted at (hit-tests like the mouse)."""
    el = ctypes.c_void_p()
    if (AX.AXUIElementCopyElementAtPosition(system, p.x, p.y, ctypes.byref(el)) != 0
            or not el.value):
        return (None, None)
    try:
        pid, window = _ax_pid(el.value), None
        if want_window:
            win = _ax_copy(el.value, "AXWindow")
            if win:
                try:
                    window = _ax_title(win)
                finally:
                    CF.CFRelease(win)
        return (pid, window)
    finally:
        CF.CFRelease(el.value)


def _guard(ev: dict | None, point: CGPoint | None = None) -> None:
    """Refuse the next post: halt first; then, only when `ev` pins an app, the
    focused app/window and (for pointer events) the element at `point`.
    Fail closed: any AX failure is `probe_failed`."""
    if _HALT.is_set():
        raise InputHalted()
    app = ev.get("require_app") if ev else None
    want = ev.get("require_window") if ev else None
    if app is None and want is None:
        return
    if not app or want == "":  # a malformed lock never degrades to "no check"
        raise FocusMismatch("invalid", app or None)
    system = AX.AXUIElementCreateSystemWide()
    if not system:
        raise FocusMismatch("probe_failed", app)
    try:
        front, front_pid, window = _focus_probe(system, bool(want))
        if front is None or front_pid is None:
            raise FocusMismatch("probe_failed", app, front)
        if front != app:
            raise FocusMismatch("app", app, front)
        if want and window is None:
            raise FocusMismatch("probe_failed", app, front)
        if want and want not in window:
            raise FocusMismatch("window", app, front, window_matched=False)
        matched = True if want else None
        if point is not None:
            pid, pwin = _point_probe(system, point, bool(want))
            if pid is None or (want and pwin is None):
                raise FocusMismatch("probe_failed", app, front, matched)
            if pid != front_pid or (want and want not in pwin):
                raise FocusMismatch("point", app, front, matched)
    finally:
        CF.CFRelease(system)
    if _HALT.is_set():  # the probes take time; a halt may have landed meanwhile
        raise InputHalted()


def _guarded_post(ev: dict | None, cg_event, point: CGPoint | None = None) -> None:
    """The one door to CGEventPost for everything but releases: run _guard,
    then post. `ev=None` = halt check only (drag's intermediate moves)."""
    global _event_posted
    try:
        _guard(ev, point)
        AX.CGEventPost(kCGHIDEventTap, cg_event)
        _event_posted = True
    finally:
        CF.CFRelease(cg_event)


def _post_release(cg_event) -> None:
    """The up for an already-posted down: never refused (no stuck input)."""
    global _event_posted
    AX.CGEventPost(kCGHIDEventTap, cg_event)
    _event_posted = True
    CF.CFRelease(cg_event)


def send_events(events, *, locked: bool = False) -> None:
    """Post a batch of gateway ScreenEvent dicts. One lock, whole batch.

    `locked=True`: every event must carry require_app, checked for the whole
    batch before anything posts. A refusal (FocusMismatch / InputHalted) is
    tagged with the refused event's index and stops the batch."""
    global _event_posted
    with _LOCK:
        if locked:
            for index, ev in enumerate(events):
                if not ev.get("require_app"):
                    raise FocusMismatch("unlocked", index=index)
                if ev.get("require_window") == "" or (
                        ev["type"] == "scroll" and (ev.get("x") is None or ev.get("y") is None)):
                    raise FocusMismatch("invalid", ev["require_app"], index=index)
        bounds = _main_bounds()
        for index, ev in enumerate(events):
            _event_posted = False
            try:
                _post(ev, bounds)
            except (FocusMismatch, InputHalted, ShortcutDenied) as exc:
                exc.index = index
                exc.partial = _event_posted
                raise


def _post(ev: dict, bounds: CGRect) -> None:
    t = ev["type"]
    if t == "move":
        _move(to_points(ev["x"], ev["y"], bounds), ev)
    elif t in ("tap", "dblclick", "right"):
        _click(to_points(ev["x"], ev["y"], bounds), ev,
               double=(t == "dblclick"),
               right=(t == "right" or ev.get("button") == "right"))
    elif t == "drag":
        # model_dump() sends unset dx/dy as None, so .get(k, 0.0) is not enough
        _drag(to_points(ev["x"], ev["y"], bounds),
              to_points(ev["x"] + (ev.get("dx") or 0.0), ev["y"] + (ev.get("dy") or 0.0),
                        bounds), ev)
    elif t == "scroll":
        p = None
        if ev.get("require_app"):
            if ev.get("x") is None or ev.get("y") is None:
                raise FocusMismatch("invalid", ev["require_app"])
            p = to_points(ev["x"], ev["y"], bounds)
            _move(p, ev)
        _scroll(ev.get("dy") or 0.0, ev.get("dx") or 0.0, ev, p)
    elif t == "text":
        _type_text(ev["text"], ev)
    elif t == "key":
        _key(ev["key"], ev.get("mods") or [], ev)
    else:
        raise ValueError(f"unknown event type {t!r}")


def _move(p: CGPoint, ev: dict | None = None) -> None:
    cg = AX.CGEventCreateMouseEvent(None, kCGEventMouseMoved, p, 0)
    _guarded_post(ev, cg, p)


def _click(p: CGPoint, ev: dict | None = None, double: bool = False,
           right: bool = False) -> None:
    """click() cadence from mirroring/driver: move, 0.15s, down, 0.05s, up —
    but NO cursor-restore warp (screen control owns the pointer). Guarded
    before the move and again before the down (focus can move in 0.15 s)."""
    _move(p, ev)
    time.sleep(0.15)
    down_t, up_t = ((kCGEventRightMouseDown, kCGEventRightMouseUp) if right
                    else (kCGEventLeftMouseDown, kCGEventLeftMouseUp))
    btn = 1 if right else 0
    down = AX.CGEventCreateMouseEvent(None, down_t, p, btn)
    if double:
        AX.CGEventSetIntegerValueField(down, kCGMouseEventClickState, 2)
    _guarded_post(ev, down, p)
    try:
        time.sleep(0.05)
    finally:
        _post_release(AX.CGEventCreateMouseEvent(None, up_t, p, btn))


def _drag(p1: CGPoint, p2: CGPoint, ev: dict | None = None) -> None:
    """Both the start and the end point are guarded before the mouse-down, so
    a bad end point refuses with nothing pressed; the end point is checked
    again before the up. The held-button moves only check halt. Any refusal
    after the down releases at the START point p1 (a drop back on the
    checked source = cancel), never at an unchecked point."""
    _move(p1, ev)
    time.sleep(0.1)
    _guard(ev, p2)
    down = AX.CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, p1, 0)
    _guarded_post(ev, down, p1)
    at = p1
    try:
        for i in range(1, 11):  # <= 10 intermediate moves, 0.03s apart
            f = i / 11
            q = CGPoint(p1.x + (p2.x - p1.x) * f, p1.y + (p2.y - p1.y) * f)
            _guarded_post(None, AX.CGEventCreateMouseEvent(None, kCGEventMouseMoved, q, 0))
            time.sleep(0.03)
        _guard(ev, p2)
        at = p2
    finally:
        _post_release(AX.CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, at, 0))


def _scroll(dy: float, dx: float = 0.0, ev: dict | None = None,
            p: CGPoint | None = None) -> None:
    cg = AX.CGEventCreateScrollWheelEvent2(None, kCGScrollEventUnitLine, 2,
                                           int(dy * 10), int(dx * 10), 0)
    _guarded_post(ev, cg, p)


def _type_text(text: str, ev: dict | None = None) -> None:
    """keycode-0 keyboard events carrying UTF-16 in UNICODE_CHUNK chunks —
    exact type_text pattern from mirroring/driver. Each chunk's key-down is
    guarded; its key-up always follows."""
    units = text.encode("utf-16-le")
    for i in range(0, len(units), UNICODE_CHUNK * 2):
        chunk = units[i:i + UNICODE_CHUNK * 2]
        buf = ctypes.create_string_buffer(chunk, len(chunk))
        n = len(chunk) // 2
        down = AX.CGEventCreateKeyboardEvent(None, 0, True)
        AX.CGEventSetFlags(down, 0)  # see _key: never inherit live modifiers
        AX.CGEventKeyboardSetUnicodeString(down, n, buf)
        _guarded_post(ev, down)
        try:
            time.sleep(0.02)
        finally:
            up = AX.CGEventCreateKeyboardEvent(None, 0, False)
            AX.CGEventSetFlags(up, 0)
            AX.CGEventKeyboardSetUnicodeString(up, n, buf)
            _post_release(up)
        time.sleep(0.02)


def _key(key: str, mods: list[str], ev: dict | None = None) -> None:
    if name := _system_shortcut(key, mods, locked=ev is not None and (
            ev.get("require_app") is not None or ev.get("require_window") is not None)):
        raise ShortcutDenied(name)
    mask = 0
    for m in mods:
        mask |= _FLAG_MASKS[m]
    down = AX.CGEventCreateKeyboardEvent(None, KEYCODES[key], True)
    # A NULL-source event copies the live HID modifier state; a stuck
    # Command there turned every typed char into Cmd+char (nothing typed).
    AX.CGEventSetFlags(down, mask)
    _guarded_post(ev, down)
    try:
        time.sleep(0.02)
    finally:
        up = AX.CGEventCreateKeyboardEvent(None, KEYCODES[key], False)
        AX.CGEventSetFlags(up, mask)
        _post_release(up)
    time.sleep(0.02)
