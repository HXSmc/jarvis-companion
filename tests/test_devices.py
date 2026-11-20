"""sshd device-action tests: allowlist rejection, exact constant argv, timeout, intent parse.

No network: `devices._exec` (the subprocess seam) is stubbed — ssh never runs.
The endpoint rides a minimal FastAPI app mounting only `devices.router`
(gateway/app.py owns the real mount). Intent cases go through
intents.device_request and the full intents.parse fast path.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway import devices
from mac.voice import intents

TOKEN = "jarvis_test_token_123"


@pytest.fixture
def stubbed_token(monkeypatch: pytest.MonkeyPatch) -> str:
    from common import config

    monkeypatch.setattr(
        config, "get_token", lambda path=None, name="token": TOKEN if name == "token" else None
    )
    return TOKEN


class FakeProc:
    def __init__(self, out: bytes = b"blocked (game-mode on)", rc: int = 0) -> None:
        self._out = out
        self.returncode = rc
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, b""

    def kill(self) -> None:
        self.killed = True


class HangingProc(FakeProc):
    """Spawns fine, then hangs in communicate() — the second timeout path."""

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        return b"", b""


def _stub_exec(calls: list, out: bytes = b"ok", rc: int = 0,
               hang: str = "", procs: list | None = None) -> object:
    async def fake_exec(*argv, **kwargs):
        calls.append(list(argv))
        if hang == "spawn":
            await asyncio.Event().wait()  # wait_for cancels this — the spawn-timeout path
        proc = HangingProc(out, rc) if hang == "run" else FakeProc(out, rc)
        if procs is not None:
            procs.append(proc)
        return proc

    return fake_exec


def _post_action(app, token: str | None, body: dict) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else None

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/device/action", headers=headers, json=body)

    return asyncio.run(run())


@pytest.fixture
def devices_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(devices.router)
    return app


def test_unknown_action_id_rejected(monkeypatch: pytest.MonkeyPatch, devices_app, stubbed_token) -> None:
    """The core security property: any id outside ACTIONS is a 404, and ssh never runs."""
    calls: list = []
    monkeypatch.setattr(devices, "_exec", _stub_exec(calls))
    for bad in ("winpc.shutdown", "winpc.gaming_mode_on; rm -rf /", "mac.reboot",
                "winpc.gaming_mode", "", "winpc.gaming_mode_on extra"):
        resp = _post_action(devices_app, TOKEN, {"id": bad})
        assert resp.status_code == 404, bad
    assert calls == []


def test_allowlisted_id_runs_exact_constant_argv(monkeypatch: pytest.MonkeyPatch, devices_app, stubbed_token) -> None:
    calls: list = []
    monkeypatch.setattr(devices, "_exec", _stub_exec(calls, out=b"game mode ON"))
    resp = _post_action(devices_app, TOKEN, {"id": "winpc.gaming_mode_on"})
    assert resp.status_code == 200
    assert resp.json() == {"id": "winpc.gaming_mode_on", "ok": True, "output": "game mode ON"}
    assert calls == [
        ["ssh", "-o", f"ConnectTimeout={devices.CONNECT_TIMEOUT_S}",
         "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3", "-o", "BatchMode=yes",
         "winpc", "C:\\Users\\i0562\\llm-switch\\bin\\game-mode.cmd on"]
    ]


def test_remote_failure_reports_ok_false(monkeypatch: pytest.MonkeyPatch, devices_app, stubbed_token) -> None:
    calls: list = []
    monkeypatch.setattr(devices, "_exec", _stub_exec(calls, out=b"refused", rc=1))
    resp = _post_action(devices_app, TOKEN, {"id": "winpc.gaming_mode_off"})
    assert resp.json()["ok"] is False
    assert resp.json()["output"] == "refused"


def test_timeout_returns_504_spawn(monkeypatch: pytest.MonkeyPatch, devices_app, stubbed_token) -> None:
    monkeypatch.setattr(devices, "_exec", _stub_exec([], hang="spawn"))
    monkeypatch.setattr(devices, "RUN_TIMEOUT_S", 0.05)
    resp = _post_action(devices_app, TOKEN, {"id": "winpc.gaming_mode_status"})
    assert resp.status_code == 504
    assert "timed out" in resp.json()["detail"]


def test_timeout_returns_504_and_kills_hung_ssh(monkeypatch: pytest.MonkeyPatch, devices_app, stubbed_token) -> None:
    """ssh spawns but the remote command hangs: 504 AND the process is killed."""
    procs: list = []
    monkeypatch.setattr(devices, "_exec", _stub_exec([], hang="run", procs=procs))
    monkeypatch.setattr(devices, "RUN_TIMEOUT_S", 0.05)
    resp = _post_action(devices_app, TOKEN, {"id": "winpc.gaming_mode_status"})
    assert resp.status_code == 504
    assert "timed out" in resp.json()["detail"]
    assert procs and procs[0].killed


def test_endpoint_requires_bearer(devices_app) -> None:
    resp = _post_action(devices_app, None, {"id": "winpc.gaming_mode_on"})
    assert resp.status_code in (401, 403)


def test_no_destructive_actions_in_allowlist() -> None:
    """Shutdown/reboot/delete style words never appear in any allowlisted command."""
    joined = " ".join(v["command"] for v in devices.ACTIONS.values()).lower()
    for word in ("shutdown", "reboot", "restart", "rm ", "del ", "format"):
        assert word not in joined


# --- intent parser ---

@pytest.mark.parametrize("text,expected", [
    ("turn on gaming mode on the PC", "winpc.gaming_mode_on"),
    ("Turn off gaming mode", "winpc.gaming_mode_off"),
    ("okay jarvis turn off gaming mode on the pc please", "winpc.gaming_mode_off"),
    ("gaming mode status", "winpc.gaming_mode_status"),
    ("is gaming mode on?", "winpc.gaming_mode_status"),
    ("can you turn on gaming mode", "winpc.gaming_mode_on"),
    ("turn gaming mode on", "winpc.gaming_mode_on"),
    ("gaming mode on", "winpc.gaming_mode_on"),
    ("gaming mode off please", "winpc.gaming_mode_off"),
    ("turn on game mode", "winpc.gaming_mode_on"),
])
def test_device_request_matches(text: str, expected: str) -> None:
    assert intents.device_request(text) == intents.Intent("device", expected)


@pytest.mark.parametrize("text", [
    "gaming mode",
    "what is gaming",
    "turn on the lights",
    "on gaming",
    "open gaming mode app",  # "open" paths are app/workspace kinds, never a device run
    "turn off the overlay",
    "mode on",
])
def test_device_request_non_matches(text: str) -> None:
    assert intents.device_request(text) is None


def test_parse_fast_path_picks_device() -> None:
    """The full parse() fast path routes the spoken phrase to the device kind."""
    hit = intents.parse("turn on gaming mode on the PC")
    assert hit.kind == "device" and hit.arg == "winpc.gaming_mode_on"
