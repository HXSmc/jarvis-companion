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
import select
import subprocess
import sys
import threading
import time
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


# The capture restarts (one ffmpeg at a time; the old one is terminated and waited for):
RESTART_GAP_S = 1.0  # a flood of reconfigure notes restarts it at most once a second
STALL_S = 3.0        # no audio for this long = a stalled capture
HEAL_WAIT_S = 1.0    # pause before a self-heal restart, so a dead device does not spin
HEAL_MAX = 5         # self-heal restarts with no 10 s of audio between them, then give up
FRAME_BYTES = FRAME_SAMPLES * 2

_capture_lock = threading.Lock()
_capture: dict = {"proc": None, "reason": None}


def restart_capture(reason: str) -> bool:
    """Restart the live capture: the app rebuilt its audio engine (Mac run 1: an ffmpeg stream open
    while the app built its voice-processing device kept its level but never scored again; a fresh
    one did). Terminating the current ffmpeg ends its read; run_live starts a fresh one and writes
    the `wake_capture restarted` row. False when no capture runs."""
    with _capture_lock:
        proc = _capture["proc"]
        if proc is None:
            return False
        _capture["reason"] = reason
    proc.terminate()
    return True


def _note_restart(reason: str) -> None:
    logger.info("wake_capture restarted reason=%s", reason)
    from gateway.overlay import debug_log
    debug_log.note("wake_capture", f"restarted reason={reason}")


def _stop(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if proc.stdout is not None:
        proc.stdout.close()


def _read_frame(proc, timeout: float) -> bytes | None:
    """One 80 ms frame; b"" when ffmpeg ended, None when no audio came for `timeout` s."""
    fd = proc.stdout.fileno()
    buf = b""
    end = time.monotonic() + timeout
    while len(buf) < FRAME_BYTES:
        left = end - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return None
        chunk = os.read(fd, FRAME_BYTES - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


def run_live(on_stats=None, on_restart=None, popen=subprocess.Popen, model=None) -> None:
    """ffmpeg mic capture -> model -> POST + callbacks on detection.

    on_stats(peak_score, peak_level) every ~10 s: a mic that stays at level 0
    is the wrong device or a denied permission, visible on the debug page.
    The capture restarts on restart_capture() (reason=reconfigure), when ffmpeg exits
    (reason=exited) or when its audio stalls (reason=stalled); on_restart(reason) after each new
    ffmpeg starts (default: a `wake_capture` debug row). HEAL_MAX self-heals in a row end it.
    """
    on_restart = on_restart or _note_restart
    model = model or load_model()
    proc = None
    reason = None    # why the next ffmpeg starts (None: the first start)
    started = 0.0    # monotonic time of the last start
    heals = 0        # self-heal restarts since the last 10 s of audio
    good = 0         # frames read since the last start
    total = 0
    peak_score = peak_level = 0.0
    frames = 0
    try:
        while True:
            if proc is None:
                wait = (HEAL_WAIT_S if reason in ("exited", "stalled")
                        else RESTART_GAP_S - (time.monotonic() - started) if reason else 0)
                if wait > 0:
                    time.sleep(wait)
                with _capture_lock:  # a restart asked for from now on terminates this new one
                    proc = popen(ffmpeg_argv(), stdout=subprocess.PIPE, bufsize=0)
                    _capture["proc"], _capture["reason"] = proc, None
                started, good = time.monotonic(), 0
                if reason:
                    model.reset()
                    on_restart(reason)
            chunk = _read_frame(proc, STALL_S)
            if not chunk:
                with _capture_lock:
                    asked = _capture["reason"]
                    _capture["proc"], _capture["reason"] = None, None
                _stop(proc)
                proc = None
                reason = asked or ("stalled" if chunk is None else "exited")
                if asked is None:
                    heals += 1
                    if heals > HEAL_MAX:
                        raise SystemExit("mic capture ended (ffmpeg exited)")
                continue
            good += 1
            if good == 125:  # 10 s of audio: the capture works again
                heals = 0
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
        with _capture_lock:
            _capture["proc"], _capture["reason"] = None, None
        if proc is not None:
            _stop(proc)


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
