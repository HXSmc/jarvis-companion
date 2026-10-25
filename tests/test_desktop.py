"""Desktop page: one-time-link login, the cookie session, and the cookie -> ios_token middleware scope."""

from __future__ import annotations

import asyncio
import os
import re
import stat

import httpx
import pytest

from conftest import AUTH_HEADER, IOS_AUTH_HEADER
from gateway import desktop

CSRF = {"X-Jarvis-Desktop": "1"}


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DESKTOP_SESSIONS_PATH", str(tmp_path / "desktop_sessions.json"))
    monkeypatch.setenv("SCREEN_PUBLIC_BASE", "https://box.example:8790")
    monkeypatch.setenv("JARVIS_INBOX_PATH", str(tmp_path / "inbox.json"))
    monkeypatch.setattr(desktop, "_sessions", None)
    desktop._BOOTS.clear()
    return tmp_path / "desktop_sessions.json"


def run(gateway_server, body):
    async def main():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", follow_redirects=False) as client:
                await body(client)
    asyncio.run(main())


async def login(client) -> str:
    """Mint a link with the ios token, open it, return the session cookie value."""
    r = await client.post("/desktop/launch", headers=IOS_AUTH_HEADER)
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("https://box.example:8790/desktop?boot=")
    r = await client.get("/desktop?" + url.split("?", 1)[1])
    assert r.status_code == 303 and r.headers["location"] == "/desktop"
    set_cookie = r.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert flag.lower() in set_cookie.lower(), set_cookie
    return re.search(r"__Host-jarvis_desktop=([^;]+)", set_cookie).group(1)


def cookie(value: str) -> dict:
    return {"Cookie": f"{desktop.COOKIE}={value}"}


def test_launch_needs_the_ios_token(gateway_server):
    async def body(client):
        assert (await client.post("/desktop/launch")).status_code == 401
        assert (await client.post("/desktop/launch", headers=AUTH_HEADER)).status_code == 401
        assert (await client.post("/desktop/launch", headers=IOS_AUTH_HEADER)).status_code == 200
    run(gateway_server, body)


def test_boot_link_is_single_use_and_page_needs_the_cookie(gateway_server):
    async def body(client):
        r = await client.post("/desktop/launch", headers=IOS_AUTH_HEADER)
        query = r.json()["url"].split("?", 1)[1]
        assert (await client.get("/desktop?" + query)).status_code == 303
        assert (await client.get("/desktop?" + query)).status_code == 401  # reused
        assert (await client.get("/desktop?boot=bogus")).status_code == 401
        assert (await client.get("/desktop")).status_code == 401
        session = await login(client)
        r = await client.get("/desktop", headers=cookie(session))
        assert r.status_code == 200 and "<title>Jarvis</title>" in r.text
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["cache-control"] == "no-store"
    run(gateway_server, body)


def test_cookie_stands_in_for_the_ios_token_on_phone_routes_only(gateway_server):
    async def body(client):
        session = await login(client)
        # Allowlisted GET: no CSRF header needed.
        assert (await client.get("/inbox")).status_code == 401
        assert (await client.get("/inbox", headers=cookie(session))).status_code == 200
        # Allowlisted POST: only with the desktop header.
        assert (await client.post("/inbox/abc/read", headers=cookie(session))).status_code == 401
        assert (await client.post("/inbox/abc/read", headers={**cookie(session), **CSRF})).status_code == 404
        # Not allowlisted: the cookie opens nothing.
        assert (await client.get("/hwmode/requests", headers=cookie(session))).status_code == 401
        assert (await client.post("/desktop/launch", headers={**cookie(session), **CSRF})).status_code == 401
        assert (await client.get("/health", headers=cookie(session))).status_code == 401
        # A request's own Authorization is never replaced.
        assert (await client.get("/inbox", headers={**cookie(session), **AUTH_HEADER})).status_code == 401
        # A forged cookie does nothing.
        assert (await client.get("/inbox", headers=cookie("forged"))).status_code == 401
    run(gateway_server, body)


def test_logout_ends_the_session(gateway_server):
    async def body(client):
        session = await login(client)
        assert (await client.post("/desktop/logout", headers=cookie(session))).status_code == 403
        assert (await client.post("/desktop/logout", headers={**cookie(session), **CSRF})).status_code == 204
        assert (await client.get("/desktop", headers=cookie(session))).status_code == 401
        assert (await client.get("/inbox", headers=cookie(session))).status_code == 401
    run(gateway_server, body)


