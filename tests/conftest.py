"""Shared gateway test harness: fixture tokens + an in-process uvicorn server factory."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import uvicorn
import websockets

from common import config

# Off macOS (the mini PC box) the Quartz driver can't load: ctypes raises
# OSError, which the per-file ModuleNotFoundError stubs don't catch. Install the
# same stub here first; on the Mac the real driver imports and wins.
try:
    from mac.screen import driver as _mac_driver  # noqa: F401
except OSError:
    import types

    _mac_driver = types.ModuleType("mac.screen.driver")
    _mac_driver.capture_jpeg = lambda w, q: (b"", w, q)
    _mac_driver.send_events = lambda events: None
    _mac_driver.tcc_ok = lambda: True
    _mac_driver.display_asleep = lambda: False
    _pkg = types.ModuleType("mac.screen")
    _pkg.driver = _mac_driver
    sys.modules["mac.screen"] = _pkg
    sys.modules["mac.screen.driver"] = _mac_driver

TOKEN = "jarvis_test_token_123"
IOS_TOKEN = "jarvis_ios_test_token_456"
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}
IOS_AUTH_HEADER = {"Authorization": f"Bearer {IOS_TOKEN}"}
# websockets 14.0 renamed connect()'s extra_headers to additional_headers.
WS_HEADERS_KWARG = (
    "additional_headers"
    if int(websockets.version.version.split(".")[0]) >= 14
    else "extra_headers"
)


@pytest.fixture
def gateway_server(monkeypatch):
    """Async-CM factory running the real app in-process on port 0.

    The server runs as an asyncio task in the caller's own event loop — the
    registry's sockets are loop-bound, so `registry.send_to_role` and the
    flow handlers must share the loop the test's `asyncio.run` created.
    `config.get_token` is patched name-aware (S2): ios -> ios_token, else the
    shared token. Usage inside a test's asyncio.run():
    `async with gateway_server() as base:`.
    """
    monkeypatch.setattr(
        config, "get_token",
        lambda path=None, name="token": IOS_TOKEN if name == "ios_token" else TOKEN,
    )

    @contextlib.asynccontextmanager
    async def _server():
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

    return _server


@pytest.fixture(autouse=True)
def no_real_voice(monkeypatch, request):
    """Hardware Mode tests never load the box's real Kokoro voice, unless a live test asks for it."""
    if "live_voice" in request.keywords:
        return
    from gateway import hwmode
    monkeypatch.setattr(hwmode, "get_voice", lambda: None)


@pytest.fixture(autouse=True)
def isolate_central_state(monkeypatch, tmp_path):
    """No test touches ~/.jarvis/central_state.json, the vault, or a real model."""
    from gateway import central

    monkeypatch.setattr(central, "STATE_PATH", tmp_path / "central_state.json")
    monkeypatch.setattr(central, "_generate_plain", lambda session: None)
    monkeypatch.setenv("JARVIS_CARDS_PATH", str(tmp_path / "cards.json"))
    monkeypatch.setenv("JARVIS_CARD_FILES_DIR", str(tmp_path / "card_files"))
    monkeypatch.setenv("MINIPC_ROUTER_DB", str(tmp_path / "no-router.db"))  # tests never read the live router
    monkeypatch.setattr(central, "_inflight", set())
    monkeypatch.setattr(central, "_failed", {})
    monkeypatch.setattr(central, "_known_ids", set())
