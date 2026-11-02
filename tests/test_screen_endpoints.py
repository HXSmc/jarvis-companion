"""SC2: /screen endpoints — cookie handshake, frames, input limits, kill switch.

Uses the shared `gateway_server` fixture (conftest.py) with fixture tokens;
`mac.screen.driver` (SC1) is stubbed via sys.modules when absent, so these
tests pass before and after SC1 merges — the real module wins once it exists.
The mac capture/sender are monkeypatched on whichever driver module resolved.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import re
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from conftest import AUTH_HEADER, IOS_AUTH_HEADER

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

from gateway import screen

FAKE_JPEG = b"fake-jpeg-bytes-sc2"


@pytest.fixture(autouse=True)
def _fresh_input_window():
    """The 60 events / 2 s limiter is module-global; reset it so suite speed never decides a test."""
    screen._input_times.clear()


@pytest.fixture
def fake_capture(monkeypatch):
    calls: list[tuple[int, int]] = []

    def capture_jpeg(w: int, q: int):
        calls.append((w, q))
        return FAKE_JPEG, w, q

    monkeypatch.setattr(mac_driver, "capture_jpeg", capture_jpeg)
    return calls


@pytest.fixture
def fake_send_events(monkeypatch):
    calls: list[list[dict]] = []
    monkeypatch.setattr(mac_driver, "send_events", lambda events: calls.append(events))
    return calls


def _tap() -> dict:
    return {"type": "tap", "x": 0.5, "y": 0.5}


def test_screen_no_auth_is_401(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen")
            assert resp.status_code == 401, resp.text
            assert resp.json() == {"detail": "not authenticated"}, resp.text

    asyncio.run(run())


def test_shared_token_is_403(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen", headers=AUTH_HEADER)
            assert resp.status_code == 403, resp.text
            assert resp.json() == {"detail": "shared token not valid for screen"}, resp.text
            resp = await http.get("/screen/mac/frame", headers=AUTH_HEADER)
            assert resp.status_code == 403, resp.text

    asyncio.run(run())


def test_handshake_mints_cookie_and_sixth_session_evicts(gateway_server, fake_capture):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            cookies = []
            for _ in range(6):
                resp = await http.get("/screen", headers=IOS_AUTH_HEADER)
                assert resp.status_code == 200, resp.text
                set_cookie = resp.headers["set-cookie"]
                # Exact attribute: /screen (no trailing slash) so the bare /screen
                # route path-matches too (RFC 6265 — /screen is not a prefix of
                # itself under Path=/screen/).
                assert "Path=/screen;" in set_cookie or set_cookie.endswith("Path=/screen"), set_cookie
                for flag in ("HttpOnly", "Secure", "SameSite=strict", "Max-Age=43200"):
                    assert flag in set_cookie, set_cookie
                cookies.append(resp.cookies.get("screen_session"))
            # 6th mint evicted the 1st of the 5 live sessions.
            resp = await http.get(
                "/screen/mac/frame", headers={"Cookie": f"screen_session={cookies[0]}"}
            )
            assert resp.status_code == 401, resp.text
            resp = await http.get(
                "/screen/mac/frame", headers={"Cookie": f"screen_session={cookies[5]}"}
            )
            assert resp.status_code == 200, resp.text

    asyncio.run(run())


def test_frame_etag_and_304(gateway_server, fake_capture):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen/mac/frame", headers=IOS_AUTH_HEADER, params={"w": 400, "q": 30})
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"].startswith("image/jpeg"), resp.headers
            etag = hashlib.sha1(FAKE_JPEG).hexdigest()
            assert resp.headers["etag"] == etag, resp.headers
            resp = await http.get(
                "/screen/mac/frame",
                headers={**IOS_AUTH_HEADER, "If-None-Match": f'"{etag}"'},
                params={"w": 400, "q": 30},
            )
            assert resp.status_code == 304, resp.text
            assert fake_capture == [(400, 30), (400, 30)], fake_capture

    asyncio.run(run())


def test_windows_offline_frame_is_503(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen/windows/frame", headers=IOS_AUTH_HEADER)
            assert resp.status_code == 503, resp.text
            assert resp.json() == {"detail": "pc offline"}, resp.text

    asyncio.run(run())


def test_input_batch_limits(gateway_server, fake_send_events):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            url = "/screen/mac/input"
            # 21 events and 0 events are both 422 (pydantic bounds).
            resp = await http.post(url, headers=IOS_AUTH_HEADER, json={"events": [_tap()] * 21})
            assert resp.status_code == 422, resp.text
            resp = await http.post(url, headers=IOS_AUTH_HEADER, json={"events": []})
            assert resp.status_code == 422, resp.text
            # 3 x 20 = 60 events inside the 2 s window pass; the 4th batch hits
            # the 60/2 s limit -> 429.
            for _ in range(3):
                resp = await http.post(url, headers=IOS_AUTH_HEADER, json={"events": [_tap()] * 20})
                assert resp.status_code == 200, resp.text
            assert len(fake_send_events) == 3, fake_send_events
            resp = await http.post(url, headers=IOS_AUTH_HEADER, json={"events": [_tap()]})
            assert resp.status_code == 429, resp.text

    asyncio.run(run())


def test_open_name_regex_is_422(gateway_server):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/screen/mac/open", headers=IOS_AUTH_HEADER, json={"name": "notes; rm -rf"}
            )
            assert resp.status_code == 422, resp.text

    asyncio.run(run())


def test_boot_nonce_mints_cookie_and_is_consumed(gateway_server, fake_capture):
    async def run():
        screen._BOOTSTRAPS.clear()
        nonce = "nonce-sc2-ok"
        screen._BOOTSTRAPS[nonce] = time.time() + 60
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen", params={"boot": nonce})
            assert resp.status_code == 200, resp.text
            cookie = resp.cookies.get("screen_session")
            assert cookie, resp.headers
            assert nonce not in screen._BOOTSTRAPS  # single-use
            resp = await http.get(
                "/screen/mac/frame", headers={"Cookie": f"screen_session={cookie}"}
            )
            assert resp.status_code == 200, resp.text

    asyncio.run(run())


def test_session_cookie_path_is_screen_exact(gateway_server):
    """Set-Cookie must carry Path=/screen (no trailing slash) as its own attribute."""
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen", headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200, resp.text
            set_cookie = resp.headers["set-cookie"]
            assert "Path=/screen;" in set_cookie or set_cookie.endswith("Path=/screen"), set_cookie
            assert "Path=/screen/" not in set_cookie, set_cookie

    asyncio.run(run())


def test_consumed_or_bogus_nonce_with_valid_cookie_serves_page(gateway_server):
    """Reload of a once-used boot link stays 200 when the session cookie is valid."""
    async def run():
        screen._BOOTSTRAPS.clear()
        nonce = "nonce-sc2-reload"
        screen._BOOTSTRAPS[nonce] = time.time() + 60
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen", params={"boot": nonce})
            assert resp.status_code == 200, resp.text
            cookie = resp.cookies.get("screen_session")
            assert cookie, resp.headers
            # Secure cookie — httpx won't jar it over http, so send it explicitly.
            cookie_header = {"Cookie": f"screen_session={cookie}"}
            resp = await http.get("/screen", params={"boot": nonce}, headers=cookie_header)
            assert resp.status_code == 200, resp.text
            resp = await http.get("/screen", params={"boot": "bogus"}, headers=cookie_header)
            assert resp.status_code == 200, resp.text

    asyncio.run(run())


def test_boot_nonce_reuse_and_bogus_are_401(gateway_server):
    async def run():
        screen._BOOTSTRAPS.clear()
        nonce = "nonce-sc2-once"
        screen._BOOTSTRAPS[nonce] = time.time() + 60
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/screen", params={"boot": nonce})
            assert resp.status_code == 200, resp.text
        # Bad nonces with NO valid cookie -> 401 as before.
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as bare:
            resp = await bare.get("/screen", params={"boot": nonce})  # reused
            assert resp.status_code == 401, resp.text
            resp = await bare.get("/screen", params={"boot": "bogus"})
            assert resp.status_code == 401, resp.text

    asyncio.run(run())


def test_kill_switch_unmounts_router(monkeypatch):
    """SCREEN_CONTROL=0 -> /screen/* is 404; the app reloads back on afterwards."""
    import gateway.app as app_module

    monkeypatch.setenv("SCREEN_CONTROL", "0")
    try:
        app_module = importlib.reload(app_module)
        transport = httpx.ASGITransport(app=app_module.app)
        async def run():
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                for path in ("/screen", "/screen/mac/frame"):
                    resp = await http.get(path, headers=IOS_AUTH_HEADER)
                    assert resp.status_code == 404, (path, resp.status_code)

        asyncio.run(run())
    finally:
        monkeypatch.delenv("SCREEN_CONTROL", raising=False)
        importlib.reload(app_module)


def test_cursor_toggle_leads_scrollable_key_row(gateway_server):
    """Phone-width fix: the Cursor toggle ships first in a horizontally
    scrollable key row (12 buttons x 40px min + 11 x 6px gap = 546px,
    wider than any iPhone — without overflow-x the trailing buttons,
    shift and Cursor, render off-screen)."""
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            for target in ("mac", "windows"):
                resp = await http.get("/screen", params={"target": target}, headers=IOS_AUTH_HEADER)
                assert resp.status_code == 200, (target, resp.text)
                html = resp.text
                assert 'id="cursor-toggle"' in html, target
                assert "function initialCursor" in html, target  # cursor.js inlined
                assert re.search(r"\.row\.keys\s*\{[^}]*overflow-x:\s*auto", html), target
                assert '<div class="row keys">' in html, target
                # toggle is the first button in the key row, before any key button
                assert html.index('id="cursor-toggle"') < html.index('class="key"'), target

    asyncio.run(run())


def test_key_events_accept_chars_and_every_key_maps_on_mac_and_windows():
    """Letters/digits/punctuation are valid keys (cmd+L, cmd+V, cmd+A) and
    every _Key literal has a Mac keycode and a Windows VK."""
    import ast
    import typing

    for key in ("l", "v", "a", "5", "\\", "/"):
        screen.ScreenEvent(type="key", key=key, mods=["cmd"])
    for bad in ("ab", "A", "é"):
        with pytest.raises(ValueError):
            screen.ScreenEvent(type="key", key=bad)

    keys = set(typing.get_args(screen._Key))
    src = (Path(__file__).resolve().parent.parent / "mac/screen/driver.py").read_text()
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "KEYCODES")
    mac_codes = ast.literal_eval(node.value)
    assert keys == set(mac_codes), keys ^ set(mac_codes)
    assert len(set(mac_codes.values())) == len(mac_codes)  # no duplicate keycodes

    from windows.agent import commands
    assert keys == set(commands.VK), keys ^ set(commands.VK)


@pytest.mark.parametrize("event", [
    {"type": "tap", "x": 0.5, "y": 0.5, "require_app": "T"},
    {"type": "text", "text": "a", "require_window": "w"},
    {"type": "text", "text": "a", "require_app": "x" * 65},
    {"type": "text", "text": "a", "require_app": "T", "require_window": "w" * 129},
    {"type": "text", "text": "a", "require_app": ""},
])
def test_require_app_field_rules_reject_bad_values(event):
    """Focus lock fields: text/key only, require_window needs require_app,
    length caps 64/128, no empty strings."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        screen.ScreenEvent(**event)


def test_require_app_on_scroll_event_is_422_before_driver(gateway_server, fake_send_events):
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post("/screen/mac/input", headers=IOS_AUTH_HEADER,
                                   json={"events": [{"type": "scroll", "dy": 0.5, "require_app": "T"}]})
            assert resp.status_code == 422, resp.text

    asyncio.run(run())
    assert fake_send_events == []


def test_focus_mismatch_is_409_with_detail_and_no_window_title(monkeypatch, gateway_server):
    def refused(events):
        raise mac_driver.FocusMismatch(index=1, frontmost_app="iTerm2", partial=False)

    monkeypatch.setattr(mac_driver, "send_events", refused)

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            events = [{"type": "key", "key": "a", "require_app": "Terminal", "require_window": "secret-doc-title"},
                      {"type": "text", "text": "b", "require_app": "Terminal", "require_window": "secret-doc-title"}]
            resp = await http.post("/screen/mac/input", headers=IOS_AUTH_HEADER, json={"events": events})
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            assert detail["error"] == "focus_mismatch", detail
            assert detail["frontmost_app"] == "iTerm2", detail
            assert detail["refused_index"] == 1, detail
            assert detail["events_run"] == 1, detail
            assert detail["partial"] is False, detail
            assert "secret-doc-title" not in resp.text, resp.text

    asyncio.run(run())


def test_focus_mismatch_partial_appends_message(monkeypatch, gateway_server):
    def refused(events):
        raise mac_driver.FocusMismatch(index=1, frontmost_app="iTerm2", partial=True)

    monkeypatch.setattr(mac_driver, "send_events", refused)

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            events = [{"type": "text", "text": "a", "require_app": "Terminal"},
                      {"type": "text", "text": "b", "require_app": "Terminal"}]
            resp = await http.post("/screen/mac/input", headers=IOS_AUTH_HEADER, json={"events": events})
            assert resp.status_code == 409, resp.text
            message = resp.json()["detail"]["message"]
            assert "partially" in message and "resend from index 1" in message, resp.text

    asyncio.run(run())


def test_focus_mismatch_unknown_app_points_to_accessibility(monkeypatch, gateway_server):
    def refused(events):
        raise mac_driver.FocusMismatch(index=0, frontmost_app=None, partial=False)

    monkeypatch.setattr(mac_driver, "send_events", refused)

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            events = [{"type": "key", "key": "return", "require_app": "Terminal"}]
            resp = await http.post("/screen/mac/input", headers=IOS_AUTH_HEADER, json={"events": events})
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            assert detail["frontmost_app"] is None, detail
            assert "Accessibility permission" in detail["message"], detail

    asyncio.run(run())


def test_require_app_on_windows_target_is_422_without_agent_call(gateway_server, monkeypatch):
    """Windows input rejects focus-lock fields before the agent roundtrip."""
    calls: list[tuple] = []

    async def never(*args, **kwargs):
        calls.append(args)
        raise AssertionError("agent_roundtrip must not be called")

    monkeypatch.setattr(screen, "agent_roundtrip", never)

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/screen/windows/input", headers=IOS_AUTH_HEADER,
                json={"events": [{"type": "text", "text": "a", "require_app": "Terminal"}]},
            )
            assert resp.status_code == 422, resp.text
            assert resp.json() == {"detail": "require_app/require_window are only supported on target mac"}, resp.text

    asyncio.run(run())
    assert calls == []
