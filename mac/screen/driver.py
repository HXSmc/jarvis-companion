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
window). Any AX failure refuses (fail closed). A refusal raises FocusMismatch,
which carries app names and booleans only, never a window title. Events
without `require_app` post exactly as before and never probe AX.
`send_events(..., locked=True)` refuses the whole batch before anything posts
unless every event carries `require_app`. The guarantee is
check-immediately-before-post, not atomicity: focus can still change in the
microseconds between the check and the post.

Halt: `halt()` makes every later post refuse (InputHalted) until `resume()`;
it is a threading.Event, so it works while a batch holds `_LOCK`.

Release rule: a posted mouse-down or key-down is always followed by its up,
even when a halt or a refusal arrives in between (a drag refused at its end
point releases at the last intermediate point). Those release posts are the
only posts after a refusal; `_guarded_post` and `_post_release` are the only
two places that call CGEventPost.

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
                                  _cfstr_value)

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
}

_FLAG_MASKS = {
    "cmd": 0x100000,   # kCGEventFlagMaskCommand
    "shift": 0x20000,  # kCGEventFlagMaskShift
    "alt": 0x80000,    # kCGEventFlagMaskAlternate
    "ctrl": 0x40000,   # kCGEventFlagMaskControl
}


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
    probe_failed | unlocked. Holds app names and booleans only, never a
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


def _ax_copy(el, name):
    """Copy one AX attribute value (caller releases it), releasing the
    CFString key (window._attr leaks both key and value)."""
    key = _cfstr(name)
    out = ctypes.c_void_p()
    try:
        if AX.AXUIElementCopyAttributeValue(el, key, ctypes.byref(out)) != 0:
            return None
        return out.value
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


def _focus_probe(system, want_window: bool):
    """(focused app title, its pid, focused window title or None). The pid is
    read while the app element is still valid, so nothing outlives its release."""
    app = _ax_copy(system, "AXFocusedApplication")
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
    if not app:
        return
    want = ev.get("require_window")
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
        bounds = _main_bounds()
        for index, ev in enumerate(events):
            _event_posted = False
            try:
                _post(ev, bounds)
            except (FocusMismatch, InputHalted) as exc:
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
                raise ValueError("a locked scroll needs x and y")
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
    """Guarded at the start point (move + down) and the end point (before the
    up). The held-button moves only check halt. Any refusal after the down
    releases the button where the pointer is."""
    _move(p1, ev)
    time.sleep(0.1)
    down = AX.CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, p1, 0)
    _guarded_post(ev, down, p1)
    at = p1
    try:
        for i in range(1, 11):  # <= 10 intermediate moves, 0.03s apart
            f = i / 11
            q = CGPoint(p1.x + (p2.x - p1.x) * f, p1.y + (p2.y - p1.y) * f)
            _guarded_post(None, AX.CGEventCreateMouseEvent(None, kCGEventMouseMoved, q, 0))
            at = q
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
        AX.CGEventKeyboardSetUnicodeString(down, n, buf)
        _guarded_post(ev, down)
        try:
            time.sleep(0.02)
        finally:
            up = AX.CGEventCreateKeyboardEvent(None, 0, False)
            AX.CGEventKeyboardSetUnicodeString(up, n, buf)
            _post_release(up)
        time.sleep(0.02)


def _key(key: str, mods: list[str], ev: dict | None = None) -> None:
    mask = 0
    for m in mods:
        mask |= _FLAG_MASKS[m]
    down = AX.CGEventCreateKeyboardEvent(None, KEYCODES[key], True)
    if mask:
        AX.CGEventSetFlags(down, mask)
    _guarded_post(ev, down)
    try:
        time.sleep(0.02)
    finally:
        _post_release(AX.CGEventCreateKeyboardEvent(None, KEYCODES[key], False))
    time.sleep(0.02)
