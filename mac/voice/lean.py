"""Lean persistent Claude session + per-conversation LRU for the overlay brain.

Copy of the hwmode/brain.py LeanSession pattern (stream-json in/out, partial
messages, stripped flags, scratch cwd, ANTHROPIC_API_KEY dropped) with one
extra `tools` argument, hard-allowlisted to {"WebSearch", "WebFetch"}: the
argv is built by the pure `lean_argv`, so a disallowed tool string raises
ValueError before any process can start. Conversations keeps one session per
conv_id and closes the sessions it evicts (LRU, max 2).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

ALLOWED_TOOLS = frozenset({"WebSearch", "WebFetch"})


@dataclass
class Reply:
    raw: str
    ttft_s: float | None
    total_s: float


class LeanError(RuntimeError):
    """The claude CLI ended a turn with a failure instead of an answer.

    `reason` is one of usage_limit | not_signed_in | error; `detail` is the raw
    CLI text for the log only (never for the voice or a card).
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# Classification reads documented stream-json fields first, message text last.
# Source: https://code.claude.com/docs/en/agent-sdk/typescript (SDKAssistantMessage `error`,
# SDKResultMessage `api_error_status`, SDKRateLimitEvent `rate_limit_info.status`) and
# https://code.claude.com/docs/en/headless ("missing authentication ... printed as the result").
_USAGE_ERRORS = frozenset({"rate_limit", "billing_error"})
_AUTH_ERRORS = frozenset({"authentication_failed", "oauth_org_not_allowed"})
# Fallback text, observed from claude 2.1.322 with an empty CLAUDE_CONFIG_DIR: the result
# carried "Not logged in · Please run /login" (is_error true, subtype success).
_AUTH_TEXT = "not logged in"


def classify_failure(assistant_error: str | None, status: int | None, rate_rejected: bool,
                     text: str) -> str:
    """usage_limit | not_signed_in | error, from documented fields; unknown shape -> error."""
    if assistant_error in _AUTH_ERRORS or status in (401, 403):
        return "not_signed_in"
    if assistant_error in _USAGE_ERRORS or rate_rejected or status == 429:
        return "usage_limit"
    if _AUTH_TEXT in (text or "").lower():
        return "not_signed_in"
    return "error"


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
            stderr=subprocess.PIPE, text=True,
            cwd=self._cwd,
            env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"},
        )
        self.turns = 0
        self._lock = threading.Lock()
        # Bounded stderr tail (about 2 KB) for the failure log; drained so the pipe never fills.
        self._err_tail: deque[str] = deque(maxlen=20)
        err = getattr(self.proc, "stderr", None)
        if err is not None:
            threading.Thread(target=self._drain, args=(err,), daemon=True).start()

    def _drain(self, err) -> None:
        try:
            for line in err:
                self._err_tail.append(line.strip()[:100])
        except (OSError, ValueError):
            pass

    def ask(self, text: str, jpeg: bytes | None = None, on_text=None) -> Reply:
        # One write+read pair at a time: interleaved asks on the same session
        # would interleave their stream-json lines and corrupt every reply.
        with self._lock:
            content = []
            if jpeg is not None:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                            "data": base64.b64encode(jpeg).decode()}})
            content.append({"type": "text", "text": text})
            start = time.monotonic()
            self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
            self.proc.stdin.flush()
            ttft = None
            assistant_error = None
            rate_rejected = False
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
                elif ev.get("type") == "assistant":
                    assistant_error = ev.get("error") or assistant_error
                elif ev.get("type") == "rate_limit_event":
                    info = ev.get("rate_limit_info")
                    rate_rejected = rate_rejected or (isinstance(info, dict) and info.get("status") == "rejected")
                elif ev.get("type") == "result":
                    self.turns += 1
                    raw = ev.get("result") or ""
                    status = ev.get("api_error_status")
                    if ev.get("is_error") is True or not (ev.get("subtype") in (None, "success")):
                        detail = f"is_error={ev.get('is_error')} subtype={ev.get('subtype')} status={status} " \
                                 f"error={assistant_error} rate_rejected={rate_rejected} text={raw[:300]}"
                        raise LeanError(classify_failure(assistant_error, status if isinstance(status, int) else None,
                                                         rate_rejected, raw), detail)
                    return Reply(raw, ttft, time.monotonic() - start)
            code = self.proc.poll()
            tail = " | ".join(self._err_tail)
            raise LeanError(classify_failure(assistant_error, None, rate_rejected, ""),
                            f"claude session ended without a result (exit={code}) stderr={tail[:300]}")

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Conversations:
    """One session per conv_id, LRU-capped at 2; evicted sessions are closed."""

    def __init__(self, factory: Callable[[], object], max_sessions: int = 2):
        self._factory = factory
        self._max = max_sessions
        self._lock = threading.Lock()  # guards _sessions and _spare; never held across close()
        self._sessions: OrderedDict[str, object] = OrderedDict()
        self._spare = None

    def prewarm(self) -> None:
        """Start one spare session now so a new conversation skips the CLI cold start."""
        with self._lock:
            if self._spare is None:
                self._spare = self._factory()

    def get(self, conv_id: str):
        evicted = []
        with self._lock:
            if conv_id in self._sessions:
                self._sessions.move_to_end(conv_id)
                return self._sessions[conv_id]
            session, self._spare = (self._spare or self._factory()), None
            self._spare = self._factory()  # next spare, inline: the lock is not reentrant
            self._sessions[conv_id] = session
            while len(self._sessions) > self._max:
                evicted.append(self._sessions.popitem(last=False)[1])
        self._close_later(evicted)
        return session

    def __contains__(self, conv_id: object) -> bool:
        """True while conv_id has a live session; never starts one."""
        with self._lock:
            return conv_id in self._sessions

    def drop(self, conv_id: str) -> None:
        """Forget (and close) a conversation's session, e.g. after its process died."""
        with self._lock:
            session = self._sessions.pop(conv_id, None)
        if session is not None:
            self._close_later([session])

    @staticmethod
    def _close_later(sessions: list) -> None:
        """Close detached sessions on a daemon thread: close() can block up to 10 s."""
        if sessions:
            threading.Thread(target=lambda: [s.close() for s in sessions], daemon=True).start()
