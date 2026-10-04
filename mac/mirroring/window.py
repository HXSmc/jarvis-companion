"""Find the iPhone Mirroring window: CGWindowList enumeration + AX frame probing.

Ported from jarvis-phone (copy, not import):
  - scripts/banner_click.py — CGPoint/CGRect ctypes structs, CF/AX function
    prototypes, AXValue CGRect reads, and the discipline that detection is
    read-only (banner_click.py: "Detection is AX ... actuation has to be a
    synthetic mouse click").
  - daemon/dialer.py — the probe-vs-actuate split: everything in THIS module
    only looks; synthetic input lives in driver.py and never runs before a
    successful find. Callers get a clear error instead of a blind click
    (DESIGN.md "Mirroring window missing / locked": "reports failure to the
    caller instead of clicking blind").

Pure ctypes against macOS system frameworks — no pyobjc, no new deps.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from collections import namedtuple

CF = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
CG = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
AX = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework"
                 "/ApplicationServices")


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGPoint)]


CF.CFStringCreateWithCString.restype = ctypes.c_void_p
CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
CF.CFStringGetCString.restype = ctypes.c_bool
CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
CF.CFGetTypeID.restype = ctypes.c_ulong
CF.CFGetTypeID.argtypes = [ctypes.c_void_p]
CF.CFStringGetTypeID.restype = ctypes.c_ulong
CF.CFArrayGetCount.restype = ctypes.c_long
CF.CFArrayGetCount.argtypes = [ctypes.c_void_p]
CF.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
CF.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
CF.CFDictionaryGetValue.restype = ctypes.c_void_p
CF.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
CF.CFNumberGetValue.restype = ctypes.c_bool
CF.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]

CG.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
CG.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
CG.CGWindowListCreateImage.restype = ctypes.c_void_p
CG.CGWindowListCreateImage.argtypes = [CGRect, ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_uint32]

AX.AXUIElementCreateApplication.restype = ctypes.c_void_p
AX.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
AX.AXUIElementCopyAttributeValue.restype = ctypes.c_int
AX.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_void_p)]
AX.AXValueGetValue.restype = ctypes.c_bool
AX.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]

kCFStringEncodingUTF8 = 0x08000100
kCFNumberSInt64Type = 4
kCFNumberDoubleType = 6
kAXValueCGRectType = 3
kCGWindowListOptionOnScreenOnly = 1 << 0
kCGNullWindowID = 0

# CFString key constants are exported symbols of CoreGraphics. Empirically on
# this macOS the window dicts key by the constant objects themselves, so the
# constants — not freshly created CFStrings — are the reliable lookup keys.
K_WINDOW_NUMBER = ctypes.c_void_p.in_dll(CG, "kCGWindowNumber")
K_OWNER_NAME = ctypes.c_void_p.in_dll(CG, "kCGWindowOwnerName")
K_OWNER_PID = ctypes.c_void_p.in_dll(CG, "kCGWindowOwnerPID")
K_LAYER = ctypes.c_void_p.in_dll(CG, "kCGWindowLayer")
K_BOUNDS = ctypes.c_void_p.in_dll(CG, "kCGWindowBounds")
# The bounds sub-dict keys are the CAPITALIZED names, and the values are
# doubles (CGFloat) — both verified live, see report T2.3.
BOUNDS_KEYS = ("X", "Y", "Width", "Height")

OWNER_MATCH = "iphone mirroring"  # lowercased substring match on owner name

# Window found: global top-left-origin frame (x, y, w, h) in points.
MirrorWindow = namedtuple("MirrorWindow", "window_id pid frame")

# Built-in fake window for --dry-run / --self-test with Mirroring closed:
# logical size of an iPhone 14/15 screen, id 0 so dry-run output reads
# "(window 0)" and can never be confused with a real CGWindowID.
FAKE_WINDOW = MirrorWindow(window_id=0, pid=0, frame=(0.0, 0.0, 390.0, 844.0))


class MirroringWindowNotFoundError(Exception):
    """Raised when no iPhone Mirroring window exists — message is the exact
    string driver.py prints on exit 2."""


def _cfstr(s: str):
    return CF.CFStringCreateWithCString(None, s.encode("utf-8"), kCFStringEncodingUTF8)


def _cfstr_value(v) -> str | None:
    if v is None or CF.CFGetTypeID(v) != CF.CFStringGetTypeID():
        return None
    buf = ctypes.create_string_buffer(512)
    if CF.CFStringGetCString(v, buf, 512, kCFStringEncodingUTF8):
        return buf.value.decode("utf-8", "replace")
    return None


def _cfnum_value(v) -> int | None:
    n = ctypes.c_int64()
    if v and CF.CFNumberGetValue(v, kCFNumberSInt64Type, ctypes.byref(n)):
        return n.value
    return None


def _cfdbl_value(v) -> float | None:
    n = ctypes.c_double()
    if v and CF.CFNumberGetValue(v, kCFNumberDoubleType, ctypes.byref(n)):
        return n.value
    return None


def _attr(el, name):
    out = ctypes.c_void_p()
    if AX.AXUIElementCopyAttributeValue(el, _cfstr(name), ctypes.byref(out)) != 0:
        return None
    return out.value


def _attr_str(el, name):
    return _cfstr_value(_attr(el, name))


def _ax_window_frames(pid: int) -> list[tuple[str | None, tuple[float, float, float, float]]]:
    """(title, frame) of every AX window of `pid` — read-only probe.

    Returns [] on any AX failure (not trusted, pid gone): callers keep the
    CGWindowList bounds, which use the same top-left global coordinate space.
    """
    app = AX.AXUIElementCreateApplication(pid)
    if not app:
        return []
    windows = _attr(app, "AXWindows")
    if windows is None:
        return []
    out = []
    for i in range(CF.CFArrayGetCount(windows)):
        el = CF.CFArrayGetValueAtIndex(windows, i)
        frame = None
        v = _attr(el, "AXFrame")
        r = CGRect()
        if v and AX.AXValueGetValue(v, kAXValueCGRectType, ctypes.byref(r)):
            frame = (r.origin.x, r.origin.y, r.size.x, r.size.y)
        if frame is not None:
            out.append((_attr_str(el, "AXTitle"), frame))
    return out


def pick_mirroring_window(windows: list[dict]) -> MirrorWindow | None:
    """Select the Mirroring window from normalized CGWindowList entries.

    Pure — self-test feeds it fixture lists. Entry shape:
    {id, owner, pid, layer, bounds: (x, y, w, h)}. Largest on-screen layer-0
    window owned by the matching process wins; None when nothing matches."""
    best = None
    best_area = 0.0
    for w in windows:
        if w.get("layer") != 0:
            continue  # skip the owner's floating/menu layers
        owner = (w.get("owner") or "").lower()
        if OWNER_MATCH not in owner:
            continue
        x, y, wid, h = w.get("bounds", (0.0, 0.0, 0.0, 0.0))
        area = wid * h
        if area <= best_area:
            continue
        best = MirrorWindow(window_id=w["id"], pid=w["pid"], frame=(x, y, wid, h))
        best_area = area
    return best


def _enum_windows() -> list[dict]:
    """Normalized on-screen window list via CGWindowListCopyWindowInfo."""
    arr = CG.CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                        kCGNullWindowID)
    if not arr:
        return []
    out = []
    for i in range(CF.CFArrayGetCount(arr)):
        d = CF.CFArrayGetValueAtIndex(arr, i)
        if not d:
            continue
        pid = _cfnum_value(CF.CFDictionaryGetValue(d, K_OWNER_PID))
        wid = _cfnum_value(CF.CFDictionaryGetValue(d, K_WINDOW_NUMBER))
        if pid is None or wid is None:
            continue
        bounds = CF.CFDictionaryGetValue(d, K_BOUNDS)
        if not bounds:
            continue
        frame = tuple(
            _cfdbl_value(CF.CFDictionaryGetValue(bounds, _cfstr(k))) or 0.0
            for k in BOUNDS_KEYS
        )
        out.append({
            "id": wid,
            "owner": _cfstr_value(CF.CFDictionaryGetValue(d, K_OWNER_NAME)) or "",
            "pid": pid,
            "layer": _cfnum_value(CF.CFDictionaryGetValue(d, K_LAYER)) or 0,
            "bounds": frame,
        })
    return out


def find_mirroring_window() -> MirrorWindow:
    """Real iPhone Mirroring window, or raise MirroringWindowNotFoundError.

    Probe-only: CGWindowList for identity/bounds, then an AX frame pass for a
    sharper frame when Accessibility vends one. Never posts events."""
    win = pick_mirroring_window(_enum_windows())
    if win is None:
        raise MirroringWindowNotFoundError("mirroring window not found")
    for _title, frame in _ax_window_frames(win.pid):
        # first AX window of the Mirroring process whose frame overlaps the
        # CG bounds is the same window in the sharper space
        if abs(frame[0] - win.frame[0]) < 5 and abs(frame[1] - win.frame[1]) < 5:
            return MirrorWindow(win.window_id, win.pid, frame)
    return win


def to_screen(frame, x: float, y: float) -> tuple[float, float]:
    """Window-relative point -> global screen point (top-left origin)."""
    return (frame[0] + x, frame[1] + y)
