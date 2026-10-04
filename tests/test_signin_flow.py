"""S2/T5.2 check: sign-in handoff flow — prefill JS, origin binding, credential never leaks.

Same harness as tests/test_flight_flow.py: `gateway.app` in-process under
uvicorn (asyncio task, port 0) with `common.config.get_token` monkeypatched to
name-aware fixture tokens, a fake app connected over WS as role=ios (Bearer
header), and the `handoff_open` dispatch driven through
`signin.handle_handoff_open` (the importable seam — it delegates to flight's
machine, then pushes the prefill). The fixture vault lives below as a
module-level string with ONLY fake credentials, written to a tmp_path file;
the autouse fixture points `signin.VAULT_PATH` at it. Eligible sections are
`## Signin/<vault_key>` with url/username/password lines (S2 HIGH-2); the
validation tests assert the generic 400 for a non-Signin vault_key, an http://
url, and a host mismatch. The leak test captures logs (caplog) and asserts the
fixture password appears only inside the single evaluateJS prefill push —
never in another WS frame, an HTTP response body, or a log record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:  # InvalidStatusCode is deprecated in websockets >=14 but raised pre-13
        from websockets.exceptions import InvalidStatusCode
    except ImportError:  # pragma: no cover
        InvalidStatusCode = InvalidStatus

from common import config
from gateway.flows import flight, signin

TOKEN = "jarvis_test_token_123"
IOS_TOKEN = "jarvis_ios_test_token_456"
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}
IOS_AUTH_HEADER = {"Authorization": f"Bearer {IOS_TOKEN}"}
# websockets 14.0 renamed connect()'s extra_headers to additional_headers.
_WS_HEADERS_KWARG = (
    "additional_headers"
    if int(websockets.version.version.split(".")[0]) >= 14
    else "extra_headers"
)
SIGNIN_URL = "https://login.example.com/signin"
VAULT_KEY = "Example Site"
FAKE_USERNAME = "user@example.com"
FAKE_PASSWORD = "not-a-real-password"
EXPECTED_HOST = "login.example.com"

# Fixture vault: same markdown shape as the real Secrets vault (T0.1) —
# `## <Section>` headings with `key: value` lines. Fake credentials only.
# S2 HIGH-2: only `## Signin/...` sections carrying url/username/password
# lines are eligible sign-in sections.
VAULT_FIXTURE = f"""\
# Secrets

## Some Other Project
api_key: sk-not-a-real-key
url: https://login.example.com
username: {FAKE_USERNAME}
password: {FAKE_PASSWORD}

## Signin/{VAULT_KEY}
url: {SIGNIN_URL}
username: {FAKE_USERNAME}
password: {FAKE_PASSWORD}

## Jarvis Companion
token: {TOKEN}
ios_token: {IOS_TOKEN}
"""


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    vault_file = tmp_path / "Projects.md"
    vault_file.write_text(VAULT_FIXTURE, encoding="utf-8")
    monkeypatch.setattr(signin, "VAULT_PATH", vault_file)
    flight._handoffs.clear()
    flight.notifications.clear()
    flight.notify = flight._default_notify
    yield
    flight._handoffs.clear()
    flight.notifications.clear()
    flight.notify = flight._default_notify


@contextlib.asynccontextmanager
async def gateway_server():
    """Run the app in-process on port 0 in the caller's event loop; yields host:port."""
    # Name-aware monkeypatch (S2): ios -> ios_token, else the shared token.
    config.get_token = lambda path=None, name="token": IOS_TOKEN if name == "ios_token" else TOKEN
    from gateway.app import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serve_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serve_task


def test_signin_creates_pending_handoff_and_prefills():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/handoff/signin",
                headers=AUTH_HEADER,
                json={"url": SIGNIN_URL, "vault_key": VAULT_KEY},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert set(body) == {"id", "link"}, body
            # Link format: jarvis://handoff/ prefix + non-empty id, and they match.
            assert body["link"].startswith("jarvis://handoff/"), body["link"]
            handoff_id = body["link"][len("jarvis://handoff/"):]
            assert handoff_id and handoff_id == body["id"], body
            handoff = await flight.get_handoff(handoff_id)
            assert handoff is not None and handoff["state"] == "pending", handoff
            assert handoff["url"] == SIGNIN_URL and handoff["vault_key"] == VAULT_KEY

            # Fake app connects over WS as role=ios (Bearer header); the
            # handoff_open dispatch goes through the importable seam (signin
            # delegates to flight's machine, then pushes the prefill).
            async with websockets.connect(
                f"ws://{base}/ws?role=ios", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}
            ) as app_ws:
                reply_data = await signin.handle_handoff_open("m1", handoff_id)
                assert reply_data == {"url": SIGNIN_URL}, reply_data

                first = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert first == {"id": "m1", "ok": True, "data": {"url": SIGNIN_URL}}, first
                second = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert second == {
                    "id": "m1",
                    "type": "navigate",
                    "payload": {"url": SIGNIN_URL},
                }, second
                third = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert third["id"] == "m1" and third["type"] == "evaluateJS", third
                # The prefill JS contains the fixture username (criterion 2).
                assert FAKE_USERNAME in third["payload"]["js"], third
                # S2 HIGH-3: the payload carries the origin-bound expected_host,
                # and the JS's FIRST statement is the location.host guard.
                assert third["payload"]["expected_host"] == EXPECTED_HOST, third
                js = third["payload"]["js"]
                guard = f"if (location.host !== {json.dumps(EXPECTED_HOST)}) {{ return; }}"
                assert guard in js, js
                body_start = js.index("(function() {") + len("(function() {")
                assert js[body_start:].startswith(f"if (location.host !=="), js

            handoff = await flight.get_handoff(handoff_id)
            assert handoff["state"] == "driving", handoff
            assert handoff["history"] == ["pending", "accepted", "driving"], handoff["history"]

    asyncio.run(run())


