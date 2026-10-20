"""Split-deploy check: vendor_watch.notify_gateway() also pokes the box.

The Mac mail watcher's guaranteed WhatsApp/email path runs against its own
local gateway call (unchanged); the NEW box call exists only to wake
whichever "ios" websocket is connected to the box, so it must be
best-effort (never raise) and independently gated by its own env var.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "vendor_watch.py"
spec = importlib.util.spec_from_file_location("vendor_watch", MODULE_PATH)
vendor_watch = importlib.util.module_from_spec(spec)
sys.modules.setdefault("vendor_watch", vendor_watch)
spec.loader.exec_module(vendor_watch)  # type: ignore[union-attr]


def test_notify_gateway_calls_mac_and_box(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=10):
        seen.append(req.full_url)
        return None

    monkeypatch.delenv("JARVIS_VENDOR_WATCH_NO_NOTIFY", raising=False)
    monkeypatch.delenv("JARVIS_VENDOR_WATCH_NO_BOX_NOTIFY", raising=False)
    # BOX notify URL is env-driven now — set it so the box leg is exercised
    monkeypatch.setattr(vendor_watch, "BOX_GATEWAY_NOTIFY_URL", "http://box.test/inbox/notify")
    with patch.object(vendor_watch.urllib.request, "urlopen", fake_urlopen):
        vendor_watch.notify_gateway("tok", 2, 5)
    assert seen == [vendor_watch.GATEWAY_NOTIFY_URL, "http://box.test/inbox/notify"]


def test_notify_gateway_box_opt_out(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=10):
        seen.append(req.full_url)
        return None

    monkeypatch.delenv("JARVIS_VENDOR_WATCH_NO_NOTIFY", raising=False)
    monkeypatch.setenv("JARVIS_VENDOR_WATCH_NO_BOX_NOTIFY", "1")
    with patch.object(vendor_watch.urllib.request, "urlopen", fake_urlopen):
        vendor_watch.notify_gateway("tok", 2, 5)
    assert seen == [vendor_watch.GATEWAY_NOTIFY_URL]


def test_notify_gateway_box_failure_never_raises(monkeypatch):
    def fake_urlopen(req, timeout=10):
        raise OSError("box offline")

    monkeypatch.delenv("JARVIS_VENDOR_WATCH_NO_NOTIFY", raising=False)
    monkeypatch.delenv("JARVIS_VENDOR_WATCH_NO_BOX_NOTIFY", raising=False)
    with patch.object(vendor_watch.urllib.request, "urlopen", fake_urlopen):
        vendor_watch.notify_gateway("tok", 2, 5)  # must not raise


def test_notify_gateway_no_notify_env_skips_both(monkeypatch):
    def fake_urlopen(req, timeout=10):
        raise AssertionError("must not be called")

    monkeypatch.setenv("JARVIS_VENDOR_WATCH_NO_NOTIFY", "1")
    with patch.object(vendor_watch.urllib.request, "urlopen", fake_urlopen):
        vendor_watch.notify_gateway("tok", 2, 5)
