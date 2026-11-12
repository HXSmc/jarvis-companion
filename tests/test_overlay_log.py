"""GET /overlay-log + jarvis-overlay-log: ios_token auth, allow-list redaction,
split-deploy forwarding, and a CLI that never prints the token.

The overlay service and the Mac gateway are fakes (MockTransport / ASGI);
no real network, and the overlay token file is pointed at tmp_path so tests
never write to the real $HOME.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from common import config
from conftest import AUTH_HEADER, IOS_AUTH_HEADER, IOS_TOKEN
from gateway import overlay, overlay_log, screen

_CLI_PATH = Path(__file__).resolve().parent.parent / "scripts" / "jarvis-overlay-log"
_cli_spec = importlib.util.spec_from_file_location(
    "jarvis_overlay_log_cli", str(_CLI_PATH),
    loader=SourceFileLoader("jarvis_overlay_log_cli", str(_CLI_PATH)))
cli = importlib.util.module_from_spec(_cli_spec)
sys.modules.setdefault("jarvis_overlay_log_cli", cli)
_cli_spec.loader.exec_module(cli)

NOW = time.time()
SECRET_OUTPUT = "SECRET-BODY"
SECRET_PIXELS = "SCREEN-PIXELS"


def _turn(turn_id: str, steps: list[dict]) -> dict:
    return {"id": turn_id, "steps": steps}


def _snapshot() -> dict:
    """An unredacted debug-turns payload: 80 turns, the first full of things to drop."""
    filler = [_turn(f"t{i}", [{"step": "wake", "at": NOW - i, "score": 0.9}]) for i in range(1, 80)]
    return {
        "listener": {"state": "awake", "mic": "ok", "level": 0.5},
        "turns": [
            _turn("t0", [
                {"step": "agent_tool", "at": NOW, "tool": "Bash", "decision": "allow",
                 "output": SECRET_OUTPUT, "screen": {"pixels": SECRET_PIXELS}, "ms": 12, "kind": "shell"},
                {"step": "transcribed", "at": NOW + 0.1, "text": "x" * 1000},
                {"step": "voice", "at": NOW + 0.2, "audio_ms": 900, "stt_final_ms": 40,
                 "stt": "hello " * 100, "followup": True, "level": 0.4, "conf": 0.9},
                {"step": "wake", "at": NOW - 0.5, "score": 0.92, "source": "chime"},
                {"step": "hunk", "at": NOW + 0.3, "detail": "want=10-20 try=1"},
                {"step": "repo_pick", "at": NOW + 0.4, "detail": "/srv/code/secret-repo"},
                {"step": "foo", "at": NOW + 0.5, "detail": "bar"},
            ]),
            *filler,
        ],
    }


@pytest.fixture
def overlay_service(monkeypatch, tmp_path):
    """The Mac's local overlay service (loopback fake) + a tmp overlay token file."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/overlay/debug/turns":
            return httpx.Response(200, json=_snapshot())
        if request.url.path == "/overlay/health":
            return httpx.Response(200, json={"ok": True, "commit": "deadbeef"})
        return httpx.Response(404)

    monkeypatch.setattr(overlay_log, "_overlay_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(overlay, "TOKEN_FILE", tmp_path / "overlay-token")
    return seen


def _local_mode(monkeypatch) -> None:
    monkeypatch.delenv(screen.MAC_GATEWAY_ENV, raising=False)


def test_auth(gateway_server, overlay_service, monkeypatch):
    _local_mode(monkeypatch)

    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}") as c:
                assert (await c.get("/overlay-log")).status_code == 401
                assert (await c.get("/overlay-log", headers=AUTH_HEADER)).status_code == 403
                assert (await c.get("/overlay-log",
                                    headers={"Authorization": "Bearer wrong"})).status_code == 401
                ok = await c.get("/overlay-log", headers=IOS_AUTH_HEADER)
                assert ok.status_code == 200, ok.text
                assert ok.json()["commit"] == "deadbeef"

    asyncio.run(run())
    assert {r.url.path for r in overlay_service} == {"/overlay/debug/turns", "/overlay/health"}
    assert {r.headers["authorization"].startswith("Bearer ") for r in overlay_service} == {True}


