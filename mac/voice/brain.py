"""Brain bridge: transcript in, reply text out.

  python3 -m mac.voice.brain "<transcript>"   (also usable as a module: reply())

DESIGN.md flow 1: the command goes to the gateway
(POST /agents/windows/command, bearer from common.config.get_token()); when no
gateway route matches (404 — the route isn't implemented yet) the transcript
falls through to `claude -p`, the local brain. The token is read at runtime and
never printed or logged.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import urllib.error
import urllib.request

from common import config

logger = logging.getLogger(__name__)

GATEWAY_URL = f"http://127.0.0.1:{config.PORT}"


def _gateway_command(transcript: str) -> str | None:
    """Send the transcript as a command to the gateway; None = no route matched."""
    token = config.get_token()
    if token is None:
        logger.error("no bearer token: get_token() failed, skipping gateway")
        return None
    req = urllib.request.Request(
        f"{GATEWAY_URL}/agents/windows/command",
        data=json.dumps({"cmd": transcript}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 405):
            return None  # route not implemented (yet) — brain-bridge fallback
        logger.error("gateway returned HTTP %s", exc.code)
        return None
    except urllib.error.URLError as exc:
        logger.error("gateway unreachable: %s", exc.reason)
        return None


def _claude_bridge(transcript: str) -> str:
    """`claude -p` fallback: the local brain answers the transcript directly."""
    proc = subprocess.run(
        ["claude", "-p", transcript],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return "the brain is unavailable"
    return proc.stdout.strip()


def reply(transcript: str) -> str:
    """Transcript → reply: gateway command first, claude -p when no route matches."""
    return _gateway_command(transcript) or _claude_bridge(transcript) or "no reply"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("transcript", help="transcript to answer")
    args = ap.parse_args(argv)

    print(reply(args.transcript))
    return 0


if __name__ == "__main__":
    sys.exit(main())
