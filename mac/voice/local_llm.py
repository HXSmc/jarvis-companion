"""OpenAI-compatible client for the local llama.cpp overlay fallback.

Drop-in sibling of lean.LeanSession for the lean.Conversations pool: text-only
chat against a keyless local server (POST <url>/v1/chat/completions, SSE
streaming), reusing lean.Reply/lean.LeanError. The client never sends any
credential header and only ever takes its URL from the process environment.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.parse

import httpx

from mac.voice import lean

LOCAL_PORT = 8410  # the local model front on the mini PC, the same host as the companion gateway
DEFAULT_MODEL = "qwen3.8-27b"
HISTORY_TOKEN_CAP = 24_000


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def default_url() -> str:
    """http://<companion gateway host>:LOCAL_PORT; "" when that gateway is this Mac or unset.
    No address is written here: this repo is public, the host comes from the Mac's setup env."""
    host = urllib.parse.urlsplit(os.environ.get("OVERLAY_COMPANION_URL", "")).hostname or ""
    if host in ("", "localhost", "::1") or host.startswith("127."):
        return ""
    return f"http://{'[' + host + ']' if ':' in host else host}:{LOCAL_PORT}"


def settings() -> dict:
    """Connection settings, read from the process env at every call (never cached)."""
    return {
        "url": (os.environ.get("OVERLAY_LOCAL_URL") or default_url()).rstrip("/"),
        "model": os.environ.get("OVERLAY_LOCAL_MODEL") or DEFAULT_MODEL,
        "ttft_s": _float_env("OVERLAY_LOCAL_TTFT_S", 120.0),
        "wall_s": _float_env("OVERLAY_LOCAL_WALL_S", 180.0),
    }


def trim_history(system: str, history: list[dict], text: str,
                 cap: int = HISTORY_TOKEN_CAP) -> list[dict]:
    """History with the oldest user+assistant pairs dropped until the rough token
    estimate (len(content) // 4 per message, system + history + new user) fits
    the cap. Pure: returns a new list, never mutates the input."""
    out = list(history)
    new_user = {"role": "user", "content": text}

    def _estimate(msgs: list[dict]) -> int:
        return len(system) // 4 + sum(len(m.get("content", "")) // 4 for m in msgs)

    while out and _estimate(out + [new_user]) > cap:
        out = out[2:]  # one user+assistant pair
    return out


class LocalSession:
    def __init__(self, system_prompt: str = ""):
        self.system_prompt = system_prompt
        self.turns = 0
        self.upstream: str | None = None
        self._history: list[dict] = []
        self._closed = False
        self._lock = threading.Lock()
        self._client = httpx.Client(trust_env=False)
        self._response: httpx.Response | None = None

    def ask(self, text: str, jpeg: bytes | None = None, on_text=None) -> lean.Reply:
        # jpeg is ignored: the local server is text-only, images are never sent.
        # One request at a time, like LeanSession: interleaved asks would tear
        # the SSE stream and the history.
        with self._lock:
            if self._closed:
                raise lean.LeanError("local_unavailable", "closed")
            cfg = settings()
            history = trim_history(self.system_prompt, self._history, text)
            messages = ([{"role": "system", "content": self.system_prompt}]
                        if self.system_prompt else []) + history \
                + [{"role": "user", "content": text}]
            payload = {"model": cfg["model"], "messages": messages, "stream": True,
                       "chat_template_kwargs": {"enable_thinking": False}}
            timeout = httpx.Timeout(connect=5.0, read=cfg["ttft_s"], write=10.0, pool=5.0)
            start = time.monotonic()
            try:
                self._response = None
                with self._client.stream("POST", cfg["url"] + "/v1/chat/completions",
                                         json=payload, timeout=timeout) as resp:
                    self._response = resp
                    self.upstream = resp.headers.get("X-LLM-Upstream")
                    if resp.status_code != 200:
                        raise lean.LeanError("local_unavailable",
                                             f"status={resp.status_code}")
                    pieces: list[str] = []
                    ttft: float | None = None
                    for line in resp.iter_lines():
                        if time.monotonic() - start > cfg["wall_s"]:
                            raise lean.LeanError("local_timeout", "wall")
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            ev = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = ev.get("choices") or []
                        delta = choices[0].get("delta", {}) if choices else {}
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            if ttft is None:
                                ttft = time.monotonic() - start
                            if on_text:
                                on_text(piece)
                            pieces.append(piece)
            except lean.LeanError:
                raise
            except Exception as exc:  # close() aborts in-flight asks with anything
                if self._closed:
                    raise lean.LeanError("local_unavailable", "closed")
                if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
                    raise lean.LeanError("local_timeout", type(exc).__name__)
                if isinstance(exc, httpx.HTTPError):
                    raise lean.LeanError("local_unavailable", type(exc).__name__)
                raise
            self._history = history + [{"role": "user", "content": text},
                                       {"role": "assistant", "content": "".join(pieces)}]
            self.turns += 1
            return lean.Reply(raw="".join(pieces), ttft_s=ttft,
                              total_s=time.monotonic() - start)

    def close(self):
        # No lock: it must abort an ask in flight, which holds the lock.
        self._closed = True
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass
        _abort_inflight(self._client)
        try:
            self._client.close()
        except Exception:
            pass


def _abort_inflight(client: httpx.Client) -> None:
    """Force-abort a request blocked on response headers: httpx 0.28 has no
    public abort and client.close() leaves active connections running, so
    shutdown the raw socket (the only thing that wakes a blocked reader).
    Best-effort over private attrs; getattr chains keep it a no-op elsewhere."""
    try:
        conns = list(client._transport._pool._connections)
    except AttributeError:
        return
    for conn in conns:
        sock = getattr(getattr(getattr(conn, "_connection", None), "_network_stream", None),
                       "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