def test_redaction(gateway_server, overlay_service, monkeypatch):
    _local_mode(monkeypatch)

    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                resp = await c.get("/overlay-log")
                assert resp.status_code == 200, resp.text
                return resp.json()

    body = asyncio.run(run())
    text = json.dumps(body)
    assert SECRET_OUTPUT not in text
    assert SECRET_PIXELS not in text
    assert '"screen"' not in text
    assert len(body["turns"]) == 50
    by_step: dict[str, list[dict]] = {}
    for row in body["turns"][0]["steps"]:
        by_step.setdefault(row["step"], []).append(row)
    assert set(by_step["agent_tool"][0]) == {"step", "at", "tool", "decision"}
    assert by_step["transcribed"][0]["text"] == "x" * 200
    assert set(by_step["foo"][0]) == {"step", "at"}
    assert set(by_step["repo_pick"][0]) == {"step", "at"}
    assert by_step["hunk"][0]["detail"] == "want=10-20 try=1"
    assert set(by_step["wake"][0]) == {"step", "at", "score"}
    voice = by_step["voice"][0]
    assert len(voice["stt"]) == 200
    assert voice["audio_ms"] == 900 and voice["followup"] is True
    for row in body["turns"][0]["steps"]:
        for value in row.values():
            if isinstance(value, str):
                assert len(value) <= 200


def test_redact_since_filter():
    payload = {"turns": [
        _turn("new", [{"step": "wake", "at": NOW - 5, "score": 0.9}]),
        _turn("old", [{"step": "wake", "at": NOW - 3600, "score": 0.9}]),
        _turn("nostart", []),
    ]}
    body = overlay_log.redact(payload, since=600)
    assert [t["id"] for t in body["turns"]] == ["new"]


def _fake_mac(seen: list[dict]) -> FastAPI:
    """A Mac gateway that answers /overlay-log UNredacted (the mini PC must re-redact)."""
    mac = FastAPI()

    @mac.get("/overlay-log")
    async def log(request: Request, since: int | None = None):
        seen.append({"auth": request.headers.get("authorization"), "since": since})
        return {
            "listener": {"state": "awake"},
            "turns": [_turn("t0", [{"step": "agent_tool", "at": NOW, "tool": "Bash",
                                    "decision": "allow", "output": SECRET_OUTPUT}])],
            "commit": "abc123",
        }

    return mac


