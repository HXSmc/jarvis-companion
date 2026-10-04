"""whisper.cpp STT: wav in, transcript on stdout.

  python3 -m mac.voice.stt --wav <file>

Copied from the jarvis-phone scripts/stt.sh invocation pattern (not imported —
separate repo): ffmpeg conditions the audio (dynaudnorm + 16 kHz mono) before
whisper sees it, and conditioning failure is non-fatal — the original file is
transcribed as-is rather than the call going deaf. Binary and model stay in
the jarvis-phone checkout (gigabytes, not copied); env overrides WHISPER_BIN /
WHISPER_MODEL point elsewhere.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

JARVIS_PHONE = Path("~/Desktop/personal/jarvis-phone").expanduser()
WHISPER_BIN = os.environ.get("WHISPER_BIN", str(JARVIS_PHONE / "vendor/whisper.cpp/build/bin/whisper-cli"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", str(JARVIS_PHONE / "models/ggml-base.en.bin"))


def condition(wav: Path) -> Path:
    """dynaudnorm + 16 kHz mono copy in a temp file; fall back to the input on failure."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    conditioned = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(wav),
            "-af", "dynaudnorm=f=150:g=15", "-ar", "16000", "-ac", "1",
            tmp.name,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if conditioned.returncode == 0:
        return Path(tmp.name)
    os.unlink(tmp.name)
    print("stt: ffmpeg conditioning failed, transcribing raw input", file=sys.stderr)
    return wav


def transcribe(wav: Path) -> str:
    """whisper-cli transcript of wav, stripped. Empty string on failure."""
    proc = subprocess.run(
        [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", str(condition(wav)), "-nt"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"stt: whisper-cli exited {proc.returncode}", file=sys.stderr)
        return ""
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wav", type=Path, required=True, help="wav file to transcribe")
    args = ap.parse_args(argv)

    transcript = transcribe(args.wav)
    if transcript:
        print(transcript)
    return 0 if transcript else 1


if __name__ == "__main__":
    sys.exit(main())