def test_floor_copy_may_be_framed_and_routes_open_chat_to_the_parent(gateway_server):
    async def body(client):
        assert (await client.get("/desktop/floor")).status_code == 401
        session = await login(client)
        r = await client.get("/desktop/floor", headers=cookie(session))
        assert r.status_code == 200
        assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
        assert "parent.postMessage(m,location.origin)" in r.text
    run(gateway_server, body)


def test_sessions_survive_a_restart_as_digests_in_a_private_file(fresh_store, monkeypatch):
    session = desktop.mint_session("192.0.2.7")
    assert stat.S_IMODE(os.stat(fresh_store).st_mode) == 0o600
    assert session not in fresh_store.read_text()  # only the sha256 is stored
    monkeypatch.setattr(desktop, "_sessions", None)  # as after a restart
    assert desktop.session_valid(session, "192.0.2.7")
    assert not desktop.session_valid(session + "x", "192.0.2.7")


def test_session_is_bound_to_the_client_ip_that_opened_the_link():
    session = desktop.mint_session("192.0.2.7")
    assert not desktop.session_valid(session, "198.51.100.8")  # cookie replayed from another node
    assert not desktop.session_valid(session, None)


def test_corrupt_store_is_an_empty_store(fresh_store, monkeypatch):
    fresh_store.write_text('{"abc": null}')
    monkeypatch.setattr(desktop, "_sessions", None)
    assert not desktop.session_valid("x", "1.2.3.4")


def test_oldest_session_is_evicted_past_the_cap():
    first = desktop.mint_session("1.2.3.4")
    for _ in range(desktop.SESSION_MAX):
        desktop.mint_session("1.2.3.4")
    assert not desktop.session_valid(first, "1.2.3.4")


@pytest.mark.parametrize("method,path,ok", [
    ("POST", "/chat", True), ("GET", "/central/sessions", True), ("POST", "/central/cards/x/answer", True),
    ("POST", "/siri/hey", True), ("POST", "/ask", True), ("POST", "/pc/wake", True),
    ("GET", "/inbox", True), ("POST", "/inbox/abc/read", True), ("GET", "/notify/notes", True),
    ("GET", "/screen", True), ("GET", "/screen/minipc/ws", False), ("POST", "/screen/launch", False),
    ("POST", "/screen/mac/input", False), ("HEAD", "/central/sessions", False),
    ("GET", "/central/sessions/a/b/events", False), ("GET", "/central/sessions/../stop", False),
    ("POST", "/inbox/notify", False), ("GET", "/hwmode/requests", False), ("GET", "/ws", False),
    ("POST", "/cards/event", False), ("POST", "/desktop/launch", False), ("POST", "/pc/sleep", False),
    ("GET", "/chat", False), ("GET", "/screens", False), ("POST", "/agents/windows/command", False),
])
def test_allowlist(method, path, ok):
    assert desktop._allowed(method, path) is ok


# Every app route the cookie can reach, as "METHOD path-template". A new route
# under an allowed pattern fails here until someone decides it belongs.
EXPECTED_REACHABLE = {
    "POST /chat", "GET /central/sessions", "POST /central/clear-done", "POST /central/attachments",
    "GET /central/agents", "GET /central/watchdog", "GET /central/feed",
    "POST /central/sessions/{session_id}/message", "POST /central/sessions/{session_id}/stop",
    "GET /central/sessions/{session_id}/events", "POST /central/cards/{session_id}/answer",
    "GET /central/cards/{session_id}/files/{file_id}", "POST /siri/hey", "POST /ask", "POST /pc/wake",
    "GET /inbox", "POST /inbox/{item_id}/read", "GET /notify/notes", "GET /screen",
}


def test_allowlist_reaches_only_the_expected_routes():
    from gateway.app import app
    def walk(routes):  # newer FastAPI nests included routers (original_router)
        for route in routes:
            inner = getattr(route, "original_router", None)
            yield from walk(inner.routes) if inner is not None else [route]

    reached = set()
    for route in walk(app.routes):
        path = getattr(route, "path", "")
        sample = re.sub(r"\{[^}]+\}", "abc", path)
        for method in getattr(route, "methods", None) or {"GET"}:
            if desktop._allowed(method, sample):
                reached.add(f"{method} {path}")
    assert len(reached) >= 15, reached  # the walk really matched the phone routes
    assert reached <= EXPECTED_REACHABLE, reached - EXPECTED_REACHABLE


def test_websocket_needs_same_origin():
    assert desktop._same_origin([(b"origin", b"https://box:8790"), (b"host", b"box:8790")])
    assert not desktop._same_origin([(b"origin", b"https://evil.example"), (b"host", b"box:8790")])
    assert not desktop._same_origin([(b"host", b"box:8790")])