@pytest.fixture
def mac_log_proxy(monkeypatch):
    seen: list[dict] = []
    transport = httpx.ASGITransport(app=_fake_mac(seen))
    monkeypatch.setenv(screen.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(overlay_log, "_mac_client", lambda: httpx.AsyncClient(transport=transport))
    return seen


def test_forwarding(gateway_server, mac_log_proxy):
    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                ok = await c.get("/overlay-log")
                assert ok.status_code == 200, ok.text
                assert (await c.get("/overlay-log", params={"since": 0})).status_code == 422
                assert (await c.get("/overlay-log", params={"since": 999999})).status_code == 422
                filtered = await c.get("/overlay-log", params={"since": 600})
                assert filtered.status_code == 200, filtered.text
                return ok.json(), filtered.json()

    ok_body, filtered_body = asyncio.run(run())
    assert mac_log_proxy[0]["auth"] == f"Bearer {IOS_TOKEN}"
    assert mac_log_proxy[0]["since"] is None
    text = json.dumps(ok_body)
    assert SECRET_OUTPUT not in text  # re-redacted even though the Mac sent it raw
    assert set(ok_body["turns"][0]["steps"][0]) == {"step", "at", "tool", "decision"}
    assert ok_body["commit"] == "abc123"
    # since=0 / 999999 never reach the Mac; the valid one forwards as 600
    assert [entry["since"] for entry in mac_log_proxy] == [None, 600]
    assert filtered_body["turns"] == ok_body["turns"]


def test_forwarding_mac_offline_is_503(gateway_server, monkeypatch):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setenv(screen.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(overlay_log, "_mac_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)))

    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                resp = await c.get("/overlay-log")
                assert resp.status_code == 503
                assert resp.json()["detail"] == screen.MAC_OFFLINE_DETAIL

    asyncio.run(run())


def test_forwarding_without_ios_token_is_503(mac_log_proxy, monkeypatch):
    # Direct unit call: over HTTP a missing ios_token fails auth (401) first; _forward's
    # own no-token 503 fires when the token disappears after a cookie-authed session.
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": None)

    async def run():
        with pytest.raises(HTTPException) as exc:
            await overlay_log._forward(None)
        assert exc.value.status_code == 503
        assert exc.value.detail == screen.MAC_OFFLINE_DETAIL

    asyncio.run(run())
    assert mac_log_proxy == []


def test_forwarding_mac_422_passes_through(gateway_server, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad since"})

    monkeypatch.setenv(screen.MAC_GATEWAY_ENV, "http://mac.test")
    monkeypatch.setattr(overlay_log, "_mac_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def run():
        async with gateway_server() as base:
            async with httpx.AsyncClient(base_url=f"http://{base}", headers=IOS_AUTH_HEADER) as c:
                resp = await c.get("/overlay-log")  # valid here; an older Mac refuses it
                assert resp.status_code == 422
                assert resp.json()["detail"] == "bad since"

    asyncio.run(run())


def test_cli_never_prints_token(monkeypatch, capsys):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={
            "listener": {"state": "awake"},
            "turns": [_turn("t0", [
                {"step": "wake", "at": NOW, "score": 0.9},
                {"step": "transcribed", "at": NOW, "text": "what time is it"},
            ])],
        })

    monkeypatch.setattr(config, "get_token",
                        lambda path=None, name="token": "TOKEN-XYZ" if name == "ios_token" else None)
    monkeypatch.setenv("JARVIS_GATEWAY_PORT", "9999")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert cli.main(["--json", "--since", "10m"], client=client) == 0
    out, err = capsys.readouterr()
    assert "TOKEN-XYZ" not in out and "TOKEN-XYZ" not in err
    assert str(seen[0].url.params.get("since")) == "600"
    assert seen[0].headers["authorization"] == "Bearer TOKEN-XYZ"
    assert seen[0].url.host == "127.0.0.1" and str(seen[0].url.port) == "9999"

    assert cli.main([], client=client) == 0
    out, err = capsys.readouterr()
    assert "TOKEN-XYZ" not in out and "TOKEN-XYZ" not in err
    assert "transcribed  what time is it" in out


def test_cli_missing_token_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(config, "get_token", lambda path=None, name="token": None)
    assert cli.main([]) == 2
    out, err = capsys.readouterr()
    assert "no ios_token configured" in err
    assert out == ""


def test_redaction_drops_nested_values_under_kept_names():
    from gateway import overlay_log
    snap = {"turns": [{"id": "t1", "steps": [
        {"step": "intent", "at": 1.0, "detail": {"body": "SECRET-BODY"}, "text": ["SECRET-BODY"]},
        {"step": "agent_tool", "at": 1.0, "tool": "Read", "decision": {"x": "SECRET-BODY"}},
    ]}]}
    out = overlay_log.redact(snap)
    assert "SECRET-BODY" not in repr(out)
    assert out["turns"][0]["steps"][0] == {"step": "intent", "at": 1.0}


def test_ground_row_drops_the_screen_label():
    """D1 'no screen': the picked element's label (escaped quotes included) never leaves the Mac."""
    from gateway import overlay_log
    detail = ('screen=1 image=0 candidates=12 history=0 pick=3 reason=ok via=ax '
              'label="SCREEN-PIXELS \\"Pay\\" now" ms=41')
    snap = {"turns": [{"id": "t1", "steps": [{"step": "ground", "at": 1.0, "detail": detail}]}]}
    row = overlay_log.redact(snap)["turns"][0]["steps"][0]
    assert "SCREEN-PIXELS" not in row["detail"] and "label" not in row["detail"]
    assert row["detail"] == "screen=1 image=0 candidates=12 history=0 pick=3 reason=ok via=ax ms=41"
