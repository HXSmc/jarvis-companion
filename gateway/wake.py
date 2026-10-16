"""Wake-on-LAN for the PC: POST /pc/wake — magic packet out, wait for the agent.

MAC sources, in order (spec Q2 §13): the vault's `pc_mac` key, else the last
MAC a Windows agent reported over its websocket (persisted by `remember_mac`
into ~/.jarvis/windows_mac.json). Neither → HTTP 400 with the setup hint.

The magic packet goes to the global broadcast 255.255.255.255:9 plus any
directed broadcast addresses an ifconfig parse yields (best-effort), plus a
unicast to the vault's `pc_ip` (same section/mechanism as `pc_mac`, no
default — W5 proved unicast to the PC's LAN IP is the variant that wakes it;
absent `pc_ip` → broadcast-only, logged). Sending is a module-level seam (`_send_packet`)
so tests never put real packets on the wire; the wait loop polls the registry
every 2 s through `asyncio.sleep` (`_sleep` seam), never blocking the loop.

`handle_hello` is the `_HANDLERS["hello"]` body for gateway/ws.py — it lives
here so that file's edit stays a two-line wiring change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse

from common import config
from gateway import agents, registry
from gateway.auth import require_bearer_any
from mac.voice import router as voice_router

logger = logging.getLogger(__name__)

router = APIRouter()

STATE_PATH = Path.home() / ".jarvis" / "windows_mac.json"

WAIT_TIMEOUT_S = 90.0
POLL_INTERVAL_S = 2.0
WOL_PORT = 9

NO_MAC_DETAIL = (
    "no PC MAC configured: set pc_mac in the vault or power the PC on once "
    "so the agent registers"
)

# Clock/sleep seams — tests drive the 90 s wait with a fake clock.
_now = time.monotonic
_sleep = asyncio.sleep


def _normalise_mac(mac: str) -> str | None:
    """`aa:bb:cc:dd:ee:ff` lowercase-colon form, or None when malformed.

    Accepts colon/dash/dot-separated and bare 12-hex forms, any case.
    """
    digits = re.sub(r"[\s:\-.]", "", mac.strip().lower())
    if len(digits) != 12 or any(c not in "0123456789abcdef" for c in digits):
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def is_plausible_mac(mac: str) -> bool:
    """Loose gate for the ws hello handler: True when `mac` parses at all."""
    return _normalise_mac(mac) is not None


def magic_packet(mac: str) -> bytes:
    """The classic 102-byte WoL frame: 6×0xFF then 16× the MAC bytes."""
    normalised = _normalise_mac(mac)
    if normalised is None:
        raise ValueError(f"malformed MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(normalised.replace(":", "")) * 16


def _directed_broadcasts() -> list[str]:
    """Best-effort ifconfig parse for per-interface broadcast addresses."""
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return re.findall(r"\bbroadcast\s+(\d{1,3}(?:\.\d{1,3}){3})", out)


def _send_packet(packet: bytes, targets: list[tuple[str, int]]) -> int:
    """UDP blast of `packet` to every target; returns the successful-send count.

    Best-effort PER TARGET: one target raising must never stop the others
    from being sent. Found live 2026-10-16 — a unicast send to the PC's last
    known IP while it is fully powered off (not just asleep, no ARP entry)
    raises OSError(64, "Host is down") on macOS but not on Linux (the box),
    which was making the whole batch look like it never went out at all on
    the Mac even though the broadcast targets (which are what actually wake
    a sleeping PC) had already gone out fine. wake_pc maps count > 0 onto
    the reply's `sent` flag.
    """
    sent = 0
    for addr in targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(packet, addr)
            sent += 1
        except OSError:
            logger.warning("wake: send to %s failed", addr, exc_info=True)
    return sent


def remember_mac(mac: str) -> None:
    """Persist the agent-reported MAC (normalised) to the state file, creating the dir.

    Private by default: dir 0o700 when created, file 0o600 on create and on
    every rewrite (chmod tightens files written by older, umask-permissive
    versions).
    """
    normalised = _normalise_mac(mac)
    if normalised is None:
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"mac": normalised}))
    os.chmod(STATE_PATH, 0o600)


def last_seen_mac() -> str | None:
    """The persisted MAC, or None when the file is missing, corrupt, or unparseable."""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mac = data.get("mac") if isinstance(data, dict) else None
    return _normalise_mac(mac) if isinstance(mac, str) else None


def _vault_mac() -> str | None:
    """`pc_mac` from the vault section (normalised), or None when absent/unparseable."""
    section = config.read_section(str(Path(config.DEFAULT_VAULT_PATH).expanduser())) or {}
    return _normalise_mac(section.get("pc_mac") or "")


def _vault_ip() -> str | None:
    """`pc_ip` from the vault section, or None when absent/not a dotted quad."""
    section = config.read_section(str(Path(config.DEFAULT_VAULT_PATH).expanduser())) or {}
    ip = (section.get("pc_ip") or "").strip()
    return ip if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", ip) else None


def _resolve_mac() -> str | None:
    """Vault `pc_mac` first, else the last agent-reported MAC."""
    return _vault_mac() or last_seen_mac()


@router.post("/pc/wake", dependencies=[Depends(require_bearer_any("token", "ios_token"))])
async def wake_pc() -> dict:
    """Send the magic packet, then wait ≤90 s (2 s polls) for the windows agent.

    Returns {"sent", "online", "waited_ms", "mac"}; 400 with the setup hint
    when no MAC is configured anywhere.
    """
    mac = _resolve_mac()
    if mac is None:
        raise HTTPException(status_code=400, detail=NO_MAC_DETAIL)
    packet = magic_packet(mac)
    targets = [("255.255.255.255", WOL_PORT)] + [(b, WOL_PORT) for b in _directed_broadcasts()]
    pc_ip = _vault_ip()
    if pc_ip:
        targets.append((pc_ip, WOL_PORT))
    else:
        logger.info("wake: no pc_ip in vault — skipping unicast send")
    sent = _send_packet(packet, targets) > 0
    start = _now()
    online = False
    while True:
        if "windows" in registry.roles():
            online = True
            break
        if _now() - start >= WAIT_TIMEOUT_S:
            break
        await _sleep(POLL_INTERVAL_S)
    return {
        "sent": sent,
        "online": online,
        "waited_ms": int((_now() - start) * 1000),
        "mac": mac,
    }


@router.post("/pc/sleep", dependencies=[Depends(require_bearer_any("token", "ios_token"))])
async def sleep_pc() -> Response:
    """Forward the `sleep` command to the connected windows agent (S3 suspend).

    Mirror of /pc/wake's shape: {"sent", "ok", "data"} on success; HTTP 503
    {"sent": false, "error": "pc offline"} when no agent is connected (or the
    connection drops mid-send). The agent acks BEFORE it suspends, so the
    roundtrip reply arrives while the PC is still awake.
    """
    offline = JSONResponse(status_code=503, content={"sent": False, "error": "pc offline"})
    if agents.WINDOWS_ROLE not in registry.roles():
        return offline
    try:
        reply = await agents.agent_roundtrip(voice_router.request_envelope("sleep"))
    except HTTPException as exc:
        if exc.status_code == 503:  # roles() said online, but the send failed
            return offline
        raise
    return {"sent": True, "ok": bool(reply.get("ok")), "data": reply.get("data")}


async def handle_hello(message: dict, role: str) -> dict:
    """`_HANDLERS["hello"]` body: remember a windows agent's reported MAC, then
    reply exactly the generic unknown-type ack envelope.

    A persistence failure is logged, never raised — it must not drop the socket.
    """
    payload = message.get("payload")
    mac = payload.get("mac") if isinstance(payload, dict) else None
    if role == "windows" and isinstance(mac, str) and is_plausible_mac(mac):
        try:
            remember_mac(mac)
        except Exception:
            logger.exception("wake: persisting hello MAC failed")
    return {"id": message.get("id"), "ok": True, "data": message.get("payload")}
