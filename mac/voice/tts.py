"""Kokoro-FastAPI TTS: text in, wav file out.

  python3 -m mac.voice.tts --text "<text>" --out <file.wav>

Copied from the jarvis-phone scripts/tts.sh request pattern (not imported —
separate repo): POST {base}/v1/audio/speech with {input, voice, speed,
response_format:"wav"}. Defaults voice=af_heart, speed=1.0,
base=http://localhost:8880; env overrides KOKORO_VOICE / KOKORO_SPEED /
KOKORO_BASE_URL. Service must be running (jarvis-phone scripts/kokoro-start.sh).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

VOICE = os.environ.get("KOKORO_VOICE", "af_heart")
SPEED = float(os.environ.get("KOKORO_SPEED", "1.0"))
BASE_URL = os.environ.get("KOKORO_BASE_URL", "http://localhost:8880")


def synthesize(text: str, out: Path) -> None:
    """Speak text into out as wav. Raises on HTTP/service failure."""
    body = json.dumps(
        {"input": text, "voice": VOICE, "speed": SPEED, "response_format": "wav"}
    ).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/v1/audio/speech",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp, open(out, "wb") as f:
        f.write(resp.read())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", required=True, help="text to speak")
    ap.add_argument("--out", type=Path, required=True, help="output wav path")
    args = ap.parse_args(argv)

    try:
        synthesize(args.text, args.out)
    except Exception as exc:  # service down / HTTP error — surface, don't half-write
        print(f"tts: Kokoro request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
