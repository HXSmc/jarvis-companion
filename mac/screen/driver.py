"""Remote screen control driver: whole-display capture + synthetic input.

Whole-display sibling of mac/mirroring/driver.py (which targets the iPhone
Mirroring window only). Reuses the same framework handles and event engine:
CG/CF/AX CDLLs, CGPoint, prototypes and constants come from the mirroring
stack — no duplicated ctypes loading. Deliberately NO frontmost-window guard
(mirroring's `_check_frontmost` is specific to that feature; screen control
acts on whatever is on the display).

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
from mac.mirroring.window import AX, CF, CG, CGPoint, CGRect, _cfstr

# CG is not re-entrant-safe from multiple threads: one lock guards every
# capture and every event batch (gateway calls run in a threadpool).
_LOCK = threading.Lock()

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
    if not dest:
        raise OSError("CGImageDestinationCreateWithData failed")
    IIO.CGImageDestinationAddImage(dest, img, None)
    if not IIO.CGImageDestinationFinalize(dest):
        raise OSError("CGImageDestinationFinalize failed")
    n = CF.CFDataGetLength(data)
    buf = CF.CFDataGetBytePtr(data)
    return (ctypes.string_at(buf, n),
            CG.CGImageGetWidth(img), CG.CGImageGetHeight(img))


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


def send_events(events) -> None:
    """Post a batch of gateway ScreenEvent dicts. One lock, whole batch."""
    with _LOCK:
        bounds = _main_bounds()
        for ev in events:
            _post(ev, bounds)


def _post(ev: dict, bounds: CGRect) -> None:
    t = ev["type"]
    if t == "move":
        _move(to_points(ev["x"], ev["y"], bounds))
    elif t in ("tap", "dblclick", "right"):
        _click(to_points(ev["x"], ev["y"], bounds),
               double=(t == "dblclick"),
               right=(t == "right" or ev.get("button") == "right"))
    elif t == "drag":
        _drag(to_points(ev["x"], ev["y"], bounds),
              to_points(ev["x"] + ev.get("dx", 0.0), ev["y"] + ev.get("dy", 0.0),
                        bounds))
    elif t == "scroll":
        _scroll(ev.get("dy", 0.0), ev.get("dx", 0.0))
    elif t == "text":
        _type_text(ev["text"])
    elif t == "key":
        _key(ev["key"], ev.get("mods") or [])
    else:
        raise ValueError(f"unknown event type {t!r}")


def _move(p: CGPoint) -> None:
    AX.CGEventPost(kCGHIDEventTap,
                   AX.CGEventCreateMouseEvent(None, kCGEventMouseMoved, p, 0))


def _click(p: CGPoint, double: bool = False, right: bool = False) -> None:
    """click() cadence from mirroring/driver: move, 0.15s, down, 0.05s, up —
    but NO cursor-restore warp (screen control owns the pointer)."""
    _move(p)
    time.sleep(0.15)
    down_t, up_t = ((kCGEventRightMouseDown, kCGEventRightMouseUp) if right
                    else (kCGEventLeftMouseDown, kCGEventLeftMouseUp))
    btn = 1 if right else 0
    down = AX.CGEventCreateMouseEvent(None, down_t, p, btn)
    if double:
        AX.CGEventSetIntegerValueField(down, kCGMouseEventClickState, 2)
    AX.CGEventPost(kCGHIDEventTap, down)
    time.sleep(0.05)
    AX.CGEventPost(kCGHIDEventTap,
                   AX.CGEventCreateMouseEvent(None, up_t, p, btn))


def _drag(p1: CGPoint, p2: CGPoint) -> None:
    _move(p1)
    time.sleep(0.1)
    AX.CGEventPost(kCGHIDEventTap,
                   AX.CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, p1, 0))
    for i in range(1, 11):  # <= 10 intermediate moves, 0.03s apart
        f = i / 11
        _move(CGPoint(p1.x + (p2.x - p1.x) * f, p1.y + (p2.y - p1.y) * f))
        time.sleep(0.03)
    AX.CGEventPost(kCGHIDEventTap,
                   AX.CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, p2, 0))


def _scroll(dy: float, dx: float = 0.0) -> None:
    ev = AX.CGEventCreateScrollWheelEvent2(None, kCGScrollEventUnitLine, 1,
                                           int(dy * 10), int(dx * 10), 0)
    AX.CGEventPost(kCGHIDEventTap, ev)


def _type_text(text: str) -> None:
    """keycode-0 keyboard events carrying UTF-16 in UNICODE_CHUNK chunks —
    exact type_text pattern from mirroring/driver."""
    units = text.encode("utf-16-le")
    for i in range(0, len(units), UNICODE_CHUNK * 2):
        chunk = units[i:i + UNICODE_CHUNK * 2]
        buf = ctypes.create_string_buffer(chunk, len(chunk))
        n = len(chunk) // 2
        for down in (True, False):
            ev = AX.CGEventCreateKeyboardEvent(None, 0, down)
            AX.CGEventKeyboardSetUnicodeString(ev, n, buf)
            AX.CGEventPost(kCGHIDEventTap, ev)
            time.sleep(0.02)


def _key(key: str, mods: list[str]) -> None:
    mask = 0
    for m in mods:
        mask |= _FLAG_MASKS[m]
    for down in (True, False):
        ev = AX.CGEventCreateKeyboardEvent(None, KEYCODES[key], down)
        if down and mask:
            AX.CGEventSetFlags(ev, mask)
        AX.CGEventPost(kCGHIDEventTap, ev)
        time.sleep(0.02)
