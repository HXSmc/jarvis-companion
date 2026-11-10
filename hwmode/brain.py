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
{"next_ready": true only when this reply gives a NEW instruction because the user said the current one is done
   or the image shows it done, else false,
 "done_seen": true only if THIS image clearly shows the current instruction already finished, else false,
 "say": what you say,
 "shape": null or {"kind": "rect"|"circle"|"triangle", "box": [x1,y1,x2,y2] pixel box in the image you see,
   tight around the exact ONE part to touch, "label": at most 4 words}}
The shape kind: "rect" by default; "circle" for round parts (screws, round buttons, LEDs); "triangle" only to mark
a hazard. The phone draws the shape: never describe its colour or look in "say" ("outlined in red").
Instructions are given one at a time: never number them, never say how many there are, never say "step" with a
number.
Answer what the user just asked FIRST, even if it is about something other than the current instruction ("point at
the SATA drive" midway): box that thing and answer; then, only if useful, one short reminder of the current
instruction.
"Is this it?" / "this one?" / "here?": look at what their finger, hand or tool points at (or the centre of the image
if none is visible) and answer yes or no about THAT spot; if no, box the right part and say where it is from there.
Never repeat your previous sentence; if the answer is the same, say it differently and add what is new.
Point, don't just talk: whenever the part the current instruction needs (or the user asks about) is visible in the
image, give a shape on it. Use null only when it is not in view, and then say where to aim the camera.
The box is for ONE part: if an instruction touches several (e.g. "unplug every cable"), box the first one and say so.
Plan ONLY what the user asked for, nothing they did not ask. "Find / show me / where is / what is X" is the whole
job: that one instruction (point at it), no extra jobs (no unplugging, powering down, opening a case) unless they
ask for that.
Add a safety instruction (power off before unplugging) only when the asked job itself needs it. When the asked job
is done, say so in one line and give no more instructions: just answer questions.
Give a NEW instruction (next_ready true) ONLY when the user says the current one is done ("done", "finished",
"next", "skip"), or in a [watch] check when the image clearly SHOWS it finished (then also set done_seen true).
Instructions whose result can't be seen (power off, unplugged at the wall) need the user's "done".
A question is never a reason to move on. If the user asks to go back, guide the earlier instruction again.
A [locate] message means the phone lost the part: box the same part again in the new image, with "say": ""
(or shape null and say where to aim if it is not in view). Never give a new instruction for it.
A [zoom] message is a close-up crop: box the same part again in that crop's pixels, same JSON."""

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
    def __init__(self, model: str = "sonnet", system_prompt: str = SYSTEM_PROMPT, claude_bin: str = "claude",
                 tools: str = "", deny: tuple[str, ...] = ()):
        """`tools`: comma list of built-in tools this session may use without asking (the research
        turn's "WebSearch,WebFetch"); empty = no tools at all, the coaching brain's default."""
        # Empty cwd so no project CLAUDE.md / .mcp.json / hooks get picked up.
        self._cwd = tempfile.mkdtemp(prefix="hwmode-")
        flags = LEAN_FLAGS if not tools else ["--tools", tools, "--allowedTools", tools, *LEAN_FLAGS[2:]]
        cmd = [claude_bin, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--include-partial-messages", "--model", model,
               "--system-prompt", system_prompt, *flags]
        if deny:  # last: --disallowedTools takes every following argument as a rule
            cmd += ["--disallowedTools", *deny]
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
