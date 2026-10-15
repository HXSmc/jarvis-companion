"""Canned mini PC router for local dev and tests (contract: framing doc §2 + §6).

Run: `uvicorn gateway.stubs.router_stub:app --port 8800` and point the gateway at
it with MINIPC_ROUTER_URL=http://127.0.0.1:8800. No auth check — it records the
headers it saw (`seen`) so tests can assert what the gateway forwarded. Delete
when the real router is up. GET /feed is NOT in the framing contract yet; it is
the shape the gateway's /central/feed expects the router to add.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()

# Each request: {"method", "path", "headers", "body"}; tests clear it.
seen: list[dict] = []
# Set to an int to make every route answer that status with a junk body.
fail_status: int | None = None
FAIL_BODY = "upstream internal detail that must never reach the phone"

SESSIONS = [
    {"session_id": "s-run", "kind": "claude", "title": "Fix login", "state": "running",
     "cwd": "/home/<user>/work/app", "updated_at": "2026-10-15T10:00:00Z",
     "project": "app", "task": "Fix login bug", "summary": "Patching auth", "phase": "in_progress"},
    {"session_id": "s-ask", "kind": "claude", "title": "Deploy", "state": "needs_input",
     "cwd": "/home/<user>/work/site", "updated_at": "2026-10-15T09:00:00Z", "internal": "x"},
    {"session_id": "s-done", "kind": "glm", "title": "Notes", "state": "done",
     "cwd": "/home/<user>/notes", "updated_at": "2026-10-15T08:00:00Z"},
]


def sse_body(session_id: str, start: int = 0) -> str:
    events = [
        {"type": "text", "text": "working", "seq": start + 1},
        {"type": "done", "text": "", "seq": start + 2},
    ]
    return "".join(
        f"id: {e['seq']}\ndata: {json.dumps({**e, 'session_id': session_id})}\n\n" for e in events
    )


async def _record(request: Request) -> Response | None:
    raw = await request.body()
    seen.append({
        "method": request.method, "path": request.url.path,
        "headers": dict(request.headers), "body": json.loads(raw) if raw else None,
    })
    if fail_status is not None:
        return Response(FAIL_BODY, status_code=fail_status)
    return None


@app.post("/route")
async def route(request: Request):
    if (err := await _record(request)) is not None:
        return err
    body = seen[-1]["body"]
    if body["text"].startswith("tell me"):
        return {"action": "answered", "session_id": None, "kind": None, "title": "Q", "reply": "42"}
    if body["text"].startswith("ask me"):
        return {"action": "needs_input", "session_id": "s-ask", "kind": "claude", "title": "Deploy",
                "reply": None, "prompt": "Which env?", "options": ["staging", "prod"]}
    return {"action": "spawned", "session_id": "s-new", "kind": "claude", "title": "New task",
            "reply": "Started", "seq": 3, "internal_debug": "dropped by gateway"}


@app.get("/sessions")
async def sessions(request: Request):
    if (err := await _record(request)) is not None:
        return err
    return JSONResponse(SESSIONS)


@app.post("/sessions/{sid}/message")
async def message(sid: str, request: Request):
    if (err := await _record(request)) is not None:
        return err
    return {"ok": True, "action": "delivered"}


@app.post("/sessions/{sid}/stop")
async def stop(sid: str, request: Request):
    if (err := await _record(request)) is not None:
        return err
    return {"ok": True}


@app.get("/sessions/{sid}/events")
async def events(sid: str, request: Request):
    if (err := await _record(request)) is not None:
        return err
    last = request.headers.get("last-event-id")
    return Response(sse_body(sid, int(last) if last else 0), media_type="text/event-stream")


@app.get("/feed")
async def feed(request: Request):
    if (err := await _record(request)) is not None:
        return err
    line = {"seq": 1, "ts": "2026-10-15T10:00:00Z", "session_id": "s-run", "type": "spawn",
            "from": "app", "to": "claude", "text": "Fix login"}
    return Response(f"id: 1\ndata: {json.dumps(line)}\n\n", media_type="text/event-stream")
