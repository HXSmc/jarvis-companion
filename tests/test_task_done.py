"""POST /inbox/task-done: exactly ONE owner message per finished task.

Router ledger pokes this when a top-level box task reaches DONE. Covers: one
WhatsApp outbound per session_id (purpose in the text), duplicate POSTs are
silently absorbed (notified:false, no new file), different sessions each get
their own message, box mode forwards to the Mac exactly once and surfaces
mac-offline as a retryable 503 (id NOT recorded), missing/empty fields 422
with nothing written, and the OVERNIGHT marker switches to email.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common import config
from conftest import AUTH_HEADER, IOS_AUTH_HEADER
from gateway import inbox

OWNER_PHONE = "+15550001111"
OWNER_EMAIL = "owner@example.com"


@pytest.fixture
def store_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """tmp vendor-watch home — hosts the task-done-seen.json dedupe file."""
    monkeypatch.setenv("JARVIS_VENDOR_WATCH_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def phone_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """tmp jarvis-phone root with an owner.json; JARVIS_PHONE_DIR points at it."""
    monkeypatch.setenv("JARVIS_PHONE_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "owner.json").write_text(
        json.dumps({"phone_whatsapp": OWNER_PHONE, "email": OWNER_EMAIL}), encoding="utf-8"
    )
    return tmp_path


def _outbound(phone_home: Path) -> list[dict]:
    directory = phone_home / "queue" / "outbound"
    if not directory.exists():
        return []
    return [json.loads(f.read_text()) for f in sorted(directory.glob("*.json"))]


def _seen(store_home: Path) -> dict:
    path = store_home / "task-done-seen.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


async def _mac_post(path: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://gw") as client:
        return await client.post(path, **kw)


def _app():
    from gateway.app import app

    return app


def _fake_mac(seen: list[dict]) -> FastAPI:
    """Fake Mac gateway recording the task-done POSTs it receives."""
    mac = FastAPI()

    @mac.post("/inbox/task-done")
    async def task_done_(request: Request):
        seen.append(await request.json())
        return JSONResponse({"ok": True, "notified": True})

    return mac


def _box(monkeypatch: pytest.MonkeyPatch, seen: list[dict]) -> None:
    """Box mode + a fake Mac gateway (tests swap in a refuse transport for 'down')."""
    monkeypatch.setenv(inbox.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(
        inbox, "_mac_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake_mac(seen)), timeout=inbox.MAC_PROXY_TIMEOUT),
    )


def _box_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = {"token": "jarvis_test_token_123", "ios_token": "jarvis_ios_test_token_456"}
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": tokens.get(name))


def test_valid_post_one_whatsapp_message(gateway_server, store_home: Path, phone_home: Path) -> None:
    async def run() -> None:
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                resp = await http.post(
                    "/inbox/task-done",
                    json={"session_id": "s1", "purpose": "mini PC setup"},
                    headers=IOS_AUTH_HEADER,
                )
                assert resp.status_code == 200, resp.text
                assert resp.json() == {"ok": True, "notified": True}, resp.text

    asyncio.run(run())
    files = _outbound(phone_home)
    assert len(files) == 1, files
    msg = files[0]
    assert msg["channel"] == "whatsapp", msg
    assert msg["payload"]["to"] == OWNER_PHONE, msg
    assert msg["payload"]["text"] == "✅ mini PC setup is done", msg
    assert "s1" in _seen(store_home)


def test_duplicate_session_id_absorbed(gateway_server, store_home: Path, phone_home: Path) -> None:
    async def run() -> None:
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                for _ in range(2):
                    resp = await http.post(
                        "/inbox/task-done",
                        json={"session_id": "s1", "purpose": "task A"},
                        headers=IOS_AUTH_HEADER,
                    )
                    assert resp.status_code == 200, resp.text
                assert resp.json() == {"ok": True, "notified": False}, resp.text

    asyncio.run(run())
    assert len(_outbound(phone_home)) == 1
    assert list(_seen(store_home)) == ["s1"]


def test_different_sessions_each_notify_once(gateway_server, store_home: Path, phone_home: Path) -> None:
    async def run() -> None:
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                for sid, purpose in (("s1", "task A"), ("s2", "task B"), ("s1", "task A")):
                    resp = await http.post(
                        "/inbox/task-done",
                        json={"session_id": sid, "purpose": purpose},
                        headers=AUTH_HEADER,  # shared token is accepted too
                    )
                    assert resp.status_code == 200, resp.text
                    # queue filenames are <ts>-<pid>.json: two real messages in
                    # the same second would collide, so space them across one.
                    await asyncio.sleep(1.1)

    asyncio.run(run())
    files = _outbound(phone_home)
    assert len(files) == 2, files
    texts = {f["payload"]["text"] for f in files}
    assert texts == {"✅ task A is done", "✅ task B is done"}, texts
    assert set(_seen(store_home)) == {"s1", "s2"}


def test_box_mode_forwards_exactly_once(monkeypatch: pytest.MonkeyPatch, store_home: Path) -> None:
    _box_stub(monkeypatch)
    seen: list[dict] = []
    _box(monkeypatch, seen)

    resp = asyncio.run(
        _mac_post("/inbox/task-done", json={"session_id": "s1", "purpose": "task A"}, headers=IOS_AUTH_HEADER)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "notified": True}, resp.text
    resp = asyncio.run(
        _mac_post("/inbox/task-done", json={"session_id": "s1", "purpose": "task A"}, headers=IOS_AUTH_HEADER)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "notified": False}, resp.text

    assert len(seen) == 1, seen  # second call: 0 forwards (local dedupe)
    assert seen[0] == {"session_id": "s1", "purpose": "task A"}
    assert list(_seen(store_home)) == ["s1"]


def test_box_mode_mac_down_503_then_retry(monkeypatch: pytest.MonkeyPatch, store_home: Path) -> None:
    _box_stub(monkeypatch)
    seen: list[dict] = []
    _box(monkeypatch, seen)

    def refuse(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        inbox, "_mac_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse), timeout=inbox.MAC_PROXY_TIMEOUT),
    )
    resp = asyncio.run(
        _mac_post("/inbox/task-done", json={"session_id": "s1", "purpose": "task A"}, headers=IOS_AUTH_HEADER)
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "mac offline", resp.text
    assert _seen(store_home) == {}, "failed attempt must not be recorded"

    # Mac back up: the retry goes through and forwards exactly once total.
    monkeypatch.setattr(
        inbox, "_mac_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=_fake_mac(seen)), timeout=inbox.MAC_PROXY_TIMEOUT),
    )
    resp = asyncio.run(
        _mac_post("/inbox/task-done", json={"session_id": "s1", "purpose": "task A"}, headers=IOS_AUTH_HEADER)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "notified": True}, resp.text
    assert len(seen) == 1, seen
    assert list(_seen(store_home)) == ["s1"]


def test_missing_or_empty_fields_422(gateway_server, store_home: Path, phone_home: Path) -> None:
    async def run() -> None:
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                for bad in (
                    {},
                    {"session_id": "s1"},
                    {"purpose": "task A"},
                    {"session_id": "", "purpose": "task A"},
                    {"session_id": "s1", "purpose": ""},
                ):
                    resp = await http.post("/inbox/task-done", json=bad, headers=IOS_AUTH_HEADER)
                    assert resp.status_code == 422, (bad, resp.text)

    asyncio.run(run())
    assert _outbound(phone_home) == []
    assert _seen(store_home) == {}


def test_overnight_marker_switches_to_email(gateway_server, store_home: Path, phone_home: Path) -> None:
    (phone_home / "queue").mkdir()
    (phone_home / "queue" / "OVERNIGHT").write_text("", encoding="utf-8")

    async def run() -> None:
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                resp = await http.post(
                    "/inbox/task-done",
                    json={"session_id": "s1", "purpose": "task A"},
                    headers=IOS_AUTH_HEADER,
                )
                assert resp.status_code == 200, resp.text
                assert resp.json() == {"ok": True, "notified": True}, resp.text

    asyncio.run(run())
    files = _outbound(phone_home)
    assert len(files) == 1, files
    msg = files[0]
    assert msg["channel"] == "email", msg
    assert msg["payload"]["to"] == OWNER_EMAIL, msg
    assert msg["payload"]["subject"] == "[jarvis] task done: task A", msg
    assert msg["payload"]["body"] == "✅ task A is done", msg
