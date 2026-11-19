#!/usr/bin/env python3
"""A stand-in OpenAI-compatible local LLM front (llama.cpp style) for tests.

ThreadingHTTPServer on 127.0.0.1:0, stdlib only. Streams the reply as SSE
`data:` lines with `choices[0].delta.content` pieces and `data: [DONE]`, and
carries the `X-LLM-Upstream` header the client records. Modes: ok | 503 | 500
| hang; mode/reply/upstream/hang_s are plain attributes, changeable between
requests. Every request (including GET /health) is recorded under a lock.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    server: "_Server"

    def log_message(self, *args) -> None:  # silence the default stderr logging
        pass

    def _record(self, body) -> None:
        with self.server.owner._lock:
            self.server.owner.requests.append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            })

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    def _json_status(self, code: int, obj: dict, upstream: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if upstream is not None:
            self.send_header("X-LLM-Upstream", upstream)
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())
        self.wfile.flush()

    def _stream_ok(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-LLM-Upstream", self.server.owner.upstream)
        self.end_headers()
        reply = self.server.owner.reply
        step = max(1, len(reply) // 3)
        for i in range(0, len(reply), step):
            chunk = json.dumps({"choices": [{"delta": {"content": reply[i:i + step]}}]})
            self.wfile.write(f"data: {chunk}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_POST(self) -> None:
        self._record(self._read_body())
        if self.server.owner.mode == "ok":
            self._stream_ok()
        elif self.server.owner.mode == "503":
            self._json_status(503, {"error": "no backend behind the router"})
        elif self.server.owner.mode == "500":
            self._json_status(500, {"error": "refused"}, self.server.owner.upstream)
        elif self.server.owner.mode == "hang":
            deadline = time.monotonic() + self.server.owner.hang_s
            while time.monotonic() < deadline and not self.server.owner._stopping:
                time.sleep(0.05)
            if not self.server.owner._stopping:
                self._stream_ok()

    def do_GET(self) -> None:
        self._record(None)
        self._json_status(200, {"status": "ok"})


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer holding its FakeLocalLLM owner for the handler."""

    daemon_threads = True

    def __init__(self, owner: "FakeLocalLLM"):
        self.owner = owner
        super().__init__(("127.0.0.1", 0), _Handler)


class FakeLocalLLM:
    def __init__(self, mode: str = "ok", reply: str = "Hello there.",
                 upstream: str = "fast-pc:8411", hang_s: float = 30.0):
        self.mode = mode
        self.reply = reply
        self.upstream = upstream
        self.hang_s = hang_s
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._stopping = False
        self._httpd = _Server(self)

    def start(self) -> "FakeLocalLLM":
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def __enter__(self) -> "FakeLocalLLM":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
