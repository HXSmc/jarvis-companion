"""Gateway FastAPI app: loopback-only bind, bearer auth, /health, router seam."""

from __future__ import annotations

import os
import sys

from fastapi import Depends, FastAPI

from common import config
from gateway.auth import require_bearer

HOST = "127.0.0.1"

# --- Security: bind only to loopback. Assert at startup (agentic-os server/index.ts pattern).
_bind_host = os.environ.get("HOST", HOST)
if _bind_host != HOST:
    print(f"[gateway] Refusing to bind non-loopback host: {_bind_host}", file=sys.stderr)
    raise SystemExit(1)

app = FastAPI()


@app.get("/health", dependencies=[Depends(require_bearer)])
async def health() -> dict[str, bool]:
    return {"ok": True}


# --- Router seam: later tasks register by dropping in gateway/ws.py, gateway/siri.py,
# gateway/flows/flight.py, gateway/flows/signin.py. Guarded imports so their absence
# (they don't exist yet) never crashes startup.
for _module in ("gateway.ws", "gateway.siri", "gateway.flows.flight", "gateway.flows.signin"):
    try:
        _mod = __import__(_module, fromlist=["router"])
    except ImportError:
        continue
    app.include_router(_mod.router)

# agents.py must always be present (gateway.ws imports it for reply routing) —
# direct import, not the guarded seam above.
from gateway import agents  # noqa: E402

app.include_router(agents.router)


if __name__ == "__main__":
    # LOW-4: the HOST env guard above can't see `uvicorn --host 0.0.0.0`, so
    # `python -m gateway.app` pins the bind here — the launchd plist also pins
    # `--host 127.0.0.1`, loopback no matter which path starts the gateway.
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=config.PORT)
