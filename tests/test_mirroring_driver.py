"""Tests for mac.mirroring.driver's unreadable-capture detector: a capture
whose every sampled alpha byte is 0 is CGWindow.h's "windows can't be read"
sentinel (display locked/asleep), not a success. Pure pixel buffers only —
no real window, no capture or Accessibility permission."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

if sys.platform != "darwin":
    pytest.skip("mac.mirroring loads Quartz via ctypes: macOS only", allow_module_level=True)

from mac.mirroring.driver import _alpha_bytes_zero

W, H = 16, 16  # small 32-bpp RGBA frame; alpha byte is offset 3 of each pixel
# the detector's 8x8 grid samples xs/ys {0, 2, 4, 6, 8, 10, 12, 15} at this size
# (px = (W-1)*gx // 7, floor division)


def _rgba_bytes(opaque_xy=(), width: int = W, height: int = H,
                row_pad: int = 0) -> bytes:
    """RGBA buffer (stride = width*4 + row_pad): every pixel fully transparent
    except the given (x, y) -> 0xFF; row padding is 0xAA junk that alpha
    sampling must never mistake for a live alpha byte."""
    stride = width * 4 + row_pad
    buf = bytearray(stride * height)
    for y in range(height):
        pad = y * stride + width * 4
        buf[pad:pad + row_pad] = b"\xaa" * row_pad
    for x, y in opaque_xy:
        buf[y * stride + x * 4 + 3] = 0xFF
    return bytes(buf)


def test_all_transparent_is_unreadable():
    assert _alpha_bytes_zero(_rgba_bytes(), W, H, W * 4, 32, 3) is True


def test_any_sampled_opaque_alpha_is_readable():
    # grid corners + interior: one live alpha byte flips the verdict
    for xy in ((0, 0), (8, 8), (15, 15)):
        assert _alpha_bytes_zero(_rgba_bytes([xy]), W, H, W * 4, 32, 3) is False


def test_row_padding_is_not_sampled_as_alpha():
    assert _alpha_bytes_zero(_rgba_bytes(row_pad=7), W, H, W * 4 + 7, 32, 3) is True
    assert _alpha_bytes_zero(_rgba_bytes([(8, 2)], row_pad=7),
                             W, H, W * 4 + 7, 32, 3) is False


def test_alpha_only_image():
    # kCGImageAlphaOnly: one byte per pixel and the byte IS alpha
    assert _alpha_bytes_zero(bytes(W * H), W, H, W, 8, 0) is True
    assert _alpha_bytes_zero(b"\x00" * (W * H - 1) + b"\x01", W, H, W, 8, 0) is False


def test_null_provider_or_data_is_unreadable(monkeypatch):
    # NULL CGImageGetDataProvider / NULL CGDataProviderCopyData must read as
    # unreadable, never dereferenced in CF (NULL data segfaulted, rc=139).
    # CG/CF stubbed — no real capture, no real image.
    from mac.mirroring import driver

    seen = []
    for name, ret in (("CGImageGetWidth", W), ("CGImageGetHeight", H),
                      ("CGImageGetBitsPerPixel", 32),
                      ("CGImageGetAlphaInfo", 1), ("CGImageGetBitmapInfo", 0)):
        monkeypatch.setattr(driver.CG, name, lambda img, _ret=ret: _ret)
    monkeypatch.setattr(driver.CG, "CGImageGetDataProvider", lambda img: 0)
    monkeypatch.setattr(driver.CG, "CGDataProviderCopyData", lambda p: 0)
    for name in ("CFDataGetBytePtr", "CFDataGetLength", "CFRelease"):
        monkeypatch.setattr(driver.CF, name,
                            lambda *a, _n=name: seen.append(_n))
    assert driver._capture_is_unreadable(object()) is True  # NULL provider
    monkeypatch.setattr(driver.CG, "CGImageGetDataProvider",
                        lambda img: object())
    assert driver._capture_is_unreadable(object()) is True  # NULL copied data
    assert seen == []  # neither NULL reached the CF layer


def test_none_skip_alpha_info_padding_is_readable(monkeypatch):
    # alpha_info 5/6 (kCGImageAlphaNoneSkipLast/First) carry no real alpha —
    # the "alpha" byte is undefined padding, so a capture of all-zero padding
    # bytes is readable, not the transparent-black sentinel.
    # CG/CF stubbed — no real capture, no real image.
    from mac.mirroring import driver

    for name, ret in (("CGImageGetWidth", W), ("CGImageGetHeight", H),
                      ("CGImageGetBitsPerPixel", 32),
                      ("CGImageGetBitmapInfo", 0)):
        monkeypatch.setattr(driver.CG, name, lambda img, _ret=ret: _ret)
    queried = []
    monkeypatch.setattr(driver.CG, "CGImageGetDataProvider",
                        lambda img: queried.append(img))
    for alpha_info in (5, 6):
        monkeypatch.setattr(driver.CG, "CGImageGetAlphaInfo",
                            lambda img, _ai=alpha_info: _ai)
        assert driver._capture_is_unreadable(object()) is False
    assert queried == []  # padding byte never sampled


def test_sentinel_geometry_like_live_capture():
    # the observed failure: 624x1374 capture, every pixel (0, 0, 0, 0)
    w, h, stride = 624, 1374, 624 * 4
    assert _alpha_bytes_zero(bytes(stride * h), w, h, stride, 32, 3) is True
    buf = bytearray(stride * h)
    buf[784 * stride + 356 * 4 + 3] = 0xFF  # opaque pixel on a sampled grid point
    assert _alpha_bytes_zero(bytes(buf), w, h, stride, 32, 3) is False
