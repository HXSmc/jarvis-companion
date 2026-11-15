"""scripts/jarvis-steps-hook: run as a real subprocess with hook JSON on stdin.

A stub HTTP server records what the hook posts. HOME points at a temp vault, so
the real one is never read; the no-secret case uses a HOME without a vault.
Every run must exit 0 with empty stdout and stderr.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "scripts" / "jarvis-steps-hook"
SECRET = "jarvis_test_token_123"


@pytest.fixture
def gateway():
    """A stub /cards/event that stores (path, auth header, body) per request."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length") or 0)
            seen.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(size))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    server.server_close()


@pytest.fixture
def home(tmp_path):
    vault = tmp_path / "home" / "Desktop" / "ObsidianVault" / "Secrets"
    vault.mkdir(parents=True)
    (vault / "Projects.md").write_text(f"## Jarvis Companion\ntoken: {SECRET}\n")
    return tmp_path / "home"


def run(stdin: bytes, url: str, home: Path, **env) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "JARVIS_GATEWAY_DIR": str(ROOT),
            "JARVIS_GATEWAY_LOCAL": url, **env}
    out = subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, env=full, timeout=10)
    assert (out.returncode, out.stdout, out.stderr) == (0, b"", b"")
    return out


def payload(tool, tool_input, tool_response=None) -> bytes:
    return json.dumps({"session_id": "uuid-s-run", "hook_event_name": "PostToolUse", "tool_name": tool,
                       "tool_input": tool_input, "tool_response": tool_response}).encode()


def test_steps_hook_task_create_posts_a_task_upsert(gateway, home):
    url, seen = gateway
    run(payload("TaskCreate", {"subject": "Render hook video", "description": "d", "activeForm": "Rendering"},
                {"task": {"id": "3", "subject": "Render hook video"}}), url, home)
    assert seen == [("/cards/event", f"Bearer {SECRET}",
                     {"session": "uuid-s-run", "action": "steps", "op": "task", "key": "3",
                      "names": ["Render hook video"], "detail": "Rendering", "status": "todo"})]


def test_steps_hook_task_update_maps_the_status(gateway, home):
    url, seen = gateway
    for status, want in (("in_progress", "active"), ("completed", "done"), ("pending", "todo"),
                         ("deleted", "deleted")):
        run(payload("TaskUpdate", {"taskId": "2", "status": status}, {"success": True}), url, home)
        assert seen[-1][2] == {"session": "uuid-s-run", "action": "steps", "op": "task", "key": "2",
                               "status": want}
    run(payload("TaskUpdate", {"taskId": "2", "subject": "New name", "activeForm": "Naming"}), url, home)
    assert seen[-1][2]["names"] == ["New name"] and seen[-1][2]["detail"] == "Naming"
    assert seen[-1][2]["status"] == ""


def test_steps_hook_todowrite_posts_the_whole_list_as_tasks(gateway, home):
    url, seen = gateway
    todos = [{"content": "Plan", "status": "completed", "activeForm": "Planning"},
             {"content": "Build", "status": "in_progress", "activeForm": "Building"},
             {"content": "Test", "status": "pending", "activeForm": "Testing"}]
    run(payload("TodoWrite", {"todos": todos}), url, home)
    assert seen[-1][2] == {"session": "uuid-s-run", "action": "steps", "op": "set", "source": "tasks",
                           "names": ["Plan", "Build", "Test"], "statuses": ["done", "active", "todo"]}


def test_steps_hook_other_tools_post_nothing(gateway, home):
    url, seen = gateway
    run(payload("Bash", {"command": "ls"}, {"stdout": ""}), url, home)
    run(payload("TaskCreate", {"subject": "x"}, {"error": "failed"}), url, home)  # no task id
    assert seen == []


def test_steps_hook_junk_json_is_silent(gateway, home):
    url, seen = gateway
    run(b"{not json", url, home)
    run(b"[1, 2]", url, home)
    assert seen == []


def test_steps_hook_empty_stdin_is_silent(gateway, home):
    url, seen = gateway
    run(b"", url, home)
    assert seen == []


def test_steps_hook_gateway_down_is_silent_and_fast(home):
    with socket.socket() as hung:  # accepts the connection, never answers
        hung.bind(("127.0.0.1", 0))
        hung.listen(1)
        for url in ("http://127.0.0.1:9", f"http://127.0.0.1:{hung.getsockname()[1]}"):
            start = time.monotonic()
            run(payload("TaskUpdate", {"taskId": "1", "status": "completed"}), url, home)
            assert time.monotonic() - start < 2.5


def test_steps_hook_without_a_secret_is_silent(gateway, tmp_path):
    url, seen = gateway
    (tmp_path / "nohome").mkdir()
    run(payload("TaskUpdate", {"taskId": "1", "status": "completed"}), url, tmp_path / "nohome")
    assert seen == []


def test_steps_hook_capture_appends_raw_stdin_privately(gateway, home, tmp_path):
    url, _ = gateway
    cap = tmp_path / "cap.jsonl"
    first = payload("TaskUpdate", {"taskId": "1", "status": "completed"})
    run(first, url, home, JARVIS_STEPS_HOOK_CAPTURE=str(cap))
    run(b'{"tool_name": "Bash"}', url, home, JARVIS_STEPS_HOOK_CAPTURE=str(cap))
    assert cap.read_bytes().splitlines() == [first, b'{"tool_name": "Bash"}']
    assert stat.S_IMODE(cap.stat().st_mode) == 0o600


def test_steps_hook_only_posts_to_a_loopback_gateway(gateway, home):
    url, seen = gateway
    body = payload("TaskUpdate", {"taskId": "1", "status": "completed"})
    for bad in ("https://127.0.0.1:1", "http://example.com", "http://10.0.0.5:8788", "file:///etc/passwd"):
        run(body, bad, home)
    run(body, url.replace("127.0.0.1", "localhost"), home)
    assert len(seen) == 1  # only the localhost run reached the stub
