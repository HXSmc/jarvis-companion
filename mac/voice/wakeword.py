"""Wake word daemon: openWakeWord hey_jarvis on Mac mic input.

Offline test mode:  python3 -m mac.voice.wakeword --test-audio <wav> --dry-run
Live mode:          python3 -m mac.voice.wakeword
  ffmpeg mic capture -> 80 ms frames -> model -> on detection POST
  {type:"wake", source:"mac"} to the overlay service + local callbacks.

Mic capture shells out to ffmpeg (jarvis-phone stt.sh pattern: ffmpeg owns the
resample/mono conversion, no Python audio package). --dry-run never touches
the network: the POST lives only in the live-mode path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path

import numpy as np
from openwakeword.model import Model

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
MODEL_PATH = MODELS_DIR / "hey_jarvis_v0.1.onnx"

# The overlay service (mac.voice.service) owns wake notifications now.
OVERLAY_PORT = int(os.environ.get("OVERLAY_PORT", "8791"))


def load_model() -> Model:
    """Model with all supporting files from models/ (not the venv package dir)."""
    if not MODEL_PATH.exists():
        # Not committed: openWakeWord's models are CC BY-NC-SA 4.0, the repo is Apache-2.0.
        from openwakeword.utils import download_models
        download_models(["hey_jarvis_v0.1"], target_directory=str(MODELS_DIR))
    return Model(
        wakeword_models=[str(MODEL_PATH)],
        inference_framework="onnx",
        melspec_model_path=str(MODELS_DIR / "melspectrogram.onnx"),
        embedding_model_path=str(MODELS_DIR / "embedding_model.onnx"),
    )
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms at 16 kHz — openWakeWord's canonical chunk
THRESHOLD = 0.5
# The system default input, the same device the Swift app's speech-to-text hears.
# Never a bare index: avfoundation index 0 can be an iPhone Continuity mic or a
# virtual device (BlackHole, Teams), and then the wake word hears nothing.
MIC = os.environ.get("OVERLAY_MIC", "default")

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


def ffmpeg_argv(mic: str = MIC) -> list[str]:
    """ffmpeg mic capture: 16 kHz mono s16le on stdout, from avfoundation audio device `mic`."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-i", f":{mic}",
        "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", "-",
    ]


def notify_wake(at_seconds: float, dry_run: bool, score: float | None = None) -> None:
    """Local callbacks always; gateway POST only in live mode (never when --dry-run)."""
    print(f"WAKE detected at {at_seconds:.2f}")
    for cb in wake_callbacks:
        cb(at_seconds)
    if dry_run:
        return  # no network side effects in dry-run, by construction
    # Dedicated overlay token (the shared gateway token never reaches the Mac app).
    from gateway.overlay import overlay_token
    token = overlay_token()
    req = urllib.request.Request(
        f"http://127.0.0.1:{OVERLAY_PORT}/overlay/wake",
        data=json.dumps({"type": "wake", "source": "mac", "score": score}).encode(),
        # Without the JSON content type FastAPI answers 422 and the wake never reaches the app.
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)


def run_live(on_stats=None) -> None:
    """ffmpeg mic capture -> model -> POST + callbacks on detection.

    on_stats(peak_score, peak_level) every ~10 s: a mic that stays at level 0
    is the wrong device or a denied permission, visible on the debug page.
    """
    ffmpeg = subprocess.Popen(ffmpeg_argv(), stdout=subprocess.PIPE)
    assert ffmpeg.stdout is not None
    model = load_model()
    total = 0
    peak_score = peak_level = 0.0
    frames = 0
    try:
        while True:
            chunk = ffmpeg.stdout.read(FRAME_SAMPLES * 2)
            if not chunk:
                raise SystemExit("mic capture ended (ffmpeg exited)")
            pcm = np.frombuffer(chunk, dtype=np.int16)
            score = max(model.predict(pcm).values())
            peak_score = max(peak_score, score)
            peak_level = max(peak_level, float(np.abs(pcm).max()) / 32768 if pcm.size else 0.0)
            if score >= THRESHOLD:
                try:
                    notify_wake(total / SAMPLE_RATE, dry_run=False, score=round(float(score), 3))
                except Exception as exc:  # one failed POST must not end wake listening
                    logger.warning("wake POST failed (%s: %s)", type(exc).__name__, exc)
                model.reset()
            frames += 1
            if on_stats and frames % 125 == 0:  # 125 x 80 ms = 10 s
                on_stats(round(float(peak_score), 3), round(peak_level, 3))
                peak_score = peak_level = 0.0
            total += len(chunk) // 2
    finally:
        ffmpeg.terminate()


def main(argv: list[str] | None = None) -> int:
    # First line so launchd (stdout/stderr to the void) still surfaces log records.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s: %(message)s")
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
