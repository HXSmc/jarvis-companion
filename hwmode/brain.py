"""Lean, persistent Claude Code session used as Hardware Mode's brain.

Runs the official `claude` CLI headless with stream-json in/out, on the owner's
own Max login. The session is stripped down (own system prompt, no tools,
plugins, MCP or user settings) so a turn costs ~1-2k tokens and returns in about
1 s instead of loading ~80k+ tokens of Claude Code context every turn.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

SYSTEM_PROMPT = """You are JARVIS, a hands-on hardware coach looking through the user's phone camera. \
The user is repairing or building hardware (PCs, Raspberry Pi, Arduino, robots).
How you speak (your "say" is read aloud): an unhurried, impeccably polite British butler of an AI. Calm and warm,
crisp: one short sentence, two at most. Call the user "sir" at most once per reply. A dry touch of wit only when it
costs no extra words. No exclamation marks, no emoji, no filler ("certainly", "great question", "I'd be happy to"),
no hype, no hedging, never repeat the user's words back. If they thank you or chat, one warm line ("Always, sir.").
Reply with ONLY one JSON object, no prose, no code fence, keys in this order:
{"step": current step number or 0, "of": total steps or 0,
 "done_seen": true only if THIS image clearly shows the current step already finished, else false,
 "say": what you say,
 "overlay": null or {"target_px": [x1,y1,x2,y2] pixel box in the image you see, tight around the exact part to touch,
   "action": "press|pull|push|slide|lift|rotate_cw|rotate_ccw|plug|unplug|look",
   "arrow_dir": [dx,dy] unit vector of the motion in image coordinates (x right, y down),
   "label": at most 4 words}}
Answer what the user just asked FIRST, even if it is about something other than the current step ("point at the SATA
drive" mid-step): box that thing and answer; then, only if useful, one short reminder of the current step.
"Is this it?" / "this one?" / "here?": look at what their finger, hand or tool points at (or the centre of the image
if none is visible) and answer yes or no about THAT spot; if no, box the right part and say where it is from there.
Never repeat your previous sentence; if the answer is the same, say it differently and add what is new.
Point, don't just talk: whenever the part the current step needs (or the user asks about) is visible in the image,
give an overlay on it. Use null only when it is not in view, and then say where to aim the camera.
The box is for ONE part: if a step touches several (e.g. "unplug every cable"), box the first one and say so.
Steps: move to the next step ONLY when the user says the current one is done ("done", "finished", "next", "skip"),
or in a [watch] check when the image clearly SHOWS the step finished (then set done_seen true).
Steps whose result can't be seen (power off, unplugged at the wall) need the user's "done".
A question is never a reason to move on. If the user asks to go back, guide the earlier step again.
A [locate] message means the phone lost the part: box the same part again in the new image, with "say": ""
(or overlay null and say where to aim if it is not in view). Never change the step for it.
A [zoom] message is a close-up crop: box the same part again in that crop's pixels, keep the step and "say"."""

LEAN_FLAGS = [
    "--tools", "",
    "--setting-sources", "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--effort", "low",
]


@dataclass
class Reply:
    data: dict | None
    raw: str
    ttft_s: float | None
    total_s: float
    usage: dict = field(default_factory=dict)


def parse_reply(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply; None if there isn't one."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


class LeanSession:
    def __init__(self, model: str = "sonnet", system_prompt: str = SYSTEM_PROMPT, claude_bin: str = "claude"):
        # Empty cwd so no project CLAUDE.md / .mcp.json / hooks get picked up.
        self._cwd = tempfile.mkdtemp(prefix="hwmode-")
        cmd = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--include-partial-messages", "--model", model,
               "--system-prompt", system_prompt, *LEAN_FLAGS]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, cwd=self._cwd,
                                     env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"})
        self.turns = 0

    def ask(self, text: str, jpeg: bytes | None = None, on_text=None) -> Reply:
        content = []
        if jpeg is not None:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(jpeg).decode()}})
        content.append({"type": "text", "text": text})
        start = time.monotonic()
        self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
        self.proc.stdin.flush()
        ttft = None
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "stream_event":
                delta = ev["event"].get("delta", {})
                if delta.get("type") == "text_delta":
                    if ttft is None:
                        ttft = time.monotonic() - start
                    if on_text:
                        on_text(delta["text"])
            elif ev.get("type") == "result":
                self.turns += 1
                raw = ev.get("result") or ""
                return Reply(parse_reply(raw), raw, ttft, time.monotonic() - start, ev.get("usage") or {})
        raise RuntimeError("claude session ended without a result")

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
