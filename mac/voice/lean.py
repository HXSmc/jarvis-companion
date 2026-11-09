"""Lean persistent Claude session + per-conversation LRU for the overlay brain.

Copy of the hwmode/brain.py LeanSession pattern (stream-json in/out, partial
messages, stripped flags, scratch cwd, ANTHROPIC_API_KEY dropped) with one
extra `tools` argument, hard-allowlisted to {"WebSearch", "WebFetch"}: the
argv is built by the pure `lean_argv`, so a disallowed tool string raises
ValueError before any process can start. Conversations keeps one session per
conv_id and closes the sessions it evicts (LRU, max 8).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

ALLOWED_TOOLS = frozenset({"WebSearch", "WebFetch"})


@dataclass
class Reply:
    raw: str
    ttft_s: float | None
    total_s: float


def lean_argv(model: str, system_prompt: str, tools: str = "", claude_bin: str = "claude") -> list[str]:
    """The claude argv for one lean session; ValueError on any tool outside the allowlist."""
    if tools and any(t not in ALLOWED_TOOLS for t in tools.split(",")):
        raise ValueError(f"tool not allowlisted: {tools!r}")
    return [
        claude_bin, "-p",
        "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "--include-partial-messages",
        "--model", model, "--system-prompt", system_prompt,
        "--tools", tools,
        # -p mode denies tool calls nobody approved: pre-approve exactly the allowlisted ones.
        *(["--allowedTools", tools] if tools else []),
        "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
        "--no-session-persistence", "--effort", "low",
    ]


class LeanSession:
    def __init__(
        self,
        model: str = "sonnet",
        system_prompt: str = "",
        tools: str = "",
        claude_bin: str = "claude",
    ):
        # Empty scratch cwd so no project CLAUDE.md / .mcp.json / hooks get picked up.
        self._cwd = tempfile.mkdtemp(prefix="jov-overlay-")
        self.proc = subprocess.Popen(
            lean_argv(model, system_prompt, tools, claude_bin),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            cwd=self._cwd,
            env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"},
        )
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
                return Reply(raw, ttft, time.monotonic() - start)
        raise RuntimeError("claude session ended without a result")

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Conversations:
    """One session per conv_id, LRU-capped at 8; evicted sessions are closed."""

    def __init__(self, factory: Callable[[], object], max_sessions: int = 8):
        self._factory = factory
        self._max = max_sessions
        self._sessions: OrderedDict[str, object] = OrderedDict()
        self._spare = None

    def prewarm(self) -> None:
        """Start one spare session now so a new conversation skips the CLI cold start."""
        if self._spare is None:
            self._spare = self._factory()

    def get(self, conv_id: str):
        if conv_id in self._sessions:
            self._sessions.move_to_end(conv_id)
            return self._sessions[conv_id]
        session, self._spare = (self._spare or self._factory()), None
        self.prewarm()
        self._sessions[conv_id] = session
        while len(self._sessions) > self._max:
            _, evicted = self._sessions.popitem(last=False)
            evicted.close()
        return session

    def drop(self, conv_id: str) -> None:
        """Forget (and close) a conversation's session, e.g. after its process died."""
        session = self._sessions.pop(conv_id, None)
        if session is not None:
            session.close()
