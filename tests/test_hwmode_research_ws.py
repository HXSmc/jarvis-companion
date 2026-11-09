"""/hwmode/ws research turn: "find out about X" -> cards, "start a session about this" -> central.route.

Fake claude CLIs, no network: one stub serves both brains, told apart by its own argv
(the research session carries the web tools, the coaching one stays tool-less)."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import websockets

from conftest import IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hud"

INFO_CARD = json.loads((FIXTURES / "info.json").read_text())["cards"][0]

# Stands in for the lean `claude -p ...` call: records each invocation's argv to
# $FAKE_ARGV and answers every user line with $FAKE_REPLY (or $FAKE_RESEARCH_REPLY
# when the argv carries the web tools).
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
argv = " ".join(sys.argv[1:])
with open(os.environ["FAKE_ARGV"], "a") as f:
    f.write(argv + "\n")
research = "WebSearch" in argv
if research and os.environ.get("FAKE_RESEARCH_FAIL"):
    sys.exit(1)
reply = os.environ.get("FAKE_RESEARCH_REPLY" if research else "FAKE_REPLY", "")
for line in sys.stdin:
    half = len(reply) // 2
    for part in (reply[:half], reply[half:]):
        print(json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": part}}}), flush=True)
    print(json.dumps({"type": "result", "result": reply, "usage": {}}), flush=True)
'''

COACH_REPLY = json.dumps({"say": "Press the latch.", "next_ready": False, "done_seen": False})
RESEARCH_REPLY = json.dumps({"say": "It is a desk accessory.", "cards": [
    INFO_CARD,
    {"id": "c-http", "type": "link", "title": "Insecure", "url": "http://example.com/x"},
    {"id": "c-odd", "type": "arrow", "title": "Unknown kind"},
]})


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    argv = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_ARGV", str(argv))
    monkeypatch.setenv("FAKE_REPLY", COACH_REPLY)
    monkeypatch.setenv("FAKE_RESEARCH_REPLY", RESEARCH_REPLY)
    monkeypatch.delenv("FAKE_RESEARCH_FAIL", raising=False)
    monkeypatch.setenv("HWMODE_CLAUDE_BIN", str(path))
    return argv


class _StubAcks:
    """Stands in for the rendered clips: every kind answers at once."""

    def clip(self, kind):
        return ({"research": "Checking.", "session": "Starting that session now.",
                 "wait": "One moment.", "done": "Right."}[kind], b"\x01\x00", 24000)


async def _ask_until_reply(ws, text, use_frame=False):
    await ws.send(json.dumps({"type": "ask", "text": text, "frame": use_frame}))
    msgs = []
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), 10))
        msgs.append(m)
        if m["type"] == "reply":
            return msgs
        if m["type"] == "error":
            raise AssertionError(m)


def _run(coro):
    asyncio.run(coro)


def test_research_turn_emits_cards(gateway_server, fake_claude, monkeypatch):
    from gateway import hwmode
    monkeypatch.setattr(hwmode, "get_acks", lambda voice: _StubAcks())

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws",
                                          **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": ""}))
                assert json.loads(await ws.recv())["type"] == "ready"
                msgs = await _ask_until_reply(ws, "find out about the Busy Bar")
                kinds = [f"{m['type']}:{m['state']}" if m["type"] == "research" else m["type"] for m in msgs]
                assert kinds == ["ack", "research:started", "research:done", "reply"], kinds
                reply = msgs[-1]
                assert reply["data"]["say"] == "It is a desk accessory."
                assert reply["data"]["shape"] is None
                assert [c["id"] for c in reply["data"]["cards"]] == [INFO_CARD["id"]]
                msgs = await _ask_until_reply(ws, "where is the latch?", use_frame=False)
                assert msgs[-1]["data"]["say"] == "Press the latch."  # coaching still alive
    _run(run())
    # Each invocation starts with the same argv head; the system prompts span lines.
    chunks = ["-p --input-format" + c for c in fake_claude.read_text().split("-p --input-format")[1:]]
    research_argv = [c for c in chunks if "WebSearch" in c]
    coach_argv = [c for c in chunks if "WebSearch" not in c]
    assert research_argv and "--allowedTools WebSearch,WebFetch" in research_argv[0]
    assert coach_argv, "the coaching session must still exist"
    parts = coach_argv[0].split(" ")
    assert parts[parts.index("--tools") + 1] == ""  # coaching stays tool-less


def test_start_session_from_research(gateway_server, fake_claude, monkeypatch):
    from gateway import central, hwmode
    monkeypatch.setattr(hwmode, "get_acks", lambda voice: _StubAcks())
    calls = []

    async def fake_route(thread_id, msg_id, text, source, *, engine=None, new_only=False):
        calls.append({"thread_id": thread_id, "text": text, "source": source, "new_only": new_only})
        return {"session_id": "s-new", "title": "Busy Bar notes"}

    monkeypatch.setattr(central, "route", fake_route)

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws",
                                          **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": ""}))
                assert json.loads(await ws.recv())["type"] == "ready"
                await _ask_until_reply(ws, "find out about the Busy Bar desk display")
                msgs = await _ask_until_reply(ws, "start a session about this")
                session = [m for m in msgs if m["type"] == "session"]
                assert session == [{"type": "session", "session_id": "s-new", "title": "Busy Bar notes"}]
                assert msgs[-1]["data"]["say"] == "Session started: Busy Bar notes."
    _run(run())
    [call] = calls
    assert call["new_only"] is True and call["source"] == "app" and call["thread_id"].startswith("hwmode-")
    text = call["text"]
    assert "Busy Bar desk display" in text and "It is a desk accessory." in text
    assert "Bearer" not in text and "ANTHROPIC" not in text


def test_start_session_without_research_asks_first(gateway_server, fake_claude, monkeypatch):
    from gateway import central
    calls = []

    async def fake_route(*a, **k):
        calls.append((a, k))
        return {"session_id": "s-new", "title": "t"}

    monkeypatch.setattr(central, "route", fake_route)

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws",
                                          **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": ""}))
                assert json.loads(await ws.recv())["type"] == "ready"
                msgs = await _ask_until_reply(ws, "start a session about this")
                assert msgs[-1]["data"]["say"] == ("Ask me to look something up first, "
                                                   "then I can start a session on it.")
                assert not [m for m in msgs if m["type"] == "session"]
    _run(run())
    assert calls == []  # nothing routed without findings


def test_research_failure_is_spoken_not_fatal(gateway_server, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_RESEARCH_FAIL", "1")

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws",
                                          **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": ""}))
                assert json.loads(await ws.recv())["type"] == "ready"
                msgs = await _ask_until_reply(ws, "what is this thing on the desk")
                assert any(m == {"type": "research", "state": "done"} for m in msgs)
                reply = msgs[-1]
                assert reply["data"]["say"] == "I couldn't look that up just now."
                assert reply["data"]["cards"] == []
                msgs = await _ask_until_reply(ws, "where is the latch?", use_frame=False)
                assert msgs[-1]["data"]["say"] == "Press the latch."  # the socket still answers
    _run(run())
