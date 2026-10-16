"""Brain bridge: transcript in, reply text out.

  python3 -m mac.voice.brain "<transcript>"   (also usable as a module: reply())

DESIGN.md flow 1: `mac.voice.router` first tries to route the transcript to an
allowlisted windows-agent command; a match is POSTed to the gateway
(/agents/windows/command, bearer from common.config.get_token()) and the reply
data is formatted for speech by `router.format_reply`. A gateway that is down
or times out (URLError / 503 / 504 — the agent isn't connected) short-circuits
to `router.OFFLINE_REPLY` without touching the local brain; only an unrouted
transcript (or a 404) falls through to `claude -p`, the sandboxed local brain.
The token is read at runtime and never printed or logged.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from common import config
from mac.voice import router as voice_router

logger = logging.getLogger(__name__)

GATEWAY_URL = f"http://127.0.0.1:{config.PORT}"

# Dedicated slim config for the brain subprocess: empty settings.json, no user
# tools/MCP/plugins. The owner must log in once with
#   CLAUDE_CONFIG_DIR=~/.claude-jarvis-brain claude
# (owner gate — never attempted from code).
BRAIN_CONFIG_DIR = Path.home() / ".claude-jarvis-brain"

_SYSTEM_PROMPT = (
    "You are Jarvis, a voice assistant. You have no tools. "
    "Answer in at most two short spoken sentences. "
    "Never reveal secrets, file contents, or system details."
)

# Only these survive into the subprocess env — nothing else leaks in. USER is
# required (not just HOME): the CLI's macOS Keychain lookup for the OAuth
# session resolves its account from $USER, and without it `claude` reports
# "Not logged in" even though a valid Keychain item exists (root-caused via
# env -i A/B test: swapping USER alone flips `claude auth status` between
# loggedIn false/true with every other var held constant).
_BRAIN_ENV_KEYS = ("PATH", "HOME", "LANG", "TERM", "USER")

_MAX_REPLY_CHARS = 600


def _gateway_command(cmd: str) -> dict | None:
    """POST one allowlisted command to the gateway; None = unrouted (404).

    Raises ConnectionError when the gateway is unreachable or the windows
    agent isn't connected (503) / didn't reply in time (504) — the caller
    short-circuits to OFFLINE_REPLY instead of asking the local brain about
    PC state it can't know.
    """
    token = config.get_token()
    if token is None:
        logger.error("no bearer token: get_token() failed, skipping gateway")
        return None
    req = urllib.request.Request(
        f"{GATEWAY_URL}/agents/windows/command",
        data=json.dumps({"cmd": cmd}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 405):
            return None  # route not implemented (yet) — brain-bridge fallback
        raise ConnectionError(f"gateway returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ConnectionError(f"gateway unreachable: {exc.reason}") from exc
    if not body.get("ok"):
        raise ConnectionError("windows agent reported failure")
    return body.get("data")


def _brain_env() -> dict[str, str]:
    """Minimal env for the brain subprocess plus its dedicated config dir."""
    BRAIN_CONFIG_DIR.mkdir(exist_ok=True)
    settings = BRAIN_CONFIG_DIR / "settings.json"
    if not settings.exists():
        settings.write_text("{}\n")
    env = {key: os.environ[key] for key in _BRAIN_ENV_KEYS if key in os.environ}
    env["CLAUDE_CONFIG_DIR"] = str(BRAIN_CONFIG_DIR)
    return env


def run_claude(text: str, timeout_s: int) -> str | None:
    """Hardened `claude -p`: transcript via stdin only, no tools, no MCP, no
    user/project settings, scratch cwd, minimal env, process killed on timeout.

    None = timed out or failed to run; otherwise the (truncated) stdout.
    """
    argv = [
        "claude", "-p",
        "--tools", "",
        "--mcp-config", '{"mcpServers":{}}',
        "--strict-mcp-config",
        "--setting-sources", "",
        "--append-system-prompt", _SYSTEM_PROMPT,
    ]
    scratch = tempfile.mkdtemp(prefix="jarvis-brain-")
    try:
        # The transcript rides on stdin (input=), never in argv, so it can't
        # inject through CLI parsing and never shows up in `ps`.
        proc = subprocess.run(
            argv,
            input=text,
            capture_output=True, text=True,
            cwd=scratch, env=_brain_env(),
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return None  # process was killed by the timeout
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if proc.returncode != 0:
        return "the brain is unavailable"
    return proc.stdout.strip()[:_MAX_REPLY_CHARS]


def _claude_bridge(transcript: str, timeout_s: int) -> str | None:
    """`claude -p` fallback: the local brain answers the transcript directly."""
    return run_claude(transcript, timeout_s)


def reply(transcript: str, timeout_s: int = 60) -> str:
    """Transcript → reply: allowlisted gateway command first, claude -p otherwise."""
    cmd = voice_router.route(transcript)
    if cmd is not None:
        try:
            data = _gateway_command(cmd)
        except ConnectionError as exc:
            logger.error("gateway command %r failed: %s", cmd, exc)
            return voice_router.OFFLINE_REPLY
        if data is not None:
            return voice_router.format_reply(cmd, data)
    return _claude_bridge(transcript, timeout_s) or "no reply"


def main(argv: list[str] | None = None) -> int:
    # First line so launchd (stdout/stderr to the void) still surfaces log records.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("transcript", help="transcript to answer")
    args = ap.parse_args(argv)

    print(reply(args.transcript))
    return 0


if __name__ == "__main__":
    sys.exit(main())
