"""S1: cursor mode — inline serving, pure-logic math under node, payloads.

cursor.js is dual-use (plain script in the page, module.exports under node);
the page route splices it in at serve time. Same mac-driver sys.modules stub
pattern as test_screen_endpoints.py so these pass regardless of SC1 state.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from conftest import IOS_AUTH_HEADER

try:
    from mac.screen import driver as mac_driver
except ModuleNotFoundError:
    mac_driver = types.ModuleType("mac.screen.driver")
    mac_driver.capture_jpeg = lambda w, q: (b"", w, q)
    mac_driver.send_events = lambda events: None
    mac_driver.tcc_ok = lambda: True
    mac_driver.display_asleep = lambda: False
    _pkg = types.ModuleType("mac.screen")
    _pkg.driver = mac_driver
    sys.modules["mac.screen"] = _pkg
    sys.modules["mac.screen.driver"] = mac_driver

CURSOR_JS = Path(__file__).resolve().parent.parent / "gateway" / "static" / "cursor.js"


@pytest.fixture
def fake_send_events(monkeypatch):
    calls: list[list[dict]] = []
    monkeypatch.setattr(mac_driver, "send_events", lambda events: calls.append(events))
    return calls


def test_page_has_cursor_toggle_and_inlined_module(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen", headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200, resp.text
            body = resp.text
            assert ">Cursor<" in body
            btn = re.search(r'<button[^>]*\bid="cursor-toggle"[^>]*>', body)
            assert btn is not None, "cursor-toggle button tag not found"
            assert 'aria-pressed="false"' in btn.group(0)  # on that same tag
            assert "function nextCursor" in body  # cursor.js was inlined
            assert "/*CURSOR_JS*/" not in body     # placeholder fully replaced

    asyncio.run(run())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_cursor_math_and_gestures():
    """The shared module's math/gesture rules, exercised via node directly."""
    checks = [
        "c.nextCursor({x:398,y:0},100,-100,1.5,1,400,300)",      # clamps x
        "c.nextCursor({x:0,y:0},20,10,1.5,1,400,300)",           # gain applies
        "c.nextCursor({x:0,y:0},49,0,1.5,0.6,400,300)",          # scale divides
        "c.nextCursor({x:100,y:100},30,0,1.5,2,400,300).x",      # pinned scale vector
        "c.classifyGesture(300,8)",                               # edge: still a tap
        "c.classifyGesture(301,8)",                               # just too slow
        "c.classifyGesture(100,9)",                               # just too far
        "c.classifyGesture(300,8.0001) === 'drag'",               # pinned: dist just past 8
        "c.clickPayload('right',{x:200,y:300},400,600)",
        "c.initialCursor(401,301)",                               # rounds half up
        "c.GAIN===1.5&&c.TAP_MS===300&&c.TAP_DIST_CSS===8&&c.LONGPRESS_MS===500",
    ]
    script = (
        "const c = require(" + json.dumps(str(CURSOR_JS)) + ");\n"
        + "\n".join(f"console.log(JSON.stringify({expr}));" for expr in checks)
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    lines = out.stdout.strip().splitlines()
    assert len(lines) == len(checks), out.stderr
    (clamp, gain, scale, pinned_x, tap, slow, far, pin_far,
     right, init, consts) = (json.loads(x) for x in lines)
    assert clamp == {"x": 399, "y": 0, "frameW": 400, "frameH": 300}
    assert gain == {"x": 30, "y": 15, "frameW": 400, "frameH": 300}
    assert scale["x"] in (122, 123)  # 49*1.5/0.6 = 122.5 -> Math.round, accept 122 or 123
    assert pinned_x == 123           # 30*1.5/2 = 22.5; 100+22.5 = 122.5; Math.round is half-up
    assert tap == "tap" and slow == "drag" and far == "drag"
    assert pin_far is True
    assert right == {"type": "right", "x": 0.5, "y": 0.5}
    assert init == {"x": 201, "y": 151, "frameW": 401, "frameH": 301}
    assert consts is True


def test_cursor_payload_reaches_driver(gateway_server, fake_send_events):
    """The payloads cursor mode sends (tap/right/move at the cursor) pass the
    protocol unchanged: pydantic adds defaults (button, dx, ...), so compare
    per key rather than whole dicts."""
    payloads = [
        {"type": "tap", "x": 0.25, "y": 0.5},
        {"type": "right", "x": 0.5, "y": 0.5},
        {"type": "move", "x": 0.5, "y": 0.5},
    ]
    expected = [(e["type"], e["x"], e["y"]) for e in payloads]

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            for event in payloads:
                resp = await http.post(
                    "/screen/mac/input", headers=IOS_AUTH_HEADER, json={"events": [event]}
                )
                assert resp.status_code == 200, resp.text

    asyncio.run(run())
    assert len(fake_send_events) == 3, fake_send_events
    for got, (etype, x, y) in zip(fake_send_events, expected):
        assert got[0]["type"] == etype
        assert got[0]["x"] == x
        assert got[0]["y"] == y


def test_direct_mode_markup_unchanged(gateway_server):
    """Direct mode keeps its sticky modifier buttons and browser zoom handling."""

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen", headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200, resp.text
            body = resp.text
            for m in ("cmd", "ctrl", "alt", "shift"):
                assert f'data-m="{m}"' in body
            assert "touch-action: manipulation" in body

    asyncio.run(run())
