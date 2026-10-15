"""RustDesk deep links: GET /screen/rustdesk, the /screen page splice, voice intents.

`screen.RUSTDESK_CONFIG` points at a tmp file; `config.get_token` is patched
name-aware (conftest pattern), so neither ~/.jarvis nor the vault is touched.
A `password` key in the file must never reach any response.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from common import config
from conftest import AUTH_HEADER, IOS_AUTH_HEADER, IOS_TOKEN, TOKEN
from gateway import screen, siri
from mac.voice import brain
from mac.voice import router as voice_router

MAC_IP = "<cgnat-ip>"
PC_IP = "<cgnat-ip>"


@pytest.fixture
def tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config, "get_token",
        lambda path=None, name="token": IOS_TOKEN if name == "ios_token" else TOKEN,
    )


@pytest.fixture
def rd_config(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "rustdesk.json"
    monkeypatch.setattr(screen, "RUSTDESK_CONFIG", path)

    def write(data) -> None:
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    return write


def _request(method: str, url: str, headers: dict | None = None, **kwargs) -> httpx.Response:
    from gateway.app import app

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, url, headers=headers or {}, **kwargs)

    return asyncio.run(run())


# --- GET /screen/rustdesk -------------------------------------------------------

def test_no_token_is_401(tokens, rd_config) -> None:
    rd_config({"mac": MAC_IP})
    assert _request("GET", "/screen/rustdesk").status_code == 401


def test_shared_token_is_401(tokens, rd_config) -> None:
    rd_config({"mac": MAC_IP})
    assert _request("GET", "/screen/rustdesk", AUTH_HEADER).status_code == 401


def test_ios_token_gets_both_links(tokens, rd_config) -> None:
    rd_config({"mac": MAC_IP, "windows": PC_IP})
    resp = _request("GET", "/screen/rustdesk", IOS_AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json() == {"mac": f"rustdesk://{MAC_IP}", "windows": f"rustdesk://{PC_IP}"}


def test_only_configured_targets(tokens, rd_config) -> None:
    rd_config({"windows": PC_IP})
    assert _request("GET", "/screen/rustdesk", IOS_AUTH_HEADER).json() == {
        "windows": f"rustdesk://{PC_IP}"
    }


def test_password_key_never_returned(tokens, rd_config) -> None:
    rd_config({"mac": MAC_IP, "password": "hunter2-secret", "windows": PC_IP,
               "minipc": "<cgnat-ip>"})
    resp = _request("GET", "/screen/rustdesk", IOS_AUTH_HEADER)
    assert set(resp.json()) == {"mac", "windows"}
    assert "hunter2" not in resp.text and "password" not in resp.text


def test_password_smuggled_in_ip_value_dropped(tokens, rd_config) -> None:
    rd_config({"mac": f"{MAC_IP}?password=hunter2", "windows": f"{PC_IP}/x"})
    resp = _request("GET", "/screen/rustdesk", IOS_AUTH_HEADER)
    assert resp.json() == {}
    assert "hunter2" not in resp.text


def test_non_tailscale_ip_dropped(tokens, rd_config) -> None:
    rd_config({"mac": "<lan>.1.10", "windows": "8.8.8.8"})
    assert _request("GET", "/screen/rustdesk", IOS_AUTH_HEADER).json() == {}


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]"])
def test_missing_or_bad_file_is_empty(tokens, rd_config, content) -> None:
    if content is not None:
        rd_config(content)
    resp = _request("GET", "/screen/rustdesk", IOS_AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json() == {}


# --- /screen page splice --------------------------------------------------------

def test_screen_page_carries_links(tokens, rd_config) -> None:
    rd_config({"mac": MAC_IP, "password": "hunter2-secret"})
    resp = _request("GET", "/screen", IOS_AUTH_HEADER)
    assert resp.status_code == 200
    assert f'var RUSTDESK = {{"mac": "rustdesk://{MAC_IP}"}};' in resp.text
    assert "hunter2" not in resp.text
    assert 'id="rd-btn"' in resp.text


def test_screen_page_without_config_has_empty_links(tokens, rd_config) -> None:
    resp = _request("GET", "/screen", IOS_AUTH_HEADER)
    assert "var RUSTDESK = {};" in resp.text


# --- voice intents ----------------------------------------------------------------

@pytest.mark.parametrize("text,cmd", [
    ("open rustdesk mac", "rustdesk_mac"),
    ("Open RustDesk on my Mac screen", "rustdesk_mac"),
    ("rustdesk pc", "rustdesk_windows"),
    ("rust desk windows", "rustdesk_windows"),
    ("rustdesk mini pc", None),
    ("show mac screen", "screen_mac"),
])
def test_router_intents(text, cmd) -> None:
    assert voice_router.route(text) == cmd


def _siri(text: str, headers: dict) -> httpx.Response:
    return _request("POST", "/siri/hey", headers, json={"text": text})


def _no_router(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args, **kwargs):
        raise AssertionError("rustdesk intent must not call the mini PC router")

    monkeypatch.setattr(siri.central, "route", boom)


@pytest.mark.parametrize("headers", [AUTH_HEADER, IOS_AUTH_HEADER])
def test_siri_replies_with_link(tokens, rd_config, monkeypatch, headers) -> None:
    _no_router(monkeypatch)
    rd_config({"mac": MAC_IP, "windows": PC_IP, "password": "hunter2-secret"})
    mac = _siri("open rustdesk mac", headers).json()["reply"]
    pc = _siri("rustdesk pc", headers).json()["reply"]
    assert mac == f"Open RustDesk for your Mac: rustdesk://{MAC_IP}"
    assert pc == f"Open RustDesk for your PC: rustdesk://{PC_IP}"
    assert "hunter2" not in mac + pc


def test_siri_not_configured(tokens, rd_config, monkeypatch) -> None:
    _no_router(monkeypatch)
    rd_config({"mac": MAC_IP})
    assert _siri("rustdesk pc", AUTH_HEADER).json() == {
        "reply": "RustDesk is not set up for the PC"
    }


def test_brain_never_sends_rustdesk_to_agent(monkeypatch) -> None:
    def no_agent(cmd):
        raise AssertionError(f"{cmd} sent to the windows agent")

    monkeypatch.setattr(brain, "_gateway_command", no_agent)
    monkeypatch.setattr(brain, "_claude_bridge", lambda transcript, timeout_s: "bridged")
    assert brain.reply("open rustdesk mac") == "bridged"
