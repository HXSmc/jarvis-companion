"""jfollow10h: the overlay reads sessions and job results from the gateway that owns the cards.

scripts/companion_access.py (install step i3) probes the mini PC gateway, then loopback, and sets
OVERLAY_COMPANION_URL; the overlay runtime is unchanged and already takes an https upstream.
Network: real http.server threads on 127.0.0.1 (the fixture hosts are routed to them by a wrapper
around the module's own opener) and httpx.MockTransport for the overlay. No real host, no real token.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import http.server
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import companion_access as ca  # noqa: E402
from common import config  # noqa: E402
from gateway import overlay  # noqa: E402
from mac.voice import acks  # noqa: E402
from test_overlay_service import make_deps  # noqa: E402

FAKE = "not-a-real-token"
ROUTER = "https://router.example.invalid:8801"
CAND = "https://router.example.invalid:8790"
OTHER_LINES = b"# setup\nOVERLAY_VAULT=/vault/x\nMINIPC_ROUTER_URL=https://router.example.invalid:8801\n"


# --- local servers ------------------------------------------------------------------------------

class Server:
    """A real HTTP server on 127.0.0.1 answering `self.answer = (status, headers, body)`."""

    def __init__(self):
        self.answer = (200, {}, b"[]")
        self.requests = []  # (path, Authorization header)
        self.served = 0  # body bytes written before the client went away
        self.delay = 0.0
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                srv.requests.append((self.path, self.headers.get("Authorization")))
                time.sleep(srv.delay)
                status, headers, body = srv.answer
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for i in range(0, len(body), 8192):
                        self.wfile.write(body[i:i + 8192])
                        self.wfile.flush()
                        srv.served += len(body[i:i + 8192])
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Route:
    """Wraps the module's real opener: the fixture https origins go to local http servers."""

    def __init__(self, real):
        self.real, self.map, self.timeouts, self.urls = real, {}, [], []

    def open(self, req, timeout=None):
        self.timeouts.append(timeout)
        self.urls.append(req.full_url)
        for origin, target in self.map.items():
            if req.full_url.startswith(origin + "/"):
                req.full_url = target + req.full_url[len(origin):]
        return self.real.open(req, timeout=timeout)


@pytest.fixture
def servers():
    made = []

    def make():
        made.append(Server())
        return made[-1]
    yield make
    for s in made:
        s.close()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A temp HOME with a vault holding the fake ios_token, a 0600 .env, a token-file path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    vault = Path(os.path.expanduser(config.DEFAULT_VAULT_PATH))
    vault.parent.mkdir(parents=True)
    vault.write_text(f"## Jarvis Companion\nios_token: {FAKE}\n")
    monkeypatch.setenv("MINIPC_ROUTER_URL", ROUTER)
    env = tmp_path / "repo.env"
    env.write_bytes(OTHER_LINES)
    env.chmod(0o600)
    route = Route(ca._OPENER)
    monkeypatch.setattr(ca, "_OPENER", route)
    return {"env": env, "token": tmp_path / "support" / "companion-token", "route": route}


def run(setup, port, *extra):
    setup["token"].parent.mkdir(exist_ok=True)
    return ca.main([str(setup["token"]), str(setup["env"]), str(port), *extra])


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "missing"


def url_lines(env: Path) -> list[bytes]:
    return [ln for ln in env.read_bytes().splitlines() if ln.startswith(b"OVERLAY_COMPANION_URL=")]


def others(env: Path) -> list[bytes]:
    return [ln for ln in env.read_bytes().splitlines(keepends=True) if not ln.startswith(b"OVERLAY_COMPANION_URL=")]


# --- criterion 5: the candidate -------------------------------------------------------------------

BAD = ["http://router.example.invalid:8801", "", "https://user:pw@router.example.invalid",
       "https://router.example.invalid@evil.example.invalid", "https:///x", "https://ro%75ter.example.invalid:1",
       "https://router/x.example.invalid", "https://router_x.example.invalid:1", "ftp://router.example.invalid:1"]


