"""Deterministic intent router: transcript -> allowlisted windows-agent command + spoken reply.

No LLM call: keyword intents map straight onto the T3.1 allowlist (`specs`,
`uptime`, `disk`, `processes`); a transcript that matches nothing routes to None,
which hands the turn to `mac.voice.brain`'s claude -p fallback (not this module).
Transports are injected: `speak(transcript, ask=..., say=...)` takes the
gateway-side call and the TTS-side call as required keyword callables, so the
e2e test fakes both edges. Wiring the live transports is unowned glue.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

OFFLINE_REPLY = "the PC is off"

# Checked in order; first keyword hit wins. `specs` leads so RAM/memory/CPU
# questions about the box itself never fall through to a later intent.
_INTENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("specs", ("ram", "memory", "cpu", "spec")),
    ("uptime", ("uptime", "how long", "up for", "been on", "been running")),
    ("disk", ("disk", "storage")),
    ("processes", ("process", "running")),
)


def route(transcript: str) -> str | None:
    """Keyword intents -> allowlisted command name; None = no match (brain fallback).

    The screen intents ("screen_mac"/"screen_windows"/"screen_minipc") never reach the agent
    path — gateway/siri.py intercepts them and launches the control page (SC7);
    they're checked before _INTENTS so no existing keyword can shadow them.
    """
    text = transcript.lower()
    # Mini PC first: its "pc" would otherwise land on screen_windows.
    if ("mini pc" in text or "minipc" in text) and ("screen" in text or "show" in text):
        return "screen_minipc"
    if "screen" in text:
        if "mac" in text:
            return "screen_mac"
        if any(word in text for word in ("pc", "windows", "computer")):
            return "screen_windows"
    for cmd, keywords in _INTENTS:
        if any(keyword in text for keyword in keywords):
            return cmd
    return None


def request_envelope(cmd: str, args: dict | None = None) -> dict:
    """The wire shape sent to the windows agent, with a fresh unique id."""
    payload = {"cmd": cmd}
    if args is not None:
        payload["args"] = args
    return {"id": uuid.uuid4().hex, "type": "command", "payload": payload}


def _ram_value(data: dict) -> str | None:
    """First RAM-ish value in the specs dict ("ram", "ram_total_gb", ...), speakable."""
    for key, value in data.items():
        if "ram" in key.lower():
            if key.lower().endswith("_gb"):
                return f"{value} gigabytes"
            return str(value)
    return None


def format_reply(cmd: str, data: dict) -> str:
    """Agent reply data -> one speakable sentence (specs must carry the RAM value)."""
    if cmd == "specs":
        ram = _ram_value(data) if isinstance(data, dict) else None
        if ram is not None:
            return f"the PC has {ram} of RAM"
        return "I couldn't read the PC's specs"
    if isinstance(data, dict):
        if cmd == "uptime" and "uptime_sec" in data:
            return f"the PC has been up for {data['uptime_sec']} seconds"
        if cmd == "disk" and "free_gb" in data:
            return f"the PC's disk has {data['free_gb']} of {data.get('total_gb', '?')} gigabytes free"
        if cmd == "processes" and "count" in data:
            return f"{data['count']} processes are running on the PC"
    return f"{cmd}: {data}"


async def speak(
    transcript: str,
    *,
    ask: Callable[[str], Awaitable[dict]],
    say: Callable[[str], Awaitable[None]],
) -> None:
    """Route, ask the agent, speak the reply. Unmatched transcript: no calls at all.

    A ConnectionError from `ask` (agent offline, DESIGN "Windows PC off" row) is
    spoken as OFFLINE_REPLY instead of raising.
    """
    cmd = route(transcript)
    if cmd is None:
        return
    try:
        data = await ask(cmd)
    except ConnectionError:
        await say(OFFLINE_REPLY)
        return
    await say(format_reply(cmd, data))
