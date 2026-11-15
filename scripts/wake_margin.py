#!/usr/bin/env python3
"""Offline check of the playback-only weak wake margin (mac/voice/wakeword.py WEAK_THRESHOLD).

A digital mix, no recording of anyone: every voice is the local Kokoro synth.
  - own:  Jarvis's own lines as the shipped voice reads them (gateway.overlay._voice_text, which
          drops the name), including near-names the filter keeps ("Travis", "Jervis").
  - wake: other Kokoro voices saying "Hey Jarvis." mixed over a story read by the shipped voice,
          at 0/+3/+6 dB against the story's RMS, at 4 offsets.
  - room: room talk with no wake word, mixed over the story at 0/+6 dB.
Prints the peak score per mix and the counts at the weak (0.1) and full (0.5) thresholds. Exit 1 when
an own line or room talk reaches WEAK_THRESHOLD (the margin is gone), or a NEAR line does while the
host's self-name hold (gateway.overlay._NEAR_NAME_RE) would not cover it.

Run: uv run --with kokoro-onnx python scripts/wake_margin.py --models DIR
  DIR holds hey_jarvis_v0.1.onnx, melspectrogram.onnx, embedding_model.onnx (openWakeWord's
  download_models); Kokoro files come from HWMODE_TTS_DIR as for the service.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.overlay import _NEAR_NAME_RE, _voice_text  # noqa: E402
from hwmode.tts import Voice  # noqa: E402
from mac.voice import wakeword  # noqa: E402

STORY = ["Once upon a time a lighthouse keeper lived alone on a small rocky island far from the coast.",
         "Every night he climbed the long stair, lit the great lamp and watched the ships pass safely by.",
         "One winter a storm cut the island off for weeks, and he kept the light burning all the same."]
OWN = ["Jarvis Companion is up to date, sir.", "Which one: Jarvis capabilities or Jarvis overlay?",
       "I hid the Jarvis card.", "Travis sent the build report an hour ago.",
       "The Jervis branch is merged.", "Your service is running on the mini PC, sir.",
       "Shall I push the branch to GitHub?", "The harvest notes are on screen."]
# "Hey" right before a near-name the voice keeps sounds like the wake phrase in Jarvis's own voice
# (full threshold too; _voice_text strips only "Jarvis"), and so does a name like Jarvis ("Jarvik").
# A weak hit is dropped while such a line plays
# (_NEAR_NAME_RE); a line that reaches the weak band without matching it fails the run.
NEAR = ["Hey, Travis.", "Hey Jervis", "Hey, the Jervis branch is merged.", "Hey, Davis.", "Hey Mavis.",
        "Hey, Harvey's here.", "Hey, service is up.", "Hey, Marcus.", "Hey, nervous?", "Hey, Marvin.",
        "Hey, Garvey.", "Hey, Davies.", "Hey, curves.", "Hey, Jarvik.", "Hey, Charles.", "Hey, it serves.",
        "Jarvik.", "Hi, Charles.", "Okay, Jarvik.", "Hay, Jarvik.", "Yes, Jarvik.", "Ask Charles about it."]
WAKE_VOICES = ["am_adam", "am_michael", "bm_george", "af_bella"]
ROOM = ["Did you see the game last night?", "Can you pass me the remote?",
        "I think it is going to rain later.", "What time is dinner tonight?",
        "Turn it down a little, it's loud.", "We should call your mother tomorrow."]
OFFSETS_S = [2, 5, 8, 11]
GAINS_DB = [0, 3, 6]


def synth(voice: Voice, text: str, name: str | None = None) -> np.ndarray:
    """16 kHz float mono (linear resample; scoring only)."""
    if name:
        voice.name = name
    pcm, rate = voice.synth(text)
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    n = int(len(x) * wakeword.SAMPLE_RATE / rate)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x ** 2))) or 1e-9


def peak(model, x: np.ndarray) -> float:
    model.reset()
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    pcm = np.concatenate([pcm, np.zeros(wakeword.SAMPLE_RATE, dtype=np.int16)])  # 1 s tail
    best = 0.0
    for i in range(0, len(pcm) - wakeword.FRAME_SAMPLES + 1, wakeword.FRAME_SAMPLES):
        best = max(best, max(model.predict(pcm[i:i + wakeword.FRAME_SAMPLES]).values()))
    return best


def over(story: np.ndarray, clip: np.ndarray, at_s: float, db: float) -> np.ndarray:
    mix = story.copy()
    start = int(at_s * wakeword.SAMPLE_RATE)
    end = min(len(mix), start + len(clip))
    mix[start:end] += clip[:end - start] * (rms(story) / rms(clip)) * 10 ** (db / 20)
    return mix / max(1.0, float(np.abs(mix).max()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", type=Path, required=True, help="openWakeWord model dir")
    args = ap.parse_args(argv)
    from openwakeword.model import Model
    model = Model(wakeword_models=[str(args.models / "hey_jarvis_v0.1.onnx")], inference_framework="onnx",
                  melspec_model_path=str(args.models / "melspectrogram.onnx"),
                  embedding_model_path=str(args.models / "embedding_model.onnx"))
    voice = Voice()
    shipped = voice.name
    if not voice.available():
        print("kokoro voice not available (HWMODE_TTS_DIR / kokoro-onnx)", file=sys.stderr)
        return 2
    weak, full = wakeword.WEAK_THRESHOLD, wakeword.THRESHOLD
    story = np.concatenate([synth(voice, _voice_text(t), shipped) for t in STORY])
    print(f"thresholds weak={weak} full={full}; shipped voice {shipped}; story {len(story) / 16000:.1f} s")
    print(f"story alone peak {peak(model, story):.4f}")

    own = [(t, peak(model, synth(voice, _voice_text(t), shipped))) for t in OWN]
    for t, p in own:
        print(f"own {p:.4f}  {_voice_text(t)!r}")
    own_peak = max(p for _t, p in own)
    raw = peak(model, synth(voice, OWN[0], shipped))
    print(f"own peak {own_peak:.4f}; the same first line with the name left in: {raw:.4f}")
    near_bad = 0
    for t in NEAR:
        p, held = peak(model, synth(voice, _voice_text(t), shipped)), bool(_NEAR_NAME_RE.search(_voice_text(t)))
        near_bad += p >= weak and not held
        print(f"near {p:.4f} {'held' if held else 'not held'}  {_voice_text(t)!r}")

    counts = {(db, th): 0 for db in GAINS_DB for th in (weak, full)}
    room_peak = 0.0
    for name in WAKE_VOICES:
        hj = synth(voice, "Hey Jarvis.", name)
        cells = []
        for db in GAINS_DB:
            for at in OFFSETS_S:
                p = peak(model, over(story, hj, at, db))
                cells.append(f"+{db}dB@{at}s={p:.3f}")
                for th in (weak, full):
                    counts[(db, th)] += p >= th
        talk = [synth(voice, t, name) for t in ROOM]
        rp = {db: max(peak(model, over(story, c, 3, db)) for c in talk) for db in (0, 6)}
        room_peak = max(room_peak, *rp.values())
        print(f"{name} hey-jarvis-over-story: {' '.join(cells)} | room talk peak +0dB={rp[0]:.3f} +6dB={rp[6]:.3f}")
    n = len(WAKE_VOICES) * len(OFFSETS_S)
    for db in GAINS_DB:
        print(f"+{db} dB: {counts[(db, weak)]}/{n} mixes reach {weak}, {counts[(db, full)]}/{n} reach {full}")
    print(f"margin: own peak {own_peak:.4f}, room talk peak {room_peak:.4f}, weak threshold {weak}; "
          f"near lines in the weak band with no hold: {near_bad}")
    return 0 if own_peak < weak and room_peak < weak and not near_bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
