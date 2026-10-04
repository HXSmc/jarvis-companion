"""iPhone Mirroring driver: screenshot + synthetic tap/swipe/type through the
Mirroring window.

  python3 -m mac.mirroring.driver --screenshot /tmp/jc_mirror.png
  python3 -m mac.mirroring.driver --dry-run --tap 100,200
  python3 -m mac.mirroring.driver --self-test

Probe-vs-actuate split (jarvis-phone daemon/dialer.py pattern): window.py
probes read-only (CGWindowList + AX); this module actuates via CGEvent
synthetic input (jarvis-phone scripts/banner_click.py click engine) and ONLY
after a successful find — a missing window is reported, never clicked blind —
and only while the Mirroring window's owner is the frontmost app, because
synthetic events land wherever focus is (otherwise exit 3, nothing posted).
--dry-run resolves against the built-in fake window so it works with Mirroring
closed and never posts events; `--type -` reads the text from stdin so it
never appears in `ps`.

Pure ctypes against macOS system frameworks — no pyobjc, no new deps.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import sys
import time
from pathlib import Path

from mac.mirroring import window
from mac.mirroring.window import (FAKE_WINDOW, MirroringWindowNotFoundError,
                                  MirrorWindow)

# Synthetic-input engine, ported verbatim-in-spirit from banner_click.py: same
# CDLL handle (ApplicationServices re-exports the CGEvent symbols there), same
# prototypes, same move->down->up click with cursor restore.
AX = window.AX
CGPoint = window.CGPoint
CGRect = window.CGRect
CG = window.CG
CF = window.CF

AX.CGEventCreate.restype = ctypes.c_void_p
AX.CGEventCreate.argtypes = [ctypes.c_void_p]
AX.CGEventGetLocation.restype = CGPoint
AX.CGEventGetLocation.argtypes = [ctypes.c_void_p]
AX.CGEventCreateMouseEvent.restype = ctypes.c_void_p
AX.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
AX.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
AX.CGWarpMouseCursorPosition.argtypes = [CGPoint]
AX.AXIsProcessTrusted.restype = ctypes.c_bool
AX.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
AX.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
AX.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                               ctypes.c_void_p]

kCGHIDEventTap = 0
kCGEventMouseMoved, kCGEventLeftMouseDown, kCGEventLeftMouseUp = 5, 1, 2
# CGEventKeyboardSetUnicodeString carries at most 20 UTF-16 code units per event
UNICODE_CHUNK = 20

# Built-in fixture PNG (78x156 RGB, generated with zlib+struct — no imaging
# dep): the "--screenshot" dry-run target and the self-test image probe.
FIXTURE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAE4AAACcCAIAAABePgpuAAAArUlEQVR42u3PMQ0AAAgDsMnh4MS/"
    "NjRw06QGmup5Iqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
    "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
    "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqjcLsYpBuGkyVTkAAAAASUVO"
    "RK5CYII="
)
FIXTURE_PNG = base64.b64decode(FIXTURE_PNG_B64)


def resolve_window(dry_run: bool) -> MirrorWindow:
    """Real window when present; fake window only under --dry-run.

    Non-dry-run with Mirroring closed raises — the caller prints the error
    and exits 2 rather than clicking blind."""
    try:
        return window.find_mirroring_window()
    except MirroringWindowNotFoundError:
        if dry_run:
            return FAKE_WINDOW
        raise


def screenshot(win: MirrorWindow, path: str, dry_run: bool) -> int:
    """Quartz CGWindowListCreateImage of the window -> PNG at `path`.

    Dry-run writes the built-in fixture PNG instead — never captures, never
    touches the real window."""
    if dry_run:
        Path(path).write_bytes(FIXTURE_PNG)
        print(f"screenshot (dry-run fixture) -> {path} (window {win.window_id})")
        return 0
    x, y, w, h = win.frame
    rect = CGRect(CGPoint(x, y), CGPoint(w, h))
    img = CG.CGWindowListCreateImage(rect, 0, win.window_id, 0)
    if not img:
        print("screenshot failed: CGWindowListCreateImage returned NULL "
              "(screen recording permission?)", file=sys.stderr)
        return 3
    _write_png(img, path)
    print(f"screenshot -> {path} (window {win.window_id})")
    return 0


def _write_png(img, path: str) -> None:
    """CGImage -> PNG file via ImageIO's CGImageDestination (ctypes only)."""
    IIO = ctypes.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")
    CF.CFURLCreateWithFileSystemPath.restype = ctypes.c_void_p
    CF.CFURLCreateWithFileSystemPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.c_int, ctypes.c_bool]
    IIO.CGImageDestinationCreateWithURL.restype = ctypes.c_void_p
    IIO.CGImageDestinationCreateWithURL.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                    ctypes.c_size_t, ctypes.c_void_p]
    IIO.CGImageDestinationAddImage.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_void_p]
    IIO.CGImageDestinationFinalize.restype = ctypes.c_bool
    IIO.CGImageDestinationFinalize.argtypes = [ctypes.c_void_p]
    url = CF.CFURLCreateWithFileSystemPath(
        None, window._cfstr(path), 0, False)  # 0 = kCFURLPOSIXPathStyle
    dest = IIO.CGImageDestinationCreateWithURL(
        url, window._cfstr("public.png"), 1, None)
    if not dest:
        raise OSError(f"CGImageDestinationCreateWithURL failed for {path}")
    IIO.CGImageDestinationAddImage(dest, img, None)
    if not IIO.CGImageDestinationFinalize(dest):
        raise OSError(f"CGImageDestinationFinalize failed for {path}")


