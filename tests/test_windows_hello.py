"""Q2 hello-envelope MAC tests: `_adapter_mac` picks the first non-virtual adapter,
`uuid.getnode` is only the fallback when no non-virtual interface matches.

No network: `psutil.net_if_addrs` (and `uuid.getnode` in the fallback test) are
monkeypatched, exactly the seam style of tests/test_wake.py.
"""

from __future__ import annotations

import json

import psutil
import pytest

try:  # psutil >= 7 moved the namedtuple module
    from psutil._ntuples import snicaddr as snic
except ImportError:
    from psutil._common import snic

from windows.agent import main


@pytest.fixture
def fake_addrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        psutil,
        "net_if_addrs",
        lambda: {
            "Tailscale": [snic(family=psutil.AF_LINK, address="", netmask=None, broadcast=None, ptp=None)],
            "Wi-Fi": [snic(family=psutil.AF_LINK, address="<mac-wifi>", netmask=None, broadcast=None, ptp=None)],
        },
    )


def test_hello_reports_wifi_mac(fake_addrs) -> None:
    envelope = json.loads(main._hello_envelope())
    assert envelope["payload"] == {"mac": "<mac-wifi>"}


def test_hello_falls_back_to_getnode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: {})
    monkeypatch.setattr(main.uuid, "getnode", lambda: 0xAABBCCDDEE02)
    envelope = json.loads(main._hello_envelope())
    assert envelope["payload"] == {"mac": "aa:bb:cc:dd:ee:02"}