def test_candidate_from_router_url(setup, monkeypatch, capsys):
    assert ca.https_origin(ROUTER, ca.MINIPC_GATEWAY_PORT) == CAND
    assert ca.https_origin("https://router.example.invalid", 8790) == CAND
    for bad in BAD:
        assert ca.https_origin(bad, 8790) is None, bad
    port = dead_port()
    for bad in BAD + [None]:
        if bad is None:
            monkeypatch.delenv("MINIPC_ROUTER_URL")
        else:
            monkeypatch.setenv("MINIPC_ROUTER_URL", bad)
        setup["route"].urls.clear()
        assert run(setup, port) == 4  # no candidate -> loopback only (down here)
        assert setup["route"].urls == [f"http://127.0.0.1:{port}/central/sessions"], bad
    # --companion-url: bad forms stop before any request (and before the vault is read)
    for bad in BAD + ["https://gw.example.invalid:8790/x", "https://gw.example.invalid:8790?a=1",
                      "https://gw.example.invalid:abc", "https://gw.example.invalid", "https://gw.example.invalid:0"]:
        setup["route"].urls.clear()
        assert run(setup, port, bad) == 2, bad
        assert ca.main(["--check-url", bad]) == 2, bad
        assert setup["route"].urls == []
    # a good --companion-url wins over the router-derived host
    monkeypatch.setenv("MINIPC_ROUTER_URL", ROUTER)
    setup["route"].urls.clear()
    assert ca.main(["--check-url", "https://gw.example.invalid:8790"]) == 0
    assert run(setup, port, "https://gw.example.invalid:8790") == 4
    assert setup["route"].urls[0] == "https://gw.example.invalid:8790/central/sessions"
    assert not any("router.example.invalid" in u for u in setup["route"].urls)


# --- criteria 6-11: probe, switch, fail closed ----------------------------------------------------

def test_probe_switches_to_minipc(setup, servers, capsys):
    mini = servers()
    setup["route"].map[CAND] = mini.url
    port = dead_port()  # the Mac today: the installer's own loopback line
    setup["env"].write_bytes(OTHER_LINES + f"OVERLAY_COMPANION_URL=http://127.0.0.1:{port}\n".encode())
    before = others(setup["env"])
    assert run(setup, port) == 0
    assert capsys.readouterr().out == "Sessions access: set (mini PC gateway)\n"
    assert mini.requests == [("/central/sessions", f"Bearer {FAKE}")]
    assert setup["token"].read_text() == FAKE and setup["token"].stat().st_mode & 0o777 == 0o600
    assert url_lines(setup["env"]) == [f"OVERLAY_COMPANION_URL={CAND}".encode()]
    assert others(setup["env"]) == before
    assert setup["env"].stat().st_mode & 0o777 == 0o600


def test_probe_refuses_redirect(setup, servers, capsys, monkeypatch):
    a, b = servers(), servers()
    a.answer = (302, {"Location": b.url + "/"}, b"")
    real = setup["route"].real
    monkeypatch.setattr(ca, "_OPENER", real)  # the module's real probe() and bare opener, no wrapper
    assert ca.probe(a.url, FAKE) is False
    assert len(a.requests) == 1 and b.requests == []
    monkeypatch.setattr(ca, "_OPENER", setup["route"])
    # the full flow: the candidate redirects -> the loopback gateway
    loop = servers()
    setup["route"].map[CAND] = a.url
    assert run(setup, loop.port) == 0
    assert capsys.readouterr().out == "Sessions access: set (local gateway)\n"
    assert b.requests == [] and len(loop.requests) == 1
    assert url_lines(setup["env"]) == [f"OVERLAY_COMPANION_URL={loop.url}".encode()]
    # loopback down too -> exit 4, nothing written; the redirect target never lands in .env
    env_sha, tok_sha = sha(setup["env"]), sha(setup["token"])
    assert run(setup, dead_port()) == 4
    assert sha(setup["env"]) == env_sha and sha(setup["token"]) == tok_sha
    assert b.url.encode() not in setup["env"].read_bytes() and b.requests == []
    assert not any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in real.handlers)


