#!/usr/bin/env python3
"""Log a PC browser in to the desktop Jarvis page (gateway/desktop.py).

Mints a 60 s single-use /desktop link and types it, plus Enter, into whatever
field has focus on the target screen. First focus a browser address bar there
(e.g. `jarvis-screen --target windows events '[{"type":"tap",...}]'` on the
new-tab button). The link and the ios_token are never printed.

usage: scripts/desktop_link.py [--target windows|mac] [--gateway http://127.0.0.1:8788]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from common import config

TEXT_MAX = 90  # gateway text events take <= 100 chars


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--target", default="windows", choices=["windows", "mac"])
    parser.add_argument("--gateway", default="http://127.0.0.1:8788")
    args = parser.parse_args()
    token = config.get_token(name="ios_token")
    if not token:
        print("no ios_token in the vault", file=sys.stderr)
        return 1
    with httpx.Client(base_url=args.gateway, headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:
        url = client.post("/desktop/launch").raise_for_status().json()["url"]
        chunks = [url[i:i + TEXT_MAX] for i in range(0, len(url), TEXT_MAX)]
        events = [{"type": "text", "text": c} for c in chunks] + [{"type": "key", "key": "return"}]
        r = client.post(f"/screen/{args.target}/input", json={"events": events})
    print(f"typed a desktop login link on {args.target}: HTTP {r.status_code}")
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
