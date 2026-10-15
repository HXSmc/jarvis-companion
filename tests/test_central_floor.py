"""Agency Floor: the public /central/floor page + the ios_token /central/agents trim.

agentic-os is a FastAPI fake mounted through httpx.ASGITransport (no network);
its bearer is stubbed so ~/.config/agentic-os/token is never read, and
AGENTIC_SECRET must never surface in a response or a log line.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common import config
from gateway import central

TOKEN = "jarvis_test_token_123"
IOS_TOKEN = "jarvis_ios_test_token_456"
AGENTIC_SECRET = "agentic_secret_value_789"
IOS = {"Authorization": f"Bearer {IOS_TOKEN}"}

SNAPSHOT = {
    "usage": {"secret_ish": "dropped"},
    "sessions": {
        "device": "mac-studio",
        "peerErrors": ["peer-x"],
        "hubs": [
            {
                "key": "mac-studio:abc", "sessionId": "abc", "device": "mac-studio",
                "dirLabel": "jarvis", "engine": "claude", "active": True, "lastTs": 1760000000000,
                "agents": [
                    {"id": "a1", "persona": "builder", "engine": "glm", "label": "spoke-1",
                     "source": "spoke", "active": True, "status": "running", "lastTs": 1, "model": "x"},
                    "not-a-dict",
                ],
            },
            "junk",
        ],
    },
}

fake = FastAPI()
fake_state: dict = {"status": 200, "body": SNAPSHOT, "seen": []}


@fake.get("/api/snapshot")
async def snapshot(request: Request):
    fake_state["seen"].append(dict(request.headers))
    return JSONResponse(fake_state["body"], status_code=fake_state["status"])


@pytest.fixture(autouse=True)
def stub(monkeypatch: pytest.MonkeyPatch):
    tokens = {"token": TOKEN, "ios_token": IOS_TOKEN}
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": tokens.get(name))
    monkeypatch.setattr(central, "agentic_os_token", lambda: AGENTIC_SECRET)
    monkeypatch.setattr(central, "agentic_os_base", lambda: "http://agentic.test")
    monkeypatch.setattr(
        central, "_agentic_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.ASGITransport(app=fake), timeout=timeout),
    )
    fake_state.update(status=200, body=SNAPSHOT, seen=[])


def _get(path: str, headers: dict | None = None) -> httpx.Response:
    from gateway.app import app

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
            return await client.get(path, headers=headers)

    return asyncio.run(run())


# --- /central/floor --------------------------------------------------------------

def test_floor_served_without_auth():
    resp = _get("/central/floor")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "AGENCY FLOOR" in resp.text
    assert "no-store" in resp.headers["cache-control"]
    assert "default-src 'none'" in resp.headers["content-security-policy"]


def test_floor_body_has_no_token():
    body = _get("/central/floor", headers=IOS).text
    for secret in (TOKEN, IOS_TOKEN, AGENTIC_SECRET):
        assert secret not in body
    # Bearer comes from the app-injected global, never a URL or storage.
    assert "localStorage" not in body and "?token=" not in body


# --- /central/agents -------------------------------------------------------------

@pytest.mark.parametrize("headers", [None, {"Authorization": f"Bearer {TOKEN}"}, {"Authorization": "Bearer nope"}])
def test_agents_needs_ios_token(headers):
    assert _get("/central/agents", headers=headers).status_code == 401
    assert fake_state["seen"] == []


def test_agents_trimmed():
    resp = _get("/central/agents", headers=IOS)
    assert resp.status_code == 200
    assert resp.json() == {
        "device": "mac-studio",
        "hubs": [{
            "key": "mac-studio:abc", "dirLabel": "jarvis", "engine": "claude", "active": True,
            "lastTs": 1760000000000,
            "agents": [{"id": "a1", "persona": "builder", "engine": "glm", "label": "spoke-1",
                        "active": True, "status": "running"}],
        }],
    }
    assert fake_state["seen"][0]["authorization"] == f"Bearer {AGENTIC_SECRET}"


@pytest.mark.parametrize("status,body", [(500, {"err": "boom"}), (401, {}), (200, {"sessions": "bad"}), (200, [1, 2])])
def test_agents_bad_upstream_is_empty(status, body):
    fake_state.update(status=status, body=body)
    assert _get("/central/agents", headers=IOS).json() == {"device": None, "hubs": []}


def test_agents_down_is_empty(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        central, "_agentic_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(refuse), timeout=timeout),
    )
    assert _get("/central/agents", headers=IOS).json() == {"device": None, "hubs": []}


def test_agents_no_token_file_is_empty_without_call(monkeypatch):
    monkeypatch.setattr(central, "agentic_os_token", lambda: None)
    assert _get("/central/agents", headers=IOS).json() == {"device": None, "hubs": []}
    assert fake_state["seen"] == []


def test_agentic_os_token_missing_file(monkeypatch, tmp_path):
    monkeypatch.undo()  # real reader, pointed at a path that does not exist
    monkeypatch.setattr(central, "AGENTIC_OS_TOKEN_PATH", tmp_path / "nope")
    assert central.agentic_os_token() is None


def test_agentic_secret_never_in_response_or_logs(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    ok = _get("/central/agents", headers=IOS)

    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        central, "_agentic_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(refuse), timeout=timeout),
    )
    down = _get("/central/agents", headers=IOS)
    assert AGENTIC_SECRET not in ok.text + down.text
    assert caplog.records, "expected some log output to check"
    assert AGENTIC_SECRET not in caplog.text
