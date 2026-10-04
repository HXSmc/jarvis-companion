"""Sign-in handoff flow: same machine as flight, plus credential prefill.

DESIGN.md "Feature data flows" #3. The requesting Claude session POSTs
/handoff/signin {url, vault_key}, gets {id, link}, and sends the
jarvis://handoff/<id> link to the owner itself. When the app arrives over WS
(role=ios) and the handoff_open message reaches `handle_handoff_open`, the
gateway reuses `gateway.flows.flight`'s machine by import (the ok reply with
the URL, the navigate push, pending -> accepted -> driving, TTL expiry through
the same notify seam) and then adds the ONE difference: it reads the vault
section named by `vault_key` (same markdown shape as `common.config` — a
`## <vault_key>` heading with `key: value` lines), builds JS that fills the
login form, and pushes it to the ios role as a single evaluateJS command.
Credential values appear ONLY in that WS push — never in logs, HTTP response
bodies, or any other frame. Logs may name the vault_key and handoff ids, never
the values.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from gateway import registry
from gateway.auth import require_bearer
from gateway.flows import flight
from gateway.flows.flight import IOS_ROLE, LINK_PREFIX

logger = logging.getLogger(__name__)

# Secrets vault the credentials are read from at runtime; module-level so tests
# can point it at a fixture file (no config knob).
VAULT_PATH = Path("~/Desktop/ObsidianVault/Secrets/Projects.md").expanduser()

router = APIRouter()


class SigninHandoffBody(BaseModel):
    url: str
    vault_key: str


def _read_vault_section(vault_key: str) -> dict[str, str]:
    """Parse the `## <vault_key>` section's `key: value` lines (T0.1 markdown shape)."""
    section: dict[str, str] = {}
    in_section = False
    for line in VAULT_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip() == vault_key
            continue
        if in_section and stripped and not stripped.startswith("#") and ":" in stripped:
            key, _, value = stripped.partition(":")
            section[key.strip()] = value.strip()
    return section


def _prefill_js(username: str, password: str) -> str:
    """JS that fills the login form: set the field values, dispatch input events.

    Values are embedded as JSON string literals (valid JS strings) so no
    quoting/escaping can break out of the literal.
    """
    return (
        "(function() {"
        "function fill(el, value) {"
        "if (!el) { return; }"
        "el.focus();"
        "el.value = value;"
        "el.dispatchEvent(new Event('input', {bubbles: true}));"
        "el.dispatchEvent(new Event('change', {bubbles: true}));"
        "}"
        f"var user = {json.dumps(username)};"
        f"var pass = {json.dumps(password)};"
        "fill(document.querySelector("
        "'input[type=email], input[name=username], input[name=email]'), user);"
        "fill(document.querySelector('input[type=password]'), pass);"
        "})()"
    )


async def handle_handoff_open(message_id: str | None, handoff_id: str) -> dict | None:
    """Handle the app's {id, type: "handoff_open", payload: {id}} WS message (role=ios).

    Delegates to `flight.handle_handoff_open` for everything the two flows
    share — the {id, ok, data: {url}} reply, the navigate push, accepted then
    driving, and the unknown/expired ok:False reply — then pushes the
    credential prefill from the vault section named by the handoff's vault_key
    as one {id, type: "evaluateJS", payload: {js}} command to the ios role.
    Returns the flight reply data, or None for an unknown/expired handoff or a
    vault section missing username/password.
    """
    data = await flight.handle_handoff_open(message_id, handoff_id)
    if data is None:
        return None
    handoff = await flight.get_handoff(handoff_id)
    section = _read_vault_section(handoff["vault_key"])
    if not section.get("username") or not section.get("password"):
        logger.error(
            "signin: handoff %s: vault section %r has no username/password line",
            handoff_id, handoff["vault_key"],
        )
        return None
    js = _prefill_js(section["username"], section["password"])
    await registry.send_to_role(
        IOS_ROLE, {"id": message_id, "type": "evaluateJS", "payload": {"js": js}}
    )
    return data


@router.post("/handoff/signin", dependencies=[Depends(require_bearer)])
async def create_signin_handoff(body: SigninHandoffBody) -> dict:
    """Create a pending handoff; the caller sends the returned link to the owner itself."""
    handoff = flight.create_handoff(body.url)
    handoff["vault_key"] = body.vault_key
    handoff["kind"] = "signin"  # gateway.ws dispatches handoff_open on this marker
    return {"id": handoff["id"], "link": LINK_PREFIX + handoff["id"]}