def test_probe_fail_closed(setup, servers, capsys, monkeypatch):
    mini, loop = servers(), servers()
    setup["route"].map[CAND] = mini.url
    monkeypatch.setattr(ca, "PROBE_TIMEOUT_S", 0.5)
    cases = [(401, b'{"detail":"no"}'), (500, b"[]"), (200, b"not json"), (200, b'{"rows": []}')]
    for status in cases + ["refused", "timeout"]:
        if isinstance(status, tuple):
            mini.answer, mini.delay = (status[0], {}, status[1]), 0.0
            setup["route"].map[CAND] = mini.url
        elif status == "refused":
            setup["route"].map[CAND] = f"http://127.0.0.1:{dead_port()}"
        else:
            mini.answer, mini.delay = (200, {}, b"[]"), 1.5
            setup["route"].map[CAND] = mini.url
        setup["env"].write_bytes(OTHER_LINES)
        assert run(setup, loop.port) == 0, status
        assert capsys.readouterr().out == "Sessions access: set (local gateway)\n", status
        assert url_lines(setup["env"]) == [f"OVERLAY_COMPANION_URL={loop.url}".encode()]
    # both down -> exit 4, token file and .env unchanged
    setup["route"].map[CAND] = f"http://127.0.0.1:{dead_port()}"
    env_sha, tok_sha = sha(setup["env"]), sha(setup["token"])
    assert run(setup, dead_port()) == 4
    assert sha(setup["env"]) == env_sha and sha(setup["token"]) == tok_sha
    assert capsys.readouterr().out == ""


def test_owner_value_kept(setup, servers, capsys):
    mini = servers()
    setup["route"].map[CAND] = mini.url
    owner = OTHER_LINES + b"OVERLAY_COMPANION_URL=https://other.example.invalid:9999\n"
    setup["env"].write_bytes(owner)
    assert run(setup, 8788) == 0
    assert capsys.readouterr().out == "Sessions access: kept\n" and setup["env"].read_bytes() == owner
    setup["env"].write_bytes(OTHER_LINES)  # missing key -> appended once
    assert run(setup, 8788) == 0 and setup["env"].read_bytes() == OTHER_LINES + f"OVERLAY_COMPANION_URL={CAND}\n".encode()
    mid = b"A=1\nOVERLAY_COMPANION_URL=http://127.0.0.1:8788\nB=2\n"  # own loopback value -> replaced in place
    setup["env"].write_bytes(mid)
    assert run(setup, 8788) == 0
    assert setup["env"].read_bytes() == f"A=1\nOVERLAY_COMPANION_URL={CAND}\nB=2\n".encode()
    assert capsys.readouterr().out.splitlines()[-1] == "Sessions access: set (mini PC gateway)"


def test_rerun_idempotent(setup, servers, capsys, monkeypatch):
    mini = servers()
    setup["route"].map[CAND] = mini.url
    assert run(setup, 8788) == 0
    first = sha(setup["env"])
    assert run(setup, 8788) == 0
    assert capsys.readouterr().out.splitlines() == ["Sessions access: set (mini PC gateway)"] * 2
    assert sha(setup["env"]) == first
    monkeypatch.delenv("MINIPC_ROUTER_URL")  # no candidate, the https line present -> kept
    loop = servers()
    assert run(setup, loop.port) == 0
    assert capsys.readouterr().out == "Sessions access: kept\n" and sha(setup["env"]) == first
    # no trailing newline + a duplicated installer-owned key -> exactly one line after
    monkeypatch.setenv("MINIPC_ROUTER_URL", ROUTER)
    setup["env"].write_bytes(b"OVERLAY_COMPANION_URL=http://127.0.0.1:8788\nA=1\nOVERLAY_COMPANION_URL=" + CAND.encode())
    assert run(setup, 8788) == 0
    assert setup["env"].read_bytes() == f"OVERLAY_COMPANION_URL={CAND}\nA=1\n".encode()


