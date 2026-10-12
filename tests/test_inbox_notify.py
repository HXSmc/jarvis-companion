"""J3 check: POST /inbox/notify's guaranteed Mac-side path (jarvis-phone queue).

Same `gateway_server` fixture as test_inbox.py; the jarvis-phone repo is
pointed at a tmp dir via JARVIS_PHONE_DIR (real owner.json / queue never
touched, nothing ever sent) and `registry.send_to_role` is monkeypatched so
no live socket is needed. Covers: new>0 drops exactly one WhatsApp outbound
file (jarvis://inbox in text, to = owner.json phone), the OVERNIGHT marker
switches the channel to email, urgent ALSO raises a call_eligible event
(non-urgent never does), and an enqueue IO error still answers 200.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from conftest import IOS_AUTH_HEADER

OWNER_PHONE = "+15550001111"
OWNER_EMAIL = "owner@example.com"

SEED = [
    {
        "id": "m3", "account": "a@example.com", "vendor": "Vercel",
        "sender": "hi@vercel.com", "subject": "Deployment failed",
        "received_at": "2026-10-12T09:30:00Z", "summary": "Prod deploy failed",
        "next_steps": ["Check build log"], "read": False,
        "created_at": "2026-10-12T09:30:02Z",
    },
    {
        "id": "m2", "account": "b@example.com", "vendor": "GitHub",
        "sender": "noreply@github.com", "subject": "PR merged",
        "received_at": "2026-10-11T14:00:00Z", "summary": "PR #42 merged",
        "next_steps": [], "read": False, "created_at": "2026-10-11T14:00:01Z",
    },
]


@pytest.fixture
def store_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("JARVIS_VENDOR_WATCH_HOME", str(tmp_path))
    (tmp_path / "inbox.json").write_text(json.dumps({"items": SEED}), encoding="utf-8")
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


@pytest.fixture(autouse=True)
def no_ios_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep notify's WS push deterministic: no socket, delivered always False."""
    async def fake_send(role: str, payload: dict) -> bool:
        return False

    monkeypatch.setattr("gateway.inbox.registry.send_to_role", fake_send)


def _outbound(phone_home: Path) -> list[dict]:
    files = sorted((phone_home / "queue" / "outbound").glob("*.json"))
    return [json.loads(f.read_text()) for f in files]


def _events(phone_home: Path) -> list[dict]:
    directory = phone_home / "queue" / "events"
    if not directory.exists():
        return []
    return [json.loads(f.read_text()) for f in sorted(directory.glob("*.json"))]


def _assert_drainer_parseable(msg: dict) -> None:
    """Drainer's _is_stale comparison must not TypeError: naive now() minus
    fromisoformat(created) only works when 'created' has no tz suffix (a
    trailing Z parses aware on py>=3.11)."""
    created = datetime.fromisoformat(msg["created"])
    assert created.tzinfo is None, msg["created"]
    _ = (datetime.now() - created).total_seconds()


async def run_new_whatsapp(server_factory, store_home: Path, phone_home: Path) -> None:
    async with server_factory() as base:
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post("/inbox/notify", json={"new": 2}, headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"ok": True, "delivered": False}, resp.text
    files = _outbound(phone_home)
    assert len(files) == 1, files
    msg = files[0]
    assert msg["channel"] == "whatsapp", msg
    assert msg["payload"]["to"] == OWNER_PHONE, msg
    assert "jarvis://inbox" in msg["payload"]["text"], msg
    assert "Vercel" in msg["payload"]["text"] and "GitHub" in msg["payload"]["text"], msg
    _assert_drainer_parseable(msg)
    shutil.rmtree(phone_home / "queue" / "outbound")
    print("[1] new>0 -> one whatsapp outbound with jarvis://inbox + owner phone")


async def run_overnight_email(server_factory, store_home: Path, phone_home: Path) -> None:
    (phone_home / "queue").mkdir()
    (phone_home / "queue" / "OVERNIGHT").write_text("", encoding="utf-8")
    async with server_factory() as base:
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post("/inbox/notify", json={"new": 1}, headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200, resp.text
    files = _outbound(phone_home)
    assert len(files) == 1, files
    msg = files[0]
    assert msg["channel"] == "email", msg
    assert msg["payload"]["to"] == OWNER_EMAIL, msg
    assert msg["payload"]["subject"] == "[jarvis] vendor inbox: 1 new", msg
    assert "jarvis://inbox" in msg["payload"]["body"], msg
    print("[2] OVERNIGHT marker -> email channel, no whatsapp file")


async def run_urgent_event(server_factory, store_home: Path, phone_home: Path) -> None:
    async with server_factory() as base:
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/inbox/notify",
                json={"new": 1, "urgent": True, "subject": "Vendor deadline today"},
                headers=IOS_AUTH_HEADER,
            )
            assert resp.status_code == 200, resp.text
    events = _events(phone_home)
    assert len(events) == 1, events
    event = events[0]
    assert event["call_eligible"] is True, event
    assert event["type"] == "decision-request" and event["kind"] == "needs_input", event
    assert event["summary"] == "Vendor deadline today", event
    _assert_drainer_parseable(event)
    # Non-urgent (flag absent) is message-only: no events file ever.
    shutil.rmtree(phone_home / "queue" / "events")
    async with server_factory() as base:
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post("/inbox/notify", json={"new": 1}, headers=IOS_AUTH_HEADER)
            assert resp.status_code == 200, resp.text
    assert _events(phone_home) == [], "non-urgent notify must not raise an event"
    print("[3] urgent -> events file with call_eligible:true; non-urgent -> none")


async def run_enqueue_io_error(server_factory, store_home: Path, phone_home: Path) -> None:
    # queue/outbound as a regular FILE: mkdir(parents, exist_ok) raises
    # FileExistsError (exist_ok only tolerates dirs) -> deterministic IO error.
    phone_home.mkdir(parents=True, exist_ok=True)
    (phone_home / "queue").mkdir()
    (phone_home / "queue" / "outbound").write_text("not a dir", encoding="utf-8")
    async with server_factory() as base:
        async with httpx.AsyncClient(base_url=f"http://{base}") as http:
            resp = await http.post(
                "/inbox/notify", json={"new": 2, "urgent": True}, headers=IOS_AUTH_HEADER
            )
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"ok": True, "delivered": False}, resp.text
    # The blocked path stays untouched (still a regular file, nothing
    # written into it); queue/events is a different subtree and may succeed.
    assert (phone_home / "queue" / "outbound").is_file()
    print("[4] enqueue IO error -> still 200 {ok: true, delivered: false}")


def test_new_mail_whatsapp_outbound(gateway_server, store_home: Path, phone_home: Path) -> None:
    asyncio.run(run_new_whatsapp(gateway_server, store_home, phone_home))


def test_overnight_switches_to_email(gateway_server, store_home: Path, phone_home: Path) -> None:
    asyncio.run(run_overnight_email(gateway_server, store_home, phone_home))


def test_urgent_event_non_urgent_none(gateway_server, store_home: Path, phone_home: Path) -> None:
    asyncio.run(run_urgent_event(gateway_server, store_home, phone_home))


def test_enqueue_failure_still_200(gateway_server, store_home: Path, phone_home: Path) -> None:
    asyncio.run(run_enqueue_io_error(gateway_server, store_home, phone_home))
