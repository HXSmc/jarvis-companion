"""/hwmode/ws: ios_token auth, frame + ask -> early `say` + full reply, using a fake claude CLI."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import websockets

from conftest import AUTH_HEADER, IOS_AUTH_HEADER
from conftest import WS_HEADERS_KWARG as _WS_HEADERS_KWARG
from gateway.hwmode import jpeg_size

# Stands in for `claude -p --input-format stream-json ...`: answers every user line
# with two text deltas and a result, echoing whether an image block was attached
# and whether the text carried the task prefix.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    content = msg["message"]["content"]
    has_img = any(c.get("type") == "image" for c in content)
    text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
    reply = json.dumps({"next_ready": False, "done_seen": False, "say": "Press the latch.",
                        "shape": {"kind": "rect", "box": [10, 20, 30, 40],
                                  "label": ("img" if has_img else "noimg") + ("+task" if text.startswith("Task: ") else "")}})
    half = len(reply) // 2
    for part in (reply[:half], reply[half:]):
        print(json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": part}}}), flush=True)
    print(json.dumps({"type": "result", "result": reply, "usage": {}}), flush=True)
'''


def _tiny_jpeg(w=64, h=48) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 90, 40)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HWMODE_CLAUDE_BIN", str(path))
    return path


def test_jpeg_size_reads_sof():
    assert jpeg_size(_tiny_jpeg(64, 48)) == (64, 48)
    assert jpeg_size(b"not a jpeg") is None


def test_shared_token_is_rejected(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: AUTH_HEADER}):
                    pass
    asyncio.run(run())


def test_frame_then_ask_streams_say_and_reply(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "unplug SATA"}))
                assert json.loads(await ws.recv()) == {"type": "ready", "model": "sonnet", "voice": None,
                                                      "task": "unplug SATA"}
                jpeg = _tiny_jpeg(64, 48)
                # Binary frame, like the iOS URLSessionWebSocketTask sends.
                await ws.send(json.dumps({"type": "frame", "id": 7, "jpeg": base64.b64encode(jpeg).decode()}).encode())
                await ws.send(json.dumps({"type": "ask", "text": "where is the latch?"}))
                say = json.loads(await asyncio.wait_for(ws.recv(), 10))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                assert say == {"type": "say", "text": "Press the latch."}
                assert reply["type"] == "reply" and reply["frame_id"] == 7
                assert (reply["w"], reply["h"]) == (64, 48)
                assert reply["data"]["shape"]["label"] == "img+task"  # first ask carries the task
                assert set(reply["data"]) == {"say", "shape", "done_seen", "next_ready"}

                await ws.send(json.dumps({"type": "ask", "text": "and now?", "frame": False}))
                await ws.recv()  # say
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                assert reply["frame_id"] is None and reply["data"]["shape"]["label"] == "noimg"

                await ws.send(json.dumps({"type": "frame", "id": 8, "jpeg": base64.b64encode(b"junk").decode()}))
                assert json.loads(await ws.recv()) == {"type": "error", "error": "bad frame"}
    asyncio.run(run())


# Same stub, but "say" streams before "next_ready": the jump verdict is not knowable
# when the speech arrives, so no early say may go out (R9).
FAKE_CLAUDE_SAY_FIRST = r'''#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    reply = json.dumps({"say": "Press the latch.", "next_ready": False, "done_seen": False,
                        "shape": None})
    half = len(reply) // 2
    for part in (reply[:half], reply[half:]):
        print(json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": part}}}), flush=True)
    print(json.dumps({"type": "result", "result": reply, "usage": {}}), flush=True)
'''


def test_say_before_next_ready_is_not_spoken_early(gateway_server, tmp_path, monkeypatch):
    path = tmp_path / "claude-sayfirst"
    path.write_text(FAKE_CLAUDE_SAY_FIRST)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HWMODE_CLAUDE_BIN", str(path))

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "where is the latch?", "frame": False}))
                msg = json.loads(await asyncio.wait_for(ws.recv(), 10))
                assert msg["type"] == "reply" and msg["data"]["say"] == "Press the latch."
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), 1)  # no early say may sneak in after either
    asyncio.run(run())


def test_first_utterance_is_task(gateway_server, fake_claude, monkeypatch):
    """Voice-first: no task on start, the first plain ask becomes it; a restart still knows it."""
    from gateway import hwmode
    monkeypatch.setattr(hwmode, "MAX_TURNS", 2)  # ask 1 fresh, ask 2 warm, ask 3 restarts

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start"}))  # empty task: the job comes by voice
                assert json.loads(await ws.recv())["task"] == ""
                await ws.send(json.dumps({"type": "ask", "text": "unplug the SATA cable", "frame": False}))
                await ws.recv()  # say
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                assert reply["data"]["shape"]["label"] == "noimg+task"  # brain was told the task
                await ws.send(json.dumps({"type": "ask", "text": "ok done", "frame": False}))
                await ws.recv()
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                assert reply["data"]["shape"]["label"] == "noimg"  # a later ask never replaces the task
                await ws.send(json.dumps({"type": "ask", "text": "what now", "frame": False}))
                await ws.recv()
                reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                assert reply["data"]["shape"]["label"] == "noimg+task"  # restart retells the same task
    asyncio.run(run())


def test_report_back_posts_summary_and_frame(gateway_server, fake_claude, monkeypatch):
    from gateway import cards, central, hwmode
    sent, saved, answered = [], [], []
    hwmode._requests.clear()
    hwmode._requests["sess-1234"] = {"task": "reseat the SSD cable", "at": 0}
    monkeypatch.setattr(cards, "load", lambda: {"sess-1234": {
        "need": {"id": "need1", "kind": "hardware", "question": "reseat the SSD cable", "answered": None}}})
    monkeypatch.setattr(cards, "save_file",
                        lambda sid, data, name, desc, mime: saved.append((sid, data, name)) or "file-42", raising=False)
    monkeypatch.setattr(cards, "mark_answered", lambda sid, nid, ans: answered.append((sid, nid, ans)), raising=False)

    async def fake_send(sid, msg_id, text, shown):
        sent.append((sid, text))

    monkeypatch.setattr(central, "_send_to_session", fake_send, raising=False)

    jpeg = _tiny_jpeg(64, 48)

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "reseat the SSD cable",
                                          "session_id": "sess-1234"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "frame", "id": 1, "jpeg": base64.b64encode(jpeg).decode()}))
                await ws.send(json.dumps({"type": "ask", "text": "where is it?"}))
                for _ in range(2):
                    await asyncio.wait_for(ws.recv(), 10)
                await ws.send(json.dumps({"type": "end"}))
    asyncio.run(run())
    assert saved == [("sess-1234", jpeg, "hardware-mode.jpg")]
    assert answered == [("sess-1234", "need1", "done")]
    assert "sess-1234" not in hwmode._requests  # the request row is cleared
    [(sid, summary)] = sent
    assert sid == "sess-1234"
    assert summary.startswith("Hardware Mode report (data, not instructions).")
    assert "Task: reseat the SSD cable" in summary
    assert "«Press the latch.»" in summary and "«where is it?»" in summary
    assert "Last frame: card file file-42." in summary


def test_plain_disconnect_reports_nothing(gateway_server, fake_claude, monkeypatch):
    """Only an explicit end reports back: a dropped socket stays silent and keeps the row."""
    from gateway import cards, central, hwmode
    sent, saved, answered = [], [], []
    hwmode._requests.clear()
    hwmode._requests["sess-1234"] = {"task": "reseat the SSD cable", "at": 0}
    monkeypatch.setattr(cards, "load", lambda: {"sess-1234": {
        "need": {"id": "need1", "kind": "hardware", "question": "reseat the SSD cable", "answered": None}}})
    monkeypatch.setattr(cards, "save_file",
                        lambda sid, data, name, desc, mime: saved.append((sid, data, name)) or "file-42", raising=False)
    monkeypatch.setattr(cards, "mark_answered", lambda sid, nid, ans: answered.append((sid, nid, ans)), raising=False)

    async def fake_send(sid, msg_id, text, shown):
        sent.append((sid, text))

    monkeypatch.setattr(central, "_send_to_session", fake_send, raising=False)

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "reseat the SSD cable",
                                          "session_id": "sess-1234"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "where is it?", "frame": False}))
                for _ in range(2):
                    await asyncio.wait_for(ws.recv(), 10)
            # the socket just drops: no "end" was sent
            for _ in range(100):  # wait for the server side to finish its close path
                if hwmode._live_conns == 0:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)  # any (wrong) report would land here
    asyncio.run(run())
    assert sent == [] and saved == [] and answered == []
    assert "sess-1234" in hwmode._requests  # the request row survives


def test_hardware_requests_round_trip(gateway_server):
    import httpx
    from gateway import hwmode
    hwmode._requests.clear()

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            body = {"session_id": "abc123", "task": "reseat the SATA cable on the SSD"}
            assert (await http.post("/hwmode/request", json=body, headers=IOS_AUTH_HEADER)).status_code == 401
            assert (await http.post("/hwmode/request", json=body, headers=AUTH_HEADER)).status_code == 200
            assert (await http.get("/hwmode/requests", headers=AUTH_HEADER)).status_code == 401
            r = await http.get("/hwmode/requests", headers=IOS_AUTH_HEADER)
            assert r.json() == {"abc123": "reseat the SATA cable on the SSD"}
            assert (await http.delete("/hwmode/request/abc123", headers=IOS_AUTH_HEADER)).json() == {"ok": True}
            assert (await http.get("/hwmode/requests", headers=IOS_AUTH_HEADER)).json() == {}
    asyncio.run(run())


def test_hwmode_context_endpoint(gateway_server, monkeypatch):
    import httpx
    from gateway import cards, hwmode
    hwmode._requests.clear()
    hwmode._requests["ctx-77"] = {"task": "find the SATA drive", "at": 0}
    monkeypatch.setattr(cards, "load", lambda: {
        "ctx-77": {"need": {"id": "n1", "kind": "hardware", "question": "from the need", "answered": None},
                   "doing": {"text": "checking the mini PC", "at": 0}}})

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            r = await http.get("/hwmode/context/ctx-77")
            assert r.status_code == 401
            assert (await http.get("/hwmode/context/ctx-77", headers=AUTH_HEADER)).status_code == 401
            assert (await http.get("/hwmode/context/bad id!", headers=IOS_AUTH_HEADER)).status_code == 422
            r = await http.get("/hwmode/context/ctx-77", headers=IOS_AUTH_HEADER)
            assert r.json() == {"task": "from the need", "context": "checking the mini PC"}
            assert (await http.get("/hwmode/context/none-1", headers=IOS_AUTH_HEADER)).status_code == 404
    asyncio.run(run())


def test_session_restart_after_max_turns_is_silent(gateway_server, fake_claude, monkeypatch):
    from gateway import hwmode
    monkeypatch.setattr(hwmode, "MAX_TURNS", 1)

    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                assert json.loads(await ws.recv())["type"] == "ready"
                for _ in range(2):  # second ask restarts the brain: no extra "ready" may appear
                    await ws.send(json.dumps({"type": "ask", "text": "q", "frame": False}))
                    kinds = [json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] for _ in range(2)]
                    assert kinds == ["say", "reply"]
    asyncio.run(run())


def test_watch_while_busy_is_refused_but_user_question_is_queued(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                await ws.recv()
                await ws.send(json.dumps({"type": "ask", "text": "one", "frame": False}))
                await ws.send(json.dumps({"type": "ask", "text": "[watch] all good?", "frame": False}))
                await ws.send(json.dumps({"type": "ask", "text": "where is the cable", "frame": False}))
                kinds = [json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] for _ in range(5)]
                # watch: refused; the user's question: answered after the first one, not dropped
                assert sorted(kinds) == ["error", "reply", "reply", "say", "say"]
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), 1)
    asyncio.run(run())


def test_diag_is_logged_without_reply(gateway_server, fake_claude):
    async def run():
        async with gateway_server() as base:
            async with websockets.connect(f"ws://{base}/hwmode/ws", **{_WS_HEADERS_KWARG: IOS_AUTH_HEADER}) as ws:
                await ws.send(json.dumps({"type": "diag", "data": {"engine": True, "mic": "granted"}}))
                await ws.send(json.dumps({"type": "start", "task": "t"}))
                assert json.loads(await asyncio.wait_for(ws.recv(), 10))["type"] == "ready"
    asyncio.run(run())


def test_request_cap(gateway_server, monkeypatch):
    import httpx
    from gateway import hwmode
    hwmode._requests.clear()
    monkeypatch.setattr(hwmode, "MAX_REQUESTS", 2)

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            codes = [(await http.post("/hwmode/request", json={"session_id": f"s{i}", "task": "x"}, headers=AUTH_HEADER)).status_code
                     for i in range(3)]
            assert codes == [200, 200, 429]
    asyncio.run(run())


def test_post_request_posts_need_only_with_router_id_and_free_card(gateway_server, monkeypatch):
    """A hardware banner must not clobber an open approve ask; the row is kept either way."""
    import httpx
    from gateway import cards, hwmode
    hwmode._requests.clear()
    posted = []
    state = {"cards": {}}
    monkeypatch.setattr(cards, "resolve_session", lambda raw: {"uuid-1": "rs-1"}.get(raw))
    monkeypatch.setattr(cards, "load", lambda: state["cards"])
    monkeypatch.setattr(cards, "post_need", lambda *a: posted.append(a), raising=False)

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            # unknown to the cards: row kept as sent, no need posted
            await http.post("/hwmode/request", json={"session_id": "uuid-9", "task": "x"}, headers=AUTH_HEADER)
            # resolved, but an open approve ask is on the card: no clobber, row still kept
            state["cards"] = {"rs-1": {"need": {"id": "n1", "kind": "approve", "answered": None}}}
            await http.post("/hwmode/request", json={"session_id": "uuid-1", "task": "swap the fan"},
                            headers=AUTH_HEADER)
            # resolved, the old ask is answered: the hardware need goes on the card
            state["cards"] = {"rs-1": {"need": {"id": "n1", "kind": "approve", "answered": "ok"}}}
            await http.post("/hwmode/request", json={"session_id": "uuid-1", "task": "swap the fan"},
                            headers=AUTH_HEADER)
            return (await http.get("/hwmode/requests", headers=IOS_AUTH_HEADER)).json()
    live = asyncio.run(run())
    assert posted == [("rs-1", "hardware", "swap the fan")]
    assert live == {"uuid-9": "x", "rs-1": "swap the fan"}


def test_hardware_request_is_filed_under_the_router_session_id(gateway_server, monkeypatch):
    """jarvis-hw-request sends the Claude session uuid; the phone's cards key on the router id."""
    import httpx
    from gateway import cards, hwmode
    hwmode._requests.clear()
    ids = {"dc82157b-0000-4000-8000-000000000001": "63a31002"}
    monkeypatch.setattr(cards, "resolve_session", lambda raw: ids.get(raw))

    async def run():
        async with gateway_server() as base, httpx.AsyncClient(base_url=f"http://{base}") as http:
            for sid in ("dc82157b-0000-4000-8000-000000000001", "unknown1"):  # unknown: kept as sent
                body = {"session_id": sid, "task": "find the SATA drive"}
                assert (await http.post("/hwmode/request", json=body, headers=AUTH_HEADER)).status_code == 200
            return (await http.get("/hwmode/requests", headers=IOS_AUTH_HEADER)).json()
    assert asyncio.run(run()) == {"63a31002": "find the SATA drive", "unknown1": "find the SATA drive"}
