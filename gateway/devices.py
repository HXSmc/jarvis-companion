"""SSH device actions: POST /device/action — a fixed allowlist, argv-list ssh exec.

Security model (voice input is untrusted): a caller — the overlay intent
parser or any model — may only NAME an action id from the ACTIONS table.
The ssh argv is built solely from that table's row; no utterance or model
text is ever interpolated into a command. Unknown ids are 404. Destructive
actions (shutdown/reboot) are deliberately absent from the table; adding one
requires a code change with review, not config.

Every call runs `ssh` via asyncio.create_subprocess_exec (no shell) with
ConnectTimeout, BatchMode (nothing may prompt) and an overall wait_for.
`_exec` is the seam tests stub — no network in the suite. Aliases come from
the caller's ~/.ssh/config (existing keys), same as `ssh winpc` at a terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from gateway.auth import require_bearer_any

logger = logging.getLogger(__name__)

router = APIRouter()

CONNECT_TIMEOUT_S = 8
RUN_TIMEOUT_S = 30

ACTIONS: dict[str, dict[str, str]] = {
    "winpc.gaming_mode_on": {
        "alias": "winpc",
        "command": r"C:\Users\i0562\llm-switch\bin\game-mode.cmd on",
        "label": "gaming mode on",
    },
    "winpc.gaming_mode_off": {
        "alias": "winpc",
        "command": r"C:\Users\i0562\llm-switch\bin\game-mode.cmd off",
        "label": "gaming mode off",
    },
    "winpc.gaming_mode_status": {
        "alias": "winpc",
        "command": r"C:\Users\i0562\llm-switch\bin\game-mode.cmd status",
        "label": "gaming mode status",
    },
}

# Tests stub this (no network); returns an asyncio subprocess-ish object.
_exec = asyncio.create_subprocess_exec


async def run_action(action_id: str) -> dict:
    """Run one allowlisted action over ssh; the only entry the overlay uses too.

    Returns {"id", "ok", "output"}. Raises HTTPException: 404 for any id not
    in ACTIONS (the non-allowlist rejection), 504 on timeout, 502 when ssh
    cannot start. Output is ssh's merged stdout/stderr, trimmed to 2000 chars.
    """
    entry = ACTIONS.get(action_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown device action: {action_id}")
    argv = [
        "ssh",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",
        entry["alias"],
        entry["command"],
    ]
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.wait_for(
            _exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT),
            timeout=RUN_TIMEOUT_S,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):  # spawn-timeout: proc is None
            if proc is not None:                       # run-timeout: kill the hung ssh
                proc.kill()
        raise HTTPException(status_code=504, detail=f"device action timed out: {action_id}")
    except OSError:
        logger.warning("devices: ssh failed to start for %s", action_id, exc_info=True)
        raise HTTPException(status_code=502, detail="ssh failed to start")
    output = (out or b"").decode(errors="replace").strip()
    return {"id": action_id, "ok": proc.returncode == 0, "output": output[:2000]}


class ActionBody(BaseModel):
    id: str


@router.post("/device/action", dependencies=[Depends(require_bearer_any("token", "ios_token"))])
async def device_action(body: ActionBody) -> dict:
    """Run an allowlisted device action by id; nothing else shapes the command."""
    return await run_action(body.id)
