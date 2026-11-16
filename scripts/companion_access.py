#!/usr/bin/env python3
"""Install-time Companion sessions access for the Mac overlay (install-mac-overlay.sh step i3).

    companion_access.py <token-file> <env-file> <local-port> [<companion-url>]
    companion_access.py --check-url <companion-url>

Picks the gateway the overlay service talks to: the mini PC gateway (the operator's
--companion-url, else https://<host of MINIPC_ROUTER_URL>:8790) when an authorized
GET /central/sessions answers a JSON list there, else the local gateway on loopback.
Then writes the token file (0600) and sets OVERLAY_COMPANION_URL in the env file.
The token comes from the vault in-process; it is never read from argv or env, never printed.
Exit: 0 done, 2 bad arguments, 3 no ios_token in the vault, 4 no gateway answered (nothing written).
"""
from __future__ import annotations

import os
import re
import ssl
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import config  # noqa: E402  (outside any try: a broken import is a traceback, never exit 4)

KEY = "OVERLAY_COMPANION_URL"
MINIPC_GATEWAY_PORT = 8790  # the repo's gateway serve port (scripts/serve.sh)
PROBE_TIMEOUT_S = 10
PROBE_MAX = 64 * 1024
_HOST = re.compile(r"[A-Za-z0-9.-]+")

# No HTTPRedirectHandler (a 3xx is returned as a plain non-200 answer and its Location is never
# requested), no ProxyHandler (the bearer never goes to an env-chosen proxy), no error processor
# (any status comes back as a response). TLS verification stays on (default context).
_OPENER = urllib.request.OpenerDirector()
for _handler in (urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(context=ssl.create_default_context())):
    _OPENER.add_handler(_handler)


def https_origin(raw: str | None, port: int | None = None) -> str | None:
    """`https://<host>:<port>` from an https URL with no userinfo, path, query or fragment, else None.
    `port` given: use it (router URL -> gateway port); None: the URL must carry a numeric port."""
    raw = (raw or "").strip()
    if not raw or any(c in raw for c in "?# \t\\"):
        return None
    try:
        parts = urllib.parse.urlsplit(raw)
        own_port = parts.port  # ValueError on a non-numeric or out-of-range port
    except ValueError:
        return None
    host = parts.hostname or ""
    if (parts.scheme != "https" or "@" in parts.netloc or parts.path not in ("", "/")
            or not _HOST.fullmatch(host)):
        return None
    port = port if port is not None else own_port
    if not port or not 0 < port < 65536:
        return None
    return f"https://{host}:{port}"


def probe(url: str, token: str) -> bool:
    """One authorized GET <url>/central/sessions: True when it answers 200 and a JSON list."""
    req = urllib.request.Request(url.rstrip("/") + "/central/sessions",
                                 headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=PROBE_TIMEOUT_S) as resp:
            if resp.status != 200:
                return False
            body = resp.read(PROBE_MAX)
    except Exception:  # refused, timeout, TLS, bad answer: not this gateway
        return False
    return body.lstrip()[:1] == b"["


def _value(line: bytes) -> bytes | None:
    """The line's OVERLAY_COMPANION_URL value, parsed like mac/voice/service.py load_setup_env."""
    key, sep, value = line.strip().partition(b"=")
    if not sep or key.strip() != KEY.encode():
        return None
    value = value.strip()
    if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in (b"'", b'"'):
        value = value[1:-1]
    return value


def set_env(path: str, url: str, owned: tuple[str, ...]) -> bool:
    """Set KEY=url in the env file unless it holds a value not in `owned` (the owner's): True = kept.
    Every other line stays byte-equal; one KEY line remains; atomic 0600 rewrite."""
    path = os.path.realpath(path)
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        data = b""
    lines = data.splitlines(keepends=True)
    current = next((v for v in map(_value, lines) if v), None)  # first non-empty wins, as the service reads it
    if current is not None and current.decode("utf-8", "replace") not in owned:
        return True
    new_line = f"{KEY}={url}\n".encode()
    out, placed = [], False
    for line in lines:
        if _value(line) is None:
            out.append(line)
        elif not placed:
            out.append(new_line)
            placed = True
    if not placed:
        if out and not out[-1].endswith((b"\n", b"\r")):
            out[-1] += b"\n"
        out.append(new_line)
    new = b"".join(out)
    if new != data:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".env.")  # mode 0600
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return False


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--check-url":
        return 0 if https_origin(argv[1]) else 2
    if len(argv) not in (3, 4) or not argv[2].isdigit():
        print("usage: companion_access.py <token-file> <env-file> <local-port> [<companion-url>]", file=sys.stderr)
        return 2
    token_file, env_file, local_port = argv[0], argv[1], int(argv[2])
    if len(argv) == 4:
        candidate = https_origin(argv[3])
        if candidate is None:
            print("companion_access: --companion-url must be https://<host>:<port>", file=sys.stderr)
            return 2
    else:
        candidate = https_origin(os.environ.get("MINIPC_ROUTER_URL"), MINIPC_GATEWAY_PORT)
    token = config.get_token(name="ios_token")
    if token is None:
        return 3
    local = f"http://127.0.0.1:{local_port}"
    if candidate and probe(candidate, token):
        url, label = candidate, "mini PC gateway"
    elif probe(local, token):
        url, label = local, "local gateway"
    else:
        return 4
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(token.encode())
    os.chmod(token_file, 0o600)
    kept = set_env(env_file, url, (local,) + ((candidate,) if candidate else ()))
    print("Sessions access: kept" if kept else f"Sessions access: set ({label})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