def test_token_never_printed(setup, servers, capsys, monkeypatch):
    """Criteria 6-9's flows again, every output and exception text checked for the token."""
    mini, loop = servers(), servers()
    texts = []
    setup["route"].map[CAND] = mini.url
    monkeypatch.setattr(ca, "PROBE_TIMEOUT_S", 0.5)
    for answer in [(200, {}, b"[]"), (302, {"Location": loop.url}, b""), (401, {}, b"no"), (200, {}, b"{")]:
        mini.answer = answer
        for port in (loop.port, dead_port()):
            try:
                run(setup, port)
            except BaseException as e:  # noqa: BLE001 (none expected; collected for the check)
                texts.append(repr(e))
    setup["env"].write_bytes(b"OVERLAY_COMPANION_URL=https://other.example.invalid:9999\n")
    run(setup, loop.port)
    out = capsys.readouterr()
    assert FAKE not in out.out + out.err + "".join(texts) and not texts
    # the installer passes only the paths, the port and the optional URL; the token never in argv/env
    lines = [ln for ln in (ROOT / "scripts/install-mac-overlay.sh").read_text().splitlines()
             if "companion_access.py" in ln]
    assert lines and not any(re.search(r"\$\{?(?!TOKEN_FILE\b)\w*(tok|TOKEN)", ln) or "TOKEN=" in ln for ln in lines)
    src = (ROOT / "scripts/companion_access.py").read_text()
    assert 'get_token(name="ios_token")' in src and "environ.get(\"OVERLAY" not in src


def test_probe_caps(setup, servers, monkeypatch):
    big = servers()
    big.answer = (200, {}, b"[" + b" " * (3 * 1024 * 1024))
    got = []
    real_read = http.client.HTTPResponse.read

    def counting_read(self, amt=None):
        data = real_read(self, amt)
        got.append(len(data))
        return data
    monkeypatch.setattr(http.client.HTTPResponse, "read", counting_read)
    assert ca.probe(big.url, FAKE) is True
    assert sum(got) <= 64 * 1024
    time.sleep(0.3)
    assert big.served < 3 * 1024 * 1024  # the client closed long before the end of the body
    monkeypatch.setattr(http.client.HTTPResponse, "read", real_read)
    for body in (b'{"a": []}', b"<html>[]</html>", b""):
        big.answer = (200, {}, body)
        assert ca.probe(big.url, FAKE) is False
    route = setup["route"]
    route.map[CAND] = big.url
    big.answer = (200, {}, b"[]")
    assert run(setup, dead_port()) == 0
    assert route.timeouts and all(t is not None and t <= 10 for t in route.timeouts)
    src = (ROOT / "scripts/companion_access.py").read_text()
    assert not re.search(r"CERT_NONE|check_hostname *= *False|_create_unverified_context|verify=False", src)
    assert ca.PROBE_TIMEOUT_S <= 10 and ca.PROBE_MAX == 64 * 1024


# --- criterion 15a: the installer's exact command line --------------------------------------------

def test_installer_command_line(tmp_path):
    line = next(ln.strip() for ln in (ROOT / "scripts/install-mac-overlay.sh").read_text().splitlines()
                if "umask 077 && python3 scripts/companion_access.py" in ln)
    assert line.startswith("(cd \"$REPO\" && umask 077 && python3 scripts/companion_access.py ")

    def go(repo: Path):
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "REPO": str(repo),
               "TOKEN_FILE": str(tmp_path / "companion-token"), "GATEWAY_PORT": str(dead_port()), "COMPANION_URL": ""}
        script = f"set -u\nRC=0\n{line}\necho RC=$RC\n"
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/companion_access.py", repo / "scripts")
    (repo / "common").symlink_to(ROOT / "common")
    res = go(repo)
    assert res.stdout.strip() in ("RC=3", "RC=4") and "Traceback" not in res.stderr
    assert not (tmp_path / "companion-token").exists() and not (repo / ".env").exists()
    (repo / "common").unlink()  # common unimportable -> a visible traceback, not exit 4
    res = go(repo)
    assert "Traceback" in res.stderr and res.stdout.strip() not in ("RC=3", "RC=4", "RC=0")


# --- criteria 13-14: the overlay over an https upstream -------------------------------------------

GW = "https://gw.example.invalid:8790"


@pytest.fixture
def https_upstream(monkeypatch, tmp_path):
    seen = []

    def use(handler):
        seen.clear()

        def wrap(request):
            seen.append(request)
            return handler(request)
        monkeypatch.setattr(overlay, "_sessions_transport", httpx.MockTransport(wrap))
        return seen
    monkeypatch.setenv("OVERLAY_COMPANION_URL", GW)
    token = tmp_path / "companion-token"
    token.write_text(FAKE)
    token.chmod(0o600)
    monkeypatch.setenv("OVERLAY_COMPANION_TOKEN_FILE", str(token))
    overlay.debug_log.clear()
    overlay._tainted.clear()
    overlay.set_deps(None)
    yield use, token
    overlay.set_deps(None)


