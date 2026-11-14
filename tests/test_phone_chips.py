"""Keep the iPhone app's screen chips and gateway.siri._PHONE_SCREEN_CMDS in sync.

Phone callers only launch a screen on an exact chip payload, so a chip that is
renamed in App.swift (or a dict key that is renamed) silently breaks the button.
"""

from __future__ import annotations

import re
from pathlib import Path

from gateway import siri

APP_SWIFT = (
    Path(__file__).resolve().parent.parent
    / "ios" / "JarvisCompanion" / "JarvisCompanion" / "App.swift"
)
_CHIP_RE = re.compile(r'Chip\(label:\s*"([^"]*)",\s*payload:\s*"([^"]*)"\)')


def _screen_payloads() -> set[str]:
    src = APP_SWIFT.read_text(encoding="utf-8")
    return {
        payload.strip().lower()
        for _label, payload in _CHIP_RE.findall(src)
        if payload.strip().lower().endswith("screen")
    }


def test_at_least_three_screen_chips_found() -> None:
    assert len(_screen_payloads()) >= 3


def test_every_screen_chip_is_a_siri_key() -> None:
    for payload in _screen_payloads():
        assert payload in siri._PHONE_SCREEN_CMDS, payload


def test_every_siri_key_is_sent_by_a_chip() -> None:
    sent = _screen_payloads()
    for key in siri._PHONE_SCREEN_CMDS:
        assert key in sent, key
