"""Gateway FastAPI app: loopback-only bind, bearer auth, /health, routers."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from common import config
from gateway.auth import IOS_TOKEN_SHARED_DETAIL, ios_token_is_shared, require_bearer

HOST = "127.0.0.1"

# --- Security: bind only to loopback. Assert at startup (agentic-os server/index.ts pattern).
_bind_host = os.environ.get("HOST", HOST)
if _bind_host != HOST:
    print(f"[gateway] Refusing to bind non-loopback host: {_bind_host}", file=sys.stderr)
    raise SystemExit(1)

logger = logging.getLogger(__name__)


def warn_if_ios_token_shared() -> bool:
    """Startup check: ios_token == shared token gets a loud warning (True).

    The ios-only routes refuse it per request anyway (503, see gateway.auth);
    this makes the misconfiguration visible at boot, not on the first phone call.
    """
    if not ios_token_is_shared():
        return False
    message = f"[gateway] SECURITY: {IOS_TOKEN_SHARED_DETAIL}; ios-only routes refuse every request until the vault is fixed"
    logger.critical(message)
    print(message, file=sys.stderr)
    return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    warn_if_ios_token_shared()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health", dependencies=[Depends(require_bearer)])
async def health() -> dict[str, bool]:
    return {"ok": True}


from gateway import agents, cards, central, hwmode, inbox, quick, screen, vnc, wake
from gateway.flows import flight, signin
from gateway.siri import router as siri_router
from gateway.ws import router as ws_router

app.include_router(ws_router)
app.include_router(siri_router)
app.include_router(flight.router)
app.include_router(signin.router)
app.include_router(agents.router)
app.include_router(quick.router, prefix="")
app.include_router(wake.router, prefix="")
app.include_router(inbox.router, prefix="")
app.include_router(central.router)  # /chat + /central/* -> mini PC router (ios_token only)
app.include_router(cards.session_router)  # /cards/*: jarvis-card from box sessions (shared token)
app.include_router(hwmode.router)  # /hwmode/ws: phone camera frames -> lean Claude brain (ios_token only)
app.include_router(central.public_router)  # /central/floor page only: no data, no auth
# SCREEN_CONTROL=0 unmounts /screen/* (kill switch); default on.
if os.environ.get("SCREEN_CONTROL", "1") != "0":
    app.include_router(vnc.router)  # Mini PC VNC routes (/screen/minipc/novnc/*, /screen/minipc/ws); same SCREEN_CONTROL kill switch as screen.router.
    app.include_router(screen.router)


if __name__ == "__main__":
    # LOW-4: the HOST env guard above can't see `uvicorn --host 0.0.0.0`, so
    # `python -m gateway.app` pins the bind here — the launchd plist also pins
    # `--host 127.0.0.1`, loopback no matter which path starts the gateway.
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=config.PORT)