def test_overlay_https_upstream(https_upstream):
    use, token = https_upstream
    assert overlay._sessions_access() == (GW, FAKE)
    seen = use(lambda r: httpx.Response(200, json=[{"session_id": "s1", "state": "done"}]))
    assert overlay._job_row("s1") == {"session_id": "s1", "state": "done"}
    seen = use(lambda r: httpx.Response(302, headers={"Location": "https://evil.example.invalid/x"}))
    assert overlay._companion_text("/central/cards/s1/files/f1") is None
    assert overlay._job_row("s1") is None
    assert len(seen) == 2 and {r.url.host for r in seen} == {"gw.example.invalid"}
    assert all(r.headers["Authorization"] == f"Bearer {FAKE}" and r.url.scheme == "https" for r in seen)
    seen = use(lambda r: httpx.Response(302, headers={"Location": "https://evil.example.invalid/x"}))
    assert overlay._companion_text("/central/cards/s1/files/f1") is None and len(seen) == 1
    token.chmod(0o644)
    assert overlay._sessions_access() is None


def _job(use, monkeypatch, files, texts):
    job = {"session_id": "sess-9", "state": "in_progress", "card": {"files": []}}
    polls = []

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"session_id": "sess-9"})
        if request.url.path == "/central/sessions":
            polls.append(1)
            if len(polls) >= 2:
                job["card"]["files"] = files
            return httpx.Response(200, json=[job])
        for fid, text in texts.items():
            if request.url.path == f"/central/cards/sess-9/files/{fid}":
                return httpx.Response(200, text=text)
        return httpx.Response(404)
    seen = use(handler)
    monkeypatch.setattr(overlay, "JOB_POLL_S", 0)
    pushed = []
    monkeypatch.setattr(overlay.bus, "publish", lambda kind, ev=None: pushed.append((kind, ev)))
    return seen, pushed


def _rows():
    return [s["detail"] for t in reversed(overlay.debug_log.snapshot()["turns"]) for s in t["steps"]
            if s["step"] == "watch_job"]


def test_job_runner_watch_over_https(https_upstream, monkeypatch):
    use, _token = https_upstream
    seen, pushed = _job(use, monkeypatch, [{"id": "f" * 32, "name": overlay.WATCH_RESULT}],
                        {"f" * 32: "- Rockets launch.\n- Four astronauts fly.\n"})
    asyncio.run(overlay._job_runner("watch", overlay.watch_job_prompt("aaaaaaaaaaa"), overlay.WATCH_RESULT,
                                    "h1", make_deps(apps={})))
    assert {r.url.host for r in seen} == {"gw.example.invalid"}
    card = next(ev["card"] for _k, ev in pushed if ev.get("type") == "card")
    assert card["title"] == "Video summary" and card["body"] == ["Rockets launch.", "Four astronauts fly."]
    assert any(ev.get("text") == acks.PHRASES["watch_ready"] for _k, ev in pushed)
    rows = _rows()
    assert rows[0].startswith("state=started id=sess-9") and rows[-1].startswith("state=done id=sess-9")


def test_job_runner_carries_3d_result(https_upstream, monkeypatch):
    use, _token = https_upstream
    files = [{"id": "a" * 32, "name": "jarvis-watch-summary.md"}, {"id": "b" * 32, "name": "jarvis-3d-result.md"}]
    seen, pushed = _job(use, monkeypatch, files, {"a" * 32: "- wrong file\n", "b" * 32: "- Model saved.\n"})
    asyncio.run(overlay._job_runner("blender", "Started by voice: fixed 3D job.", "jarvis-3d-result.md",
                                    "h2", make_deps(apps={})))
    gets = [r.url.path for r in seen if r.method == "GET" and "/files/" in r.url.path]
    assert gets == [f"/central/cards/sess-9/files/{'b' * 32}"]
    card = next(ev["card"] for _k, ev in pushed if ev.get("type") == "card")
    assert card["body"] == ["Model saved."]
    assert _rows()[-1].startswith("state=done id=sess-9")