def click(x: float, y: float) -> None:
    """Synthetic left click at global (x, y), cursor restored afterwards
    (banner_click.py click engine, unchanged)."""
    origin = AX.CGEventGetLocation(AX.CGEventCreate(None))
    p = CGPoint(x, y)
    AX.CGEventPost(kCGHIDEventTap, AX.CGEventCreateMouseEvent(None, kCGEventMouseMoved, p, 0))
    time.sleep(0.15)
    AX.CGEventPost(kCGHIDEventTap, AX.CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, p, 0))
    time.sleep(0.05)
    AX.CGEventPost(kCGHIDEventTap, AX.CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, p, 0))
    time.sleep(0.1)
    AX.CGWarpMouseCursorPosition(origin)


def swipe(x1: float, y1: float, x2: float, y2: float) -> None:
    """Synthetic left-button drag between two global points."""
    origin = AX.CGEventGetLocation(AX.CGEventCreate(None))
    p1, p2 = CGPoint(x1, y1), CGPoint(x2, y2)
    AX.CGEventPost(kCGHIDEventTap, AX.CGEventCreateMouseEvent(None, kCGEventMouseMoved, p1, 0))
    time.sleep(0.1)
    AX.CGEventPost(kCGHIDEventTap, AX.CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, p1, 0))
    for frac in (0.25, 0.5, 0.75):
        p = CGPoint(x1 + (x2 - x1) * frac, y1 + (y2 - y1) * frac)
        AX.CGEventPost(kCGHIDEventTap, AX.CGEventCreateMouseEvent(None, kCGEventMouseMoved, p, 0))
        time.sleep(0.05)
    AX.CGEventPost(kCGHIDEventTap, AX.CGEventCreateMouseEvent(None, kCGEventMouseMoved, p2, 0))
    time.sleep(0.05)
    AX.CGEventPost(kCGHIDEventTap, AX.CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, p2, 0))
    time.sleep(0.1)
    AX.CGWarpMouseCursorPosition(origin)


def type_text(text: str) -> None:
    """Synthetic keystrokes carrying the text as Unicode (keycode 0 — the
    attached string is what gets delivered)."""
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


def _check_trusted() -> int | None:
    """banner_click.py rule: CGEventPost from an Accessibility-untrusted
    process is swallowed silently — say so instead of clicking into the void."""
    if not AX.AXIsProcessTrusted():
        print("NOT Accessibility-trusted — CGEventPost will be swallowed "
              "silently", file=sys.stderr)
        return 3
    return None


def _frontmost_pid() -> int | None:
    """PID owning the first on-screen layer-0 window: CGWindowList is
    front-to-back, so that window belongs to the frontmost application."""
    for w in window._enum_windows():
        if w.get("layer") == 0:
            return w["pid"]
    return None


def _check_frontmost(win: MirrorWindow) -> int | None:
    """LOW-6 focus guard: CGEvents land wherever focus is, so posting while
    another app is frontmost would type/click into it. Nonzero exit code,
    nothing posted, unless the Mirroring window's owner IS frontmost."""
    if _frontmost_pid() != win.pid:
        print("mirroring window not frontmost")
        return 3
    return None


def _parse_coords(s: str, n: int, what: str) -> list[float]:
    parts = [p for p in s.split(",")]
    if len(parts) != n or not all(p.strip().lstrip("-").replace(".", "", 1).isdigit()
                                  for p in parts):
        raise SystemExit(f"{what}: expected {n} comma-separated numbers, got {s!r}")
    return [float(p) for p in parts]