def test_invalid_requests_get_generic_400():
    """S2 HIGH-2: every violation is a 400 with the SAME generic detail.

    Covers: a vault_key whose section exists but is not under `## Signin/`;
    an http:// url; a handoff url whose host is neither the section host nor a
    subdomain of it; a nonexistent vault_key. None may reveal section
    existence.
    """
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            cases = [
                # (label, url, vault_key)
                ("non-signin section", SIGNIN_URL, "Some Other Project"),
                ("plaintext http url", "http://login.example.com/signin", VAULT_KEY),
                ("host mismatch", "https://evil.example.net/signin", VAULT_KEY),
                ("unknown vault key", SIGNIN_URL, "No Such Site"),
            ]
            for label, url, vault_key in cases:
                resp = await http.post(
                    "/handoff/signin",
                    headers=AUTH_HEADER,
                    json={"url": url, "vault_key": vault_key},
                )
                assert resp.status_code == 400, (label, resp.status_code, resp.text)
                assert resp.json() == {"detail": signin.GENERIC_400_DETAIL}, (label, resp.text)

            # A subdomain of the section host is still allowed.
            resp = await http.post(
                "/handoff/signin",
                headers=AUTH_HEADER,
                json={"url": "https://sso.login.example.com/signin", "vault_key": VAULT_KEY},
            )
            assert resp.status_code == 200, resp.text

    asyncio.run(run())


def test_password_appears_only_in_the_evaluateJS_push(caplog):
    async def run():
        with caplog.at_level(logging.DEBUG):
            async with gateway_server() as base, httpx.AsyncClient(
                base_url=f"http://{base}"
            ) as http:
                resp = await http.post(
                    "/handoff/signin",
                    headers=AUTH_HEADER,
                    json={"url": SIGNIN_URL, "vault_key": VAULT_KEY},
                )
                assert resp.status_code == 200, resp.text
                handoff_id = resp.json()["id"]

                frames = []
                async with websockets.connect(
                    f"ws://{base}/ws?role=ios", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}
                ) as app_ws:
                    await signin.handle_handoff_open("m1", handoff_id)
                    for _ in range(3):
                        frames.append(await asyncio.wait_for(app_ws.recv(), timeout=10))

        parsed = [json.loads(f) for f in frames]
        js_pushes = [m for m in parsed if m.get("type") == "evaluateJS"]
        # Exactly one frame carries the password: the single prefill push.
        assert len(js_pushes) == 1, parsed
        assert FAKE_USERNAME in js_pushes[0]["payload"]["js"], js_pushes
        for m in parsed:
            if m is not js_pushes[0]:
                assert FAKE_PASSWORD not in json.dumps(m), m
        assert FAKE_PASSWORD not in resp.text, resp.text
        # And nowhere in captured logs (criterion 3).
        assert FAKE_PASSWORD not in caplog.text, caplog.text

    asyncio.run(run())


def test_second_handoff_open_pushes_prefill_only_once():
    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/handoff/signin",
                headers=AUTH_HEADER,
                json={"url": SIGNIN_URL, "vault_key": VAULT_KEY},
            )
            handoff_id = resp.json()["id"]

            async with websockets.connect(
                f"ws://{base}/ws?role=ios", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}
            ) as app_ws:
                assert await signin.handle_handoff_open("m1", handoff_id) == {"url": SIGNIN_URL}
                for _ in range(3):  # reply + navigate + prefill
                    await asyncio.wait_for(app_ws.recv(), timeout=10)
                # MED-1 single-use: the second open is refused by flight's
                # machine and signin adds NO second prefill push.
                assert await signin.handle_handoff_open("m2", handoff_id) is None
                reply = json.loads(await asyncio.wait_for(app_ws.recv(), timeout=10))
                assert reply == {"id": "m2", "ok": False, "data": "handoff already used"}, reply
                try:  # and nothing else arrives
                    extra = await asyncio.wait_for(app_ws.recv(), timeout=1)
                    raise AssertionError(f"unexpected extra frame: {extra}")
                except asyncio.TimeoutError:
                    pass

    asyncio.run(run())


def test_ttl_expiry_uses_flight_notify_seam():
    async def run():
        recorded: list[dict] = []

        async def fake_notify(event: dict) -> None:
            recorded.append(event)

        flight.notify = fake_notify
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/handoff/signin",
                headers=AUTH_HEADER,
                json={"url": SIGNIN_URL, "vault_key": VAULT_KEY},
            )
            handoff_id = resp.json()["id"]
            handoff = await flight.get_handoff(handoff_id)
            handoff["expires_at"] = 0.0  # test seam: force past TTL

            first = await flight.get_handoff(handoff_id)
            assert first["state"] == "expired", first
            second = await flight.get_handoff(handoff_id)  # re-read must not re-notify
            assert second["state"] == "expired", second

        assert recorded == [{"id": handoff_id, "event": "expired"}], recorded

    asyncio.run(run())
