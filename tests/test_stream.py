"""H.264 stream prototype: helper framing, Gate, Qos, Backlog, xdotool mapping, WS auth + flow.

The WS tests run a fake helper (tests/fake_stream_helper.py) through
STREAM_HELPER_CMD, so no X display, GStreamer, or GPU is needed. Tokens come
from the conftest `gateway_server` fixture, never the real vault.
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import AUTH_HEADER, IOS_AUTH_HEADER

from gateway import stream
from linux import stream_helper

FAKE = Path(__file__).resolve().parent / "fake_stream_helper.py"


@pytest.fixture(autouse=True)
def _reset_live():
    stream._live = 0
    yield
    start = time.monotonic()
    while stream._live and time.monotonic() - start < 2:
        time.sleep(0.05)
    stream._live = 0


def _client() -> TestClient:
    from gateway.app import app

    return TestClient(app)


def _headers() -> dict:
    return dict(IOS_AUTH_HEADER)  # websocket_connect mutates the dict it gets


# ---- pure pieces ----

def test_helper_pack_matches_gateway_reader():
    frame = stream_helper.pack(b"\x00\x00\x00\x01\x65abc", True, 12.5)

    async def parse():
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()
        return await stream.read_au(reader)

    assert asyncio.run(parse()) == (1, 12.5, b"\x00\x00\x00\x01\x65abc")


def test_read_au_rejects_huge_length():
    async def parse():
        reader = asyncio.StreamReader()
        reader.feed_data(stream.HELPER_HEADER.pack(stream.MAX_AU_BYTES + 1, 0, 0.0))
        return await stream.read_au(reader)

    with pytest.raises(ValueError):
        asyncio.run(parse())


def test_gate_fps_cap_unchanged_skip_and_force():
    gate = stream_helper.Gate(fps=10)
    assert gate.admit(1.0, b"a")  # first frame always
    assert not gate.admit(1.05, b"b")  # too soon for 10 fps
    assert not gate.admit(1.2, b"a")  # unchanged screen
    assert gate.admit(1.3, b"b")  # changed
    gate.force = True
    assert gate.admit(1.5, b"b")  # keyframe request lets an unchanged frame through


def test_qos_degrades_qp_then_fps_and_recovers():
    qos = stream.Qos()
    assert qos.update(20) is None  # sets the session minimum
    changes = [qos.update(400) for _ in range(3 * 7)]
    applied = [c for c in changes if c]
    assert applied[0] == {"qp": 30}
    assert {"qp": stream.QP_MAX} in applied
    assert applied[-1] == {"fps": 7}  # 30 -> 15 -> 7 once QP is maxed
    recover = [c for c in (qos.update(25) for _ in range(5 * 3)) if c]
    assert recover[:2] == [{"fps": 14}, {"fps": 28}]  # fps comes back before quality


def test_backlog_drops_to_next_keyframe():
    async def run():
        b = stream.Backlog()
        assert not b.push(1, 0, b"K")
        for i in range(stream.BACKLOG_DROP - 1):
            assert not b.push(0, i, b"d")
        assert b.push(0, 9, b"late")  # backlog full -> ask for a keyframe
        assert not b.push(0, 10, b"skipped")  # deltas dropped while waiting
        assert not b.push(1, 11, b"K2")
        return list(b.q)

    assert asyncio.run(run()) == [(1, 11, b"K2")]


def test_xdotool_mapping():
    w, h = 1921, 1081  # (w-1, h-1) = 1920x1080 so 0.5 -> 960/540
    assert stream.xdotool_args({"type": "tap", "x": 0.5, "y": 0.5, "button": "left"}, w, h) == [
        "mousemove", "--sync", "960", "540", "click", "1"]
    assert stream.xdotool_args({"type": "right", "x": 0, "y": 1, "button": "left"}, w, h)[-1] == "3"
    drag = stream.xdotool_args({"type": "drag", "x": 0.5, "y": 0.5, "dx": 0.25, "dy": -0.9, "button": "left"}, w, h)
    assert drag == ["mousemove", "--sync", "960", "540", "mousedown", "1",
                    "mousemove", "--sync", "1440", "0", "mouseup", "1"]
    assert stream.xdotool_args({"type": "scroll", "dy": 1, "dx": None}, w, h) == ["click", "--repeat", "3", "5"]
    assert stream.xdotool_args({"type": "text", "text": "-rf"}, w, h) == ["type", "--delay", "0", "--", "-rf"]
    assert stream.xdotool_args({"type": "key", "key": "return", "mods": ["ctrl", "shift"]}, w, h) == [
        "key", "--", "ctrl+shift+Return"]


# ---- HTTP page ----

def test_stream_page_auth(gateway_server):
    client = _client()
    assert client.get("/screen/stream").status_code == 401
    assert client.get("/screen/stream", headers=AUTH_HEADER).status_code == 403
    resp = client.get("/screen/stream", headers=IOS_AUTH_HEADER)
    assert resp.status_code == 200 and "VideoDecoder" in resp.text
    assert "screen_session=" in resp.headers["set-cookie"]


# ---- WebSocket ----

def _closed_code(headers: dict, path: str = "/screen/minipc/stream") -> int:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client().websocket_connect(path, headers=headers) as ws:
            while True:  # hello/ping until the server closes
                ws.receive_text()
    return exc_info.value.code


def test_ws_rejects_no_auth_and_shared_token(gateway_server):
    assert _closed_code({}) in (1008, 403)
    assert _closed_code(dict(AUTH_HEADER)) in (1008, 403)


def test_ws_unknown_target_4005(gateway_server):
    assert _closed_code(_headers(), "/screen/windows/stream") == 4005


def test_ws_session_cap_4004(gateway_server, monkeypatch):
    monkeypatch.setattr(stream, "_live", stream.MAX_STREAM_SESSIONS)
    assert _closed_code(_headers()) == 4004


def test_ws_helper_dies_4006(gateway_server, monkeypatch):
    monkeypatch.setenv("STREAM_HELPER_CMD", f"{sys.executable} {FAKE} --die")
    assert _closed_code(_headers()) == 4006


def test_ws_video_keyframe_and_input(gateway_server, monkeypatch):
    monkeypatch.setenv("STREAM_HELPER_CMD", f"{sys.executable} {FAKE}")
    injected: list = []

    async def fake_inject(events, env, size):
        injected.append((events, size))

    async def fake_size(env):
        return (1920, 1080)

    monkeypatch.setattr(stream, "_inject", fake_inject)
    monkeypatch.setattr(stream, "_display_size", fake_size)
    with _client().websocket_connect("/screen/minipc/stream", headers=_headers()) as ws:
        assert json.loads(ws.receive_text()) == {"type": "hello", "target": "minipc"}

        def next_binary() -> bytes:
            while True:
                msg = ws.receive()
                if msg.get("bytes") is not None:
                    return msg["bytes"]
                if json.loads(msg["text"])["type"] == "ping":
                    continue

        first = next_binary()
        flags, ts = struct.unpack_from("<Bd", first)
        assert flags == 1 and first[9:] == b"\x00\x00\x00\x01\x67KEY0"
        ws.send_text(json.dumps({"type": "keyframe"}))
        again = next_binary()
        assert again[0] == 1 and again[9:] == b"\x00\x00\x00\x01\x67KEY1"
        ws.send_text(json.dumps({"type": "input", "events": [{"type": "tap", "x": 0.1, "y": 0.2}]}))
        ws.send_text(json.dumps({"type": "input", "events": [{"type": "tap", "x": 7, "y": 0.2}]}))  # invalid
        ws.send_text("not json")
        ws.send_text(json.dumps({"type": "keyframe"}))
        assert next_binary()[9:] == b"\x00\x00\x00\x01\x67KEY2"  # socket still alive after junk
    assert len(injected) == 1
    assert injected[0][0][0]["type"] == "tap" and injected[0][1] == (1920, 1080)
