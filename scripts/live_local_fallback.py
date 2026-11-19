#!/usr/bin/env python3
"""Live check of the overlay's usage-limit fallback against a real local model front.

Claude is the always-at-limit stand-in CLI (tests/fixtures/fake_claude_limit.py), so every answer
turn hits the usage limit and moves to the local model. Three turns run in one conversation
through the real `_answer_turn` with real lean/local pools; only the macOS-only parts are stubbed
(`apps` names Safari, `run` records the launch instead of running `open`).

    python3 scripts/live_local_fallback.py [--url http://127.0.0.1:8410] [--out result.json]

The local timeouts default to 400 s / 600 s here so a cold mini PC prompt (about 6 minutes) is
part of the evidence. Prints one table row per turn; exit 0 when every turn ended with a done event.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ("What is the capital of France?", "Open Safari", "And what is its population?")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8410")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    os.environ["OVERLAY_CLAUDE_BIN"] = str(ROOT / "tests" / "fixtures" / "fake_claude_limit.py")
    os.environ["OVERLAY_LOCAL_URL"] = args.url
    os.environ.setdefault("OVERLAY_LOCAL_TTFT_S", "400")
    os.environ.setdefault("OVERLAY_LOCAL_WALL_S", "600")
    sys.path.insert(0, str(ROOT))
    scratch = tempfile.mkdtemp(prefix="live-local-")
    tempfile.tempdir = scratch  # every claude scratch cwd lands here and goes with it at the end
    import httpx

    from gateway import overlay
    from mac.voice import acks, lean, local_llm

    try:
        health = httpx.get(args.url + "/health", timeout=10, trust_env=False)
        print(f"health {health.status_code} {health.text.strip()[:200]}")
    except httpx.HTTPError as exc:
        print(f"health unreachable: {type(exc).__name__}")
    launched: list = []

    def run(argv, *a, **k):
        launched.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    pools = [
        lean.Conversations(lambda: lean.LeanSession(model="sonnet", system_prompt=overlay.answer_prompt(),
                                                    tools="WebSearch,WebFetch")),
        lean.Conversations(lambda: lean.LeanSession(model="sonnet", system_prompt=overlay.quiet_prompt())),
        lean.Conversations(lambda: local_llm.LocalSession(system_prompt=overlay.answer_prompt())),
    ]
    deps = overlay.OverlayDeps(acks=acks.AckCache(None), synth=None, conversations=pools[0],
                               quiet_conversations=pools[1], vlm_ask=overlay._LazyVLM(),
                               run=run, apps={"safari": "Safari"}, local_conversations=pools[2])
    rows = []

    async def turns() -> None:
        for q in QUESTIONS:
            body = overlay.TurnBody(text=q, conv_id="live-local")
            metrics = {"stt_final_ms": None, "ack_ms": None, "ttft_s": None,
                       "first_answer_audio_s": None, "total_s": None}
            said: list[str] = []
            failed: list[str] = []
            steps: list = []
            events: list[dict] = []
            start = deps.clock()
            async for ev in overlay._answer_turn(body, q, deps, metrics, said, start, failed=failed,
                                                 on_step=lambda step, **kw: steps.append((step, kw))):
                events.append({k: v for k, v in ev.items() if k != "pcm_b64"})
            events.append({**overlay._finish(body, deps, metrics, said, start), "error": failed[0] if failed else None})
            local = pools[2].peek(body.conv_id)
            rows.append({"question": q, "ttft_s": metrics["ttft_s"], "total_s": metrics["total_s"],
                         "upstream": getattr(local, "upstream", None), "events": events,
                         "steps": [s for s in steps if s[0] == "local"]})
            print(f"| {q} | {metrics['ttft_s']} | {metrics['total_s']} | {rows[-1]['upstream']} | "
                  + "; ".join(f"{e['type']}:{e.get('text') or e.get('card', {}).get('title') or e.get('error') or ''}"
                              for e in events) + " |", flush=True)

    try:
        asyncio.run(turns())
    finally:
        for pool in pools:  # end every stand-in claude process and local client this run started
            for s in [*pool._sessions.values(), pool._spare]:
                if s is not None:
                    s.close()
        tempfile.tempdir = None
        shutil.rmtree(scratch, ignore_errors=True)
    says = [e["text"] for r in rows for e in r["events"] if e["type"] == "say"]
    summary = {"switch_line": says.count(overlay.LOCAL_SWITCH[0]), "slow_line": says.count(overlay.LOCAL_SLOW_LINE),
               "launched": launched, "all_done": all(r["events"][-1]["type"] == "done" for r in rows)}
    print(json.dumps(summary))
    if args.out:
        Path(args.out).write_text(json.dumps({"rows": rows, "summary": summary}, indent=1))
    return 0 if summary["all_done"] else 1


if __name__ == "__main__":
    sys.exit(main())
