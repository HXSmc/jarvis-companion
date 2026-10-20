"""J2 check: /inbox endpoints (serve store, mark-read, loopback notify).

Runs `gateway.app` via the shared `gateway_server` fixture (in-process uvicorn
on 127.0.0.1, fixture tokens through the name-aware get_token patch); the
store is pointed at a tmp dir via JARVIS_VENDOR_WATCH_HOME so the real
~/.jarvis/vendor-watch is never touched. Covers: GET returns seeded items
newest-first + unread count, POST read flips the flag persistently and
recounts, unknown id 404s, notify with no ios socket replies 200
delivered:false, the loopback guard rejects non-127.0.0.1 callers, and notify
with a live ios socket pushes the {id, type: "inbox_new", payload} envelope
WebBridge dispatches. The vault is never read.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
import websockets

from conftest import AUTH_HEADER, IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG

# Seeded deliberately out of chronological order: newest (m3) in the middle.
SEED = [
    {
        "id": "m1", "account": "a@example.com", "vendor": "Stripe", "sender": "no-reply@stripe.com",
        "subject": "Your invoice is ready", "received_at": "2026-10-10T08:00:00Z",
        "summary": "October invoice", "next_steps": ["Download invoice"],
        "read": True, "created_at": "2026-10-10T08:00:05Z",
    },
    {
        "id": "m3", "account": "a@example.com", "vendor": "Vercel", "sender": "hi@vercel.com",
        "subject": "Deployment failed", "received_at": "2026-10-12T09:30:00Z",
        "summary": "Prod deploy failed", "next_steps": ["Check build log", "Rollback"],
        "read": False, "created_at": "2026-10-12T09:30:02Z",
    },
    {
        "id": "m2", "account": "b@example.com", "vendor": "GitHub", "sender": "noreply@github.com",
        "subject": "PR merged", "received_at": "2026-10-11T14:00:00Z",
        "summary": "PR #42 merged", "next_steps": [],
        "read": False, "created_at": "2026-10-11T14:00:01Z",
    },
]


def _seed(tmp_path: Path, items: list[dict]) -> None:
    (tmp_path / "inbox.json").write_text(json.dumps({"items": items}), encoding="utf-8")


async def run_get(server_factory, tmp_path: Path) -> None:
    _seed(tmp_path, SEED)
    async with server_factory() as base:
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            no_auth = await http.get("/inbox")
            assert no_auth.status_code == 401, "GET /inbox must require the ios token"
            shared = await http.get("/inbox", headers=AUTH_HEADER)
            assert shared.status_code == 401, "shared token must not read the inbox"

            resp = await http.get("/inbox", headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            # Newest first by received_at (m3 > m2 > m1), unread = the two read:false items.
            assert [i["id"] for i in body["items"]] == ["m3", "m2", "m1"], body
            assert body["unread"] == 2, body
            print(f"[1] GET /inbox -> newest-first ids + unread=2: {body['unread']}")


async def run_read(server_factory, tmp_path: Path) -> None:
    _seed(tmp_path, SEED)
    async with server_factory() as base:
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post("/inbox/m3/read", headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body == {"ok": True, "unread": 1}, body

            # Persistent: store on disk carries read:true, and a fresh GET agrees.
            on_disk = json.loads((tmp_path / "inbox.json").read_text())
            assert all(i["read"] for i in on_disk["items"] if i["id"] == "m3")
            got = (await http.get("/inbox", headers=IOS_AUTH_HEADER)).json()
            assert got["unread"] == 1 and got["items"][0]["read"] is True, got
            print(f"[2] POST /inbox/m3/read -> {body}; store + GET agree unread=1")

            missing = await http.post("/inbox/nope/read", headers=IOS_AUTH_HEADER)
            assert missing.status_code == 404, missing.text
            print("[3] POST /inbox/nope/read -> 404")


async def run_missing_store(server_factory, tmp_path: Path) -> None:
    async with server_factory() as base:
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.get("/inbox", headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200
            assert resp.json() == {"items": [], "unread": 0}, resp.text
            print("[4] missing store file -> empty inbox, no crash")


async def run_notify_no_socket(server_factory, tmp_path: Path) -> None:
    _seed(tmp_path, SEED)
    async with server_factory() as base:
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            for label, headers in (("ios token", IOS_AUTH_HEADER), ("shared token", AUTH_HEADER)):
                resp = await http.post(
                    "/inbox/notify", json={"new": 1, "unread": 99}, headers=headers
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body == {"ok": True, "delivered": False}, body
                print(f"[5] notify with {label}, no ios socket -> {body}")


async def run_notify_with_socket(server_factory, tmp_path: Path) -> None:
    _seed(tmp_path, SEED)
    async with server_factory() as base:
        async with websockets.connect(
            f"ws://{base}/ws?role=ios", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}
        ) as ws:
            async with httpx.AsyncClient(base_url=f"http://{base}") as http:
                resp = await http.post(
                    "/inbox/notify", json={"new": 2, "unread": 99}, headers=AUTH_HEADER
                )
                assert resp.status_code == 200, resp.text
                assert resp.json() == {"ok": True, "delivered": True}, resp.text

                # ios socket receives the pushed-command envelope WebBridge dispatches;
                # unread is recounted from the store (2), not the body's 99.
                pushed = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                assert pushed["type"] == "inbox_new", pushed
                assert pushed["id"].startswith("inbox-"), pushed
                assert pushed["payload"] == {"unread": 2, "new": 2}, pushed
                print(f"[6] notify with live ios socket -> delivered:true, pushed {pushed}")


@pytest.fixture
def store_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("JARVIS_VENDOR_WATCH_HOME", str(tmp_path))
    return tmp_path


def test_get_inbox_newest_first(gateway_server, store_home: Path) -> None:
    asyncio.run(run_get(gateway_server, store_home))


def test_mark_read_persists_and_recounts(gateway_server, store_home: Path) -> None:
    asyncio.run(run_read(gateway_server, store_home))


def test_missing_store_reads_empty(gateway_server, store_home: Path) -> None:
    asyncio.run(run_missing_store(gateway_server, store_home))


def test_notify_without_socket(gateway_server, store_home: Path) -> None:
    asyncio.run(run_notify_no_socket(gateway_server, store_home))


def test_notify_with_ios_socket(gateway_server, store_home: Path) -> None:
    asyncio.run(run_notify_with_socket(gateway_server, store_home))


def test_notify_rejects_non_loopback() -> None:
    """/inbox/notify is localhost-only: spoof a non-loopback client scope."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from gateway.inbox import _loopback_guard

    for host in ("10.0.0.4", "::1", "10.0.0.5"):
        request = Request({"type": "http", "client": (host, 51515), "method": "POST"})
        with pytest.raises(HTTPException) as exc:
            _loopback_guard(request)
        assert exc.value.status_code == 403, host
        print(f"[7] loopback guard: {host} -> 403")
