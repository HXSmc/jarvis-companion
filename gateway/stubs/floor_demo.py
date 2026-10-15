"""Agency Floor demo: the real gateway app fed by canned router + agentic-os data.

Run: `.venv/bin/python -m gateway.stubs.floor_demo [port]` (default 8899; the fake router takes port+1), then open
http://127.0.0.1:<port>/central/floor with `window.__JARVIS = {token: "demo-ios-token"}`
injected (the screenshot script does this, like the iOS WKUserScript). Loopback only;
the demo tokens are fake and the vault is never read.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from common import config
from gateway import central

DEMO_IOS_TOKEN = "demo-ios-token"

SESSIONS = [
    {"session_id": "s-auth", "kind": "claude", "title": "Fix login bug", "state": "running", "project": "notekit",
     "task": "Fix login redirect loop", "summary": "Patching the OAuth callback", "phase": "in_progress",
     "updated_at": "2026-10-15T10:00:00Z"},
    {"session_id": "s-rev", "kind": "claude", "title": "Review billing PR", "state": "needs_input", "project": "notekit",
     "task": "Review #212", "summary": "2 findings, waiting on owner", "phase": "in_review",
     "updated_at": "2026-10-15T09:40:00Z"},
    {"session_id": "s-res", "kind": "claude", "title": "Research PDPL consent", "state": "running", "project": "<project>",
     "task": "Research consent rules", "summary": "Reading the law pack", "phase": "in_progress",
     "updated_at": "2026-10-15T09:55:00Z"},
    {"session_id": "s-ui", "kind": "glm", "title": "Landing page layout", "state": "running", "project": "<name>",
     "task": "Build hero section", "summary": "Spoke writing components", "phase": "in_progress",
     "updated_at": "2026-10-15T09:50:00Z"},
    {"session_id": "s-old", "kind": "claude", "title": "Notes cleanup", "state": "done", "project": "vault",
     "task": "Tidy daily notes", "summary": "Done", "phase": "done", "updated_at": "2026-10-15T08:00:00Z"},
]

SNAPSHOT = {"sessions": {"device": "mac-studio", "hubs": [
    {"key": "mac:1", "dirLabel": "jarvis-companion", "engine": "claude", "active": True, "lastTs": 1760520000000,
     "agents": [
         {"id": "a1", "persona": "builder", "engine": "glm", "label": "gateway routes", "active": True, "status": "running"},
         {"id": "a2", "persona": "reviewer", "engine": "claude", "label": "code-reviewer", "active": True, "status": "running"},
         {"id": "a3", "persona": "researcher", "engine": "agy", "label": "docs lookup", "active": False, "status": "done"},
     ]},
    {"key": "mac:2", "dirLabel": "agentic-os", "engine": "claude", "active": False, "lastTs": 1760510000000,
     "agents": [{"id": "b1", "persona": "verifier", "engine": "claude", "label": "verifier", "active": False, "status": "done"}]},
]}}

FEED = [
    {"session_id": "s-auth", "type": "spawn", "from": "app", "to": "claude", "text": "Fix login bug"},
    {"session_id": "s-res", "type": "status", "from": "s-res", "to": "brain", "text": "found 3 articles"},
    {"session_id": "s-rev", "type": "text", "from": "s-auth", "to": "s-rev", "text": "patch ready for review"},
    {"session_id": "s-ui", "type": "status", "from": "s-ui", "to": "brain", "text": "hero done"},
    {"session_id": "s-old", "type": "done", "from": "s-old", "to": "app", "text": "notes tidy"},
    {"session_id": "s-res", "type": "text", "from": "s-res", "to": "s-ui", "text": "consent copy"},
]

router = FastAPI()
agentic = FastAPI()


@router.get("/sessions")
async def sessions():
    return JSONResponse(SESSIONS)


@router.get("/feed")
async def feed():
    async def gen():
        seq = 0
        while True:
            for e in FEED:
                seq += 1
                yield f"id: {seq}\ndata: {json.dumps({**e, 'seq': seq, 'ts': '2026-10-15T10:00:00Z'})}\n\n"
                await asyncio.sleep(0.7)

    return StreamingResponse(gen(), media_type="text/event-stream")


@agentic.get("/api/snapshot")
async def snapshot():
    return JSONResponse(SNAPSHOT)


def install(router_port: int) -> None:
    tokens = {"ios_token": DEMO_IOS_TOKEN, central.ROUTER_TOKEN_NAME: "demo-router"}
    config.get_token = lambda path=None, name="token": tokens.get(name)
    # Real loopback server: httpx.ASGITransport buffers whole bodies, so the endless feed needs a socket.
    central.router_base = lambda: f"http://127.0.0.1:{router_port}"
    central.agentic_os_token = lambda: "demo-agentic"
    central._agentic_client = lambda timeout: httpx.AsyncClient(transport=httpx.ASGITransport(app=agentic), timeout=timeout)


async def main(port: int) -> None:
    import uvicorn

    install(port + 1)
    from gateway.app import app

    servers = [
        uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=p, log_level="warning"))
        for a, p in ((app, port), (router, port + 1))
    ]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8899))
