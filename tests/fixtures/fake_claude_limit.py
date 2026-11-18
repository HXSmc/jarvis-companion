#!/usr/bin/env python3
"""A stand-in claude CLI that is always at its usage limit (tests + the Mac live check).

Reads stream-json lines on stdin. Every `user` line gets a rejected rate_limit_event and an
error result (status 429); every `control_request` gets a success `control_response`.
Stdlib only, no network, no files; argv is ignored.
"""

import json
import sys


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if not isinstance(msg, dict):
        continue
    if msg.get("type") == "user":
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
        out({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
             "result": "fake limit"})
    elif msg.get("type") == "control_request":
        out({"type": "control_response",
             "response": {"subtype": "success", "request_id": msg.get("request_id"), "response": {}}})
