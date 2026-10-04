"""Wake word daemon: openWakeWord hey_jarvis on Mac mic input.

Offline test mode:  python3 -m mac.voice.wakeword --test-audio <wav> --dry-run
Live mode:          python3 -m mac.voice.wakeword
  ffmpeg mic capture -> 80 ms frames -> model -> on detection POST
  {type:"wake", source:"mac"} to the gateway + local callbacks.

Mic capture shells out to ffmpeg (jarvis-phone stt.sh pattern: ffmpeg owns the
resample/mono conversion, no Python audio package). --dry-run never touches
the network: the POST lives only in the live-mode path.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np
from openwakeword.model import Model

from common import config

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
MODEL_PATH = MODELS_DIR / "hey_jarvis_v0.1.onnx"


def load_model() -> Model:
    """Model with all supporting files from models/ (not the venv package dir)."""
    return Model(
        wakeword_models=[str(MODEL_PATH)],
        inference_framework="onnx",
        melspec_model_path=str(MODELS_DIR / "melspectrogram.onnx"),
        embedding_model_path=str(MODELS_DIR / "embedding_model.onnx"),
    )
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms at 16 kHz — openWakeWord's canonical chunk
THRESHOLD = 0.5

# Local subscribers notified on every detection (live + test mode).
wake_callbacks = []


def score_wav(model: Model, wav_path: Path) -> tuple[bool, float, float | None]:
    """Stream a 16 kHz mono wav through the model. Returns (detected, peak_score, at_seconds)."""
    with wave.open(str(wav_path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
            raise SystemExit(
                f"{wav_path}: expected {SAMPLE_RATE} Hz mono, got "
                f"{w.getframerate()} Hz {w.getnchannels()}ch — convert with "
                f"ffmpeg -ar 16000 -ac 1"
            )
        total = 0
        peak = 0.0
        while True:
            frames = w.readframes(FRAME_SAMPLES)
            if not frames:
                break
            scores = model.predict(np.frombuffer(frames, dtype=np.int16))
            score = max(scores.values())
            peak = max(peak, score)
            if score >= THRESHOLD:
                return True, peak, total / SAMPLE_RATE
            total += len(frames) // 2
    return False, peak, None


def notify_wake(at_seconds: float, dry_run: bool) -> None:
    """Local callbacks always; gateway POST only in live mode (never when --dry-run)."""
    print(f"WAKE detected at {at_seconds:.2f}")
    for cb in wake_callbacks:
        cb(at_seconds)
    if dry_run:
        return  # no network side effects in dry-run, by construction
    token = config.get_token()
    if token is None:
        logger.error("no bearer token: get_token() failed, wake not delivered to gateway")
        return
    req = urllib.request.Request(
        f"http://127.0.0.1:{config.PORT}",
        data=json.dumps({"type": "wake", "source": "mac"}).encode(),
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)


def run_live() -> None:
    """ffmpeg mic capture -> model -> POST + callbacks on detection."""
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-i", ":0",
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", "-",
        ],
        stdout=subprocess.PIPE,
    )
    assert ffmpeg.stdout is not None
    model = load_model()
    total = 0
    try:
        while True:
            chunk = ffmpeg.stdout.read(FRAME_SAMPLES * 2)
            if not chunk:
                raise SystemExit("mic capture ended (ffmpeg exited)")
            scores = model.predict(np.frombuffer(chunk, dtype=np.int16))
            if max(scores.values()) >= THRESHOLD:
                notify_wake(total / SAMPLE_RATE, dry_run=False)
                model.reset()
            total += len(chunk) // 2
    finally:
        ffmpeg.terminate()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--test-audio", type=Path, help="offline: score this 16 kHz mono wav")
    ap.add_argument("--dry-run", action="store_true", help="never POST to the gateway")
    args = ap.parse_args(argv)

    model = load_model()

    if args.test_audio:
        detected, peak, at = score_wav(model, args.test_audio)
        if not detected:
            print(f"no detection: peak score {peak:.3f} < {THRESHOLD}", file=sys.stderr)
            return 1
        notify_wake(at, dry_run=args.dry_run)
        return 0

    run_live()
    return 0


if __name__ == "__main__":
    sys.exit(main())
