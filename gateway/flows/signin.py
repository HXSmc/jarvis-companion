"""Sign-in handoff flow: same machine as flight, plus origin-bound credential prefill.

DESIGN.md "Feature data flows" #3. The requesting Claude session POSTs
/handoff/signin {url, vault_key}, gets {id, link}, and sends the
jarvis://handoff/<id> link to the owner itself. The request is validated at
POST time (HIGH-2): only vault sections headed `## Signin/<vault_key>` that
carry `url:`, `username:`, `password:` lines are eligible, `vault_key` must
equal the text after `Signin/` exactly, the handoff `url` must be `https://`,
and its host must be the section's `url:` host or a subdomain of it. Any
violation is an HTTP 400 with a GENERIC detail — the reply never reveals
whether a section exists.

When the app arrives over WS (role=ios) and the handoff_open message reaches
`handle_handoff_open`, the gateway reuses `gateway.flows.flight`'s machine
by import (the ok reply with the URL, the navigate push, pending -> accepted
-> driving, TTL expiry through the same notify seam) and then adds the ONE
difference: it reads the `## Signin/<vault_key>` vault section (same markdown
shape as `common.config`), builds JS that fills the login form, and pushes it
to the ios role as a single evaluateJS command whose payload also carries
`expected_host` (HIGH-3). The JS starts with a `location.host` guard so it
runs only on the intended origin, and the app additionally holds it until the
navigation to that host finishes. Credential values appear ONLY in that WS
push — never in logs, HTTP response bodies, or any other frame. Logs may name
the vault_key and handoff ids, never the values.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from common import config
from gateway import registry
from gateway.auth import require_bearer
from gateway.flows import flight
from gateway.flows.flight import IOS_ROLE, LINK_PREFIX

logger = logging.getLogger(__name__)

# Secrets vault the credentials are read from at runtime; module-level so tests
# can point it at a fixture file (no config knob).
VAULT_PATH = Path(config.DEFAULT_VAULT_PATH).expanduser()

router = APIRouter()

SIGNIN_PREFIX = "Signin/"
GENERIC_400_DETAIL = "invalid signin handoff request"


class SigninHandoffBody(BaseModel):
    url: str
    vault_key: str


def _read_vault_section(vault_key: str) -> dict[str, str]:
    """Parse the `## Signin/<vault_key>` section's `key: value` lines (T0.1 markdown shape).

    Only `## Signin/...` headings are eligible (HIGH-2); `vault_key` must equal
    the text after `Signin/` exactly. Returns {} when no such section exists.
    An UNREADABLE vault raises OSError instead — the request fails loudly (500
    on the HTTP path, logged-and-no-reply on the WS path), never as a silent
    "no eligible section". `read_section` has already logged the path by then;
    the error text carries the path only, never credential values.
    """
    section = config.read_section(VAULT_PATH, SIGNIN_PREFIX + vault_key)
    if section is None:  # unreadable vault must not read as "no eligible section"
        raise OSError(f"vault file not readable: {VAULT_PATH}")
    return section


def _url_host(url: str) -> str | None:
    """The URL's lowercased hostname, or None when unparseable/absent."""
    try:
        return (urlparse(url).hostname or "").lower() or None
    except ValueError:
        return None


def _validate_signin_request(url: str, vault_key: str) -> str:
    """Enforce HIGH-2: eligible section + exact vault_key + https + host binding.

    Returns the section's allowed host (the section `url:`'s hostname) on
    success. Raises HTTPException 400 with a generic detail on ANY violation —
    never revealing whether the section exists.
    """
    def reject() -> "HTTPException":
        return HTTPException(status_code=400, detail=GENERIC_400_DETAIL)

    target = _url_host(url)
    if not url.startswith("https://") or target is None:
        raise reject()
    if not vault_key or vault_key.startswith("/") or vault_key != vault_key.strip():
        raise reject()
    section = _read_vault_section(vault_key)
    if not section.get("url") or not section.get("username") or not section.get("password"):
        raise reject()
    allowed = _url_host(section["url"])
    if allowed is None:
        raise reject()
    if target != allowed and not target.endswith("." + allowed):
        raise reject()
    return allowed


def _prefill_js(username: str, password: str, expected_host: str) -> str:
    """JS that fills the login form on the expected origin only.

    The FIRST statement is the HIGH-3 origin guard: unless the document's
    location.host equals the handoff URL's host, the script returns without
    touching the page. Values are embedded as JSON string literals (valid JS
    strings) so no quoting/escaping can break out of the literal.
    """
    return (
        "(function() {"
        f"if (location.host !== {json.dumps(expected_host)}) {{ return; }}"
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
    driving, single-use, and the unknown/expired/used ok:False replies — then
    pushes the credential prefill from the `## Signin/<vault_key>` vault
    section as one {id, type: "evaluateJS", payload: {js, expected_host}}
    command to the ios role. expected_host is the handoff URL's host
    (netloc, port included when present) so the JS `location.host` guard and
    the app's didFinish hold both bind to the same origin. Returns the flight
    reply data, or None for any failure (already replied on the socket).
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
    expected_host = urlparse(handoff["url"]).netloc.lower()
    js = _prefill_js(section["username"], section["password"], expected_host)
    await registry.send_to_role(
        IOS_ROLE,
        {"id": message_id, "type": "evaluateJS", "payload": {"js": js, "expected_host": expected_host}},
    )
    return data


@router.post("/handoff/signin", dependencies=[Depends(require_bearer)])
async def create_signin_handoff(body: SigninHandoffBody) -> dict:
    """Create a pending handoff; the caller sends the returned link to the owner itself.

    Validates the request against the vault section first (HIGH-2); any
    violation is a generic 400.
    """
    _validate_signin_request(body.url, body.vault_key)
    handoff = flight.create_handoff(body.url)
    handoff["vault_key"] = body.vault_key
    handoff["kind"] = "signin"  # gateway.ws dispatches handoff_open on this marker
    return {"id": handoff["id"], "link": LINK_PREFIX + handoff["id"]}
