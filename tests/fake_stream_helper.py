"""Fake linux/stream_helper.py for tests/test_stream.py: same framing, no capture.

Emits one keyframe at start and one more per {"keyframe": true} line; exits on
stdin EOF. `--die` exits at once (helper-crash path).
"""

import json
import struct
import sys

if "--die" in sys.argv:
    sys.exit(1)

out = sys.stdout.buffer
n = 0


def key() -> None:
    global n
    au = b"\x00\x00\x00\x01\x67KEY" + str(n).encode()
    out.write(struct.pack("<IBd", len(au), 1, float(n)) + au)
    out.flush()
    n += 1


key()
for line in sys.stdin:
    if json.loads(line).get("keyframe"):
        key()