def self_test() -> int:
    """No-Mirroring proof of the probe/actuate split: probe logic against the
    built-in fake window + fixture PNG, coordinate math pinned by asserts."""
    fake_list = [
        {"id": 12, "owner": "Dock", "pid": 200, "layer": 0,
         "bounds": (0.0, 2400.0, 3000.0, 100.0)},
        {"id": 99, "owner": "iPhone Mirroring", "pid": 321, "layer": 0,
         "bounds": (100.0, 200.0, 390.0, 844.0)},
        # same owner, floating layer -> must be skipped
        {"id": 98, "owner": "iPhone Mirroring", "pid": 321, "layer": 3,
         "bounds": (0.0, 0.0, 5000.0, 5000.0)},
    ]
    win = window.pick_mirroring_window(fake_list)
    assert win is not None, "fake Mirroring window not found in fixture list"
    assert win.window_id == 99 and win.pid == 321, win
    assert win.frame == (100.0, 200.0, 390.0, 844.0), win
    # largest-area rule: a smaller Mirroring window loses to the bigger one
    smaller = dict(fake_list[1], id=77, bounds=(0.0, 0.0, 100.0, 200.0))
    assert window.pick_mirroring_window([fake_list[1], smaller]).window_id == 99
    # no matching owner -> None -> find_mirroring_window's error path
    assert window.pick_mirroring_window([fake_list[0]]) is None
    # window-relative -> screen translation pinned to FAKE_WINDOW's frame
    assert window.to_screen(FAKE_WINDOW.frame, 100, 200) == (100.0, 200.0)
    assert window.to_screen((100.0, 200.0, 390.0, 844.0), 0, 0) == (100.0, 200.0)
    assert window.to_screen((100.0, 200.0, 390.0, 844.0), 390, 844) == (490.0, 1044.0)
    # fake window resolves under dry-run even with Mirroring closed
    got = resolve_window(dry_run=True)
    assert got.window_id is not None and len(got.frame) == 4, got
    # fixture PNG is a real PNG of the expected size
    assert FIXTURE_PNG[:8] == b"\x89PNG\r\n\x1a\n", "fixture PNG magic bytes"
    w = int.from_bytes(FIXTURE_PNG[16:20], "big")
    h = int.from_bytes(FIXTURE_PNG[20:24], "big")
    assert (w, h) == (78, 156), f"fixture PNG is {w}x{h}, expected 78x156"
    print("mac.mirroring self-test OK "
          f"(fake window id={FAKE_WINDOW.window_id}, fixture PNG {w}x{h})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="iPhone Mirroring tap-through driver")
    ap.add_argument("--screenshot", metavar="PATH",
                    help="capture the Mirroring window to a PNG")
    ap.add_argument("--tap", metavar="X,Y", help="tap at window-relative coords")
    ap.add_argument("--swipe", metavar="X1,Y1,X2,Y2",
                    help="swipe between two window-relative points")
    ap.add_argument("--type", dest="type_text_arg", metavar="TEXT",
                    help="type text into the Mirroring window ('-' reads stdin)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the action instead of clicking; never requires "
                         "the real window")
    ap.add_argument("--self-test", action="store_true",
                    help="probe logic against the built-in fake window + "
                         "fixture PNG; no real window needed")
    args = ap.parse_args(argv)

    if args.type_text_arg == "-":
        # stdin, never argv: the text must not show up in `ps` (LOW-6).
        args.type_text_arg = sys.stdin.read()

    if args.self_test:
        return self_test()

    actions = [a for a in (args.screenshot, args.tap, args.swipe,
                           args.type_text_arg) if a is not None]
    if not actions:
        ap.error("one of --screenshot/--tap/--swipe/--type (or --self-test) "
                 "is required")

    try:
        win = resolve_window(args.dry_run)
    except MirroringWindowNotFoundError:
        print("mirroring window not found")
        return 2

    if args.screenshot:
        rc = screenshot(win, args.screenshot, args.dry_run)
        if rc:
            return rc

    if args.tap:
        x, y = _parse_coords(args.tap, 2, "--tap")
        if args.dry_run:
            print(f"TAP {x:g} {y:g} (window {win.window_id})")
        else:
            rc = _check_frontmost(win)
            if rc is not None:
                return rc
            rc = _check_trusted()
            if rc is not None:
                return rc
            click(*window.to_screen(win.frame, x, y))

    if args.swipe:
        x1, y1, x2, y2 = _parse_coords(args.swipe, 4, "--swipe")
        if args.dry_run:
            print(f"SWIPE {x1:g} {y1:g} {x2:g} {y2:g} (window {win.window_id})")
        else:
            rc = _check_frontmost(win)
            if rc is not None:
                return rc
            rc = _check_trusted()
            if rc is not None:
                return rc
            swipe(*window.to_screen(win.frame, x1, y1),
                  *window.to_screen(win.frame, x2, y2))

    if args.type_text_arg:
        if args.dry_run:
            print(f"TYPE {args.type_text_arg} (window {win.window_id})")
        else:
            rc = _check_frontmost(win)
            if rc is not None:
                return rc
            rc = _check_trusted()
            if rc is not None:
                return rc
            type_text(args.type_text_arg)

    return 0


if __name__ == "__main__":
    sys.exit(main())
