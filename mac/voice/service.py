"""Standalone overlay service: `python -m mac.voice.service` on the Mac.

Serves only the /overlay router, on loopback, port OVERLAY_PORT (8791). The
lifespan opens the port first; the voice, the ack clips and the warm claude
sessions get ready in background threads after it (jfollow11b: they took the
37-45 s a restart used to refuse connections for). It starts the wake-word
thread unless OVERLAY_WAKEWORD=0. Every
optional piece (voice, wake word) degrades to a warning, never a crash: the
overlay must always come up.
"""

from __future__ import annotations

import time

_T0 = time.monotonic()  # the start-up phase clock: ms since this module began to import

import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from pathlib import Path

from fastapi import FastAPI  # noqa: E402

from gateway.overlay import debug_log, get_deps, overlay_token, router  # noqa: E402

logger = logging.getLogger(__name__)
_IMPORTS_MS = round((time.monotonic() - _T0) * 1000)
_phases: dict[str, int] = {}


def _phase(name: str, ms: int | None = None) -> None:
    """One `startup phase=<name> ms=<n>` line (fixed words and numbers only)."""
    _phases[name] = round((time.monotonic() - _T0) * 1000) if ms is None else ms
    logger.info("startup phase=%s ms=%d", name, _phases[name])

HOST = "127.0.0.1"

# The installer writes these to the repo's gitignored .env; launchd gives the service only PATH.
# Only these keys load: the agent's claude child inherits os.environ, so other .env keys stay out.
SETUP_KEYS = ("OVERLAY_VAULT", "OVERLAY_COMPANION_URL")
ENV_FILE = Path(os.environ.get("OVERLAY_ENV_FILE", str(Path(__file__).resolve().parents[2] / ".env")))


def load_setup_env(path: Path = ENV_FILE) -> list[str]:
    """Copy SETUP_KEYS from `path` into os.environ (the real env wins); returns the keys set."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    loaded = []
    for line in lines:
        key, sep, value = line.strip().partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in SETUP_KEYS or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value:
            os.environ[key] = value
            loaded.append(key)
    return loaded


load_setup_env()


def _start_wake_thread() -> None:
    try:
        from mac.voice import wakeword
    except Exception as exc:  # openwakeword not installed on this box
        logger.warning("wake word unavailable (%s), overlay continues without it",
                       type(exc).__name__)
        debug_log.listener(state=f"unavailable ({type(exc).__name__})")
        return

    def run() -> None:
        debug_log.listener(state="listening", mic=wakeword.MIC)
        try:
            wakeword.run_live(on_stats=lambda score, level: debug_log.listener(
                peak_score_10s=score, peak_level_10s=level))
            debug_log.listener(state="stopped")
        except BaseException as exc:  # mic/model issues (SystemExit too) must not take the service down
            logger.warning("wake word thread exited (%s: %s)", type(exc).__name__, exc)
            debug_log.listener(state=f"stopped ({type(exc).__name__}: {exc})")

    threading.Thread(target=run, name="wakeword", daemon=True).start()


def _warm_up(deps) -> None:
    """After the port opened: render the ack clips (waits on the voice load), then start one spare
    claude per pool. A clip asked for before it is ready is synthesized on demand by the turn."""
    try:
        deps.acks.prerender()
    except Exception as exc:  # warm-up must never take the overlay down
        logger.warning("ack prerender failed (%s), continuing", type(exc).__name__)
    _phase("acks_ready")
    for pool_name in ("conversations", "quiet_conversations", "agent_conversations"):
        try:
            getattr(deps, pool_name).prewarm()  # first answer skips the CLI cold start
        except Exception as exc:
            logger.warning("%s prewarm failed (%s), continuing", pool_name, type(exc).__name__)
    _phase("prewarm_done")
    debug_log.alone("service_start", " ".join(f"{k}={v}" for k, v in _phases.items()))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _phases.clear()
    _phase("imports", _IMPORTS_MS)
    deps = get_deps()  # cheap now: the voice loads in its own thread
    _phase("deps")
    # Create the overlay token before serving: the app reads this file before its first request.
    overlay_token()
    _phase("token")
    if os.environ.get("OVERLAY_WAKEWORD", "1") != "0":
        _start_wake_thread()
        _phase("wake_started")
    _phase("health_up")  # uvicorn opens the port as soon as this returns
    threading.Thread(target=_warm_up, args=(deps,), name="warm-up", daemon=True).start()
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
