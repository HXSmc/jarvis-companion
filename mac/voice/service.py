"""Standalone overlay service: `python -m mac.voice.service` on the Mac.

Serves only the /overlay router, on loopback, port OVERLAY_PORT (8791). The
lifespan prerenders the ack phrases in a thread so the first turn is not the
slow one, and starts the wake-word thread unless OVERLAY_WAKEWORD=0. Every
optional piece (voice, wake word) degrades to a warning, never a crash: the
overlay must always come up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gateway.overlay import get_deps, router

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"


def _start_wake_thread() -> None:
    try:
        from mac.voice import wakeword
    except Exception as exc:  # openwakeword not installed on this box
        logger.warning("wake word unavailable (%s), overlay continues without it",
                       type(exc).__name__)
        return

    def run() -> None:
        try:
            wakeword.run_live()
        except Exception as exc:  # mic/model issues must not take the service down
            logger.warning("wake word thread exited (%s)", type(exc).__name__)

    threading.Thread(target=run, name="wakeword", daemon=True).start()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await asyncio.to_thread(get_deps().acks.prerender)
    if os.environ.get("OVERLAY_WAKEWORD", "1") != "0":
        _start_wake_thread()
    yield


app = FastAPI(lifespan=_lifespan)
app.include_router(router)


def main() -> None:
    bind = os.environ.get("HOST", HOST)
    if bind != HOST:  # the overlay is Mac-local by design; refuse to expose it
        print(f"[overlay] Refusing to bind non-loopback host: {bind}", file=sys.stderr)
        raise SystemExit(1)
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host=HOST, port=int(os.environ.get("OVERLAY_PORT", "8791")))


if __name__ == "__main__":
    main()
