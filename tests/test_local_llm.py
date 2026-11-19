"""local_llm.LocalSession against the in-process fake server (tests/fixtures/fake_local_llm.py).

Plain sync tests; every OVERLAY_LOCAL_* value goes through monkeypatch.setenv so
no test reads the real box environment, and every test stops its fake server
via the context manager.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mac.voice import lean, local_llm
from tests.fixtures.fake_local_llm import FakeLocalLLM


def _env(monkeypatch, url, model=None, ttft_s=None, wall_s=None) -> None:
    monkeypatch.setenv("OVERLAY_LOCAL_URL", url)
    if model is not None:
        monkeypatch.setenv("OVERLAY_LOCAL_MODEL", model)
    if ttft_s is not None:
        monkeypatch.setenv("OVERLAY_LOCAL_TTFT_S", str(ttft_s))
    if wall_s is not None:
        monkeypatch.setenv("OVERLAY_LOCAL_WALL_S", str(wall_s))


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_streams_and_returns_reply(monkeypatch):
    with FakeLocalLLM(reply="Hello there.") as srv:
        _env(monkeypatch, srv.url)
        session = local_llm.LocalSession("be brief")
        pieces = []
        reply = session.ask("hi", on_text=pieces.append)
        assert "".join(pieces) == "Hello there."
        assert reply.raw == "Hello there."
        assert isinstance(reply.ttft_s, float)
        assert session.turns == 1
        session.close()


def test_request_url_model_and_body(monkeypatch):
    with FakeLocalLLM() as srv:
        _env(monkeypatch, srv.url, model="test-model")
        session = local_llm.LocalSession("be brief")
        session.ask("hi")
        session.close()
        req = srv.requests[-1]
        assert req["path"] == "/v1/chat/completions"
        assert req["body"]["model"] == "test-model"
        assert req["body"]["stream"] is True
        assert req["body"]["messages"][0] == {"role": "system", "content": "be brief"}


def test_no_credentials_sent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    with FakeLocalLLM() as srv:
        _env(monkeypatch, srv.url)
        session = local_llm.LocalSession("be brief")
        session.ask("hi")
        session.close()
        req = srv.requests[-1]
        assert not any(h in req["headers"] for h in ("authorization", "x-api-key", "cookie"))
        blob = json.dumps(req["headers"]) + json.dumps(req["body"])
        assert "ANTHROPIC" not in blob and "sk-test" not in blob


def test_jpeg_ignored(monkeypatch):
    with FakeLocalLLM() as srv:
        _env(monkeypatch, srv.url)
        session = local_llm.LocalSession("be brief")
        session.ask("hi", jpeg=b"\xff\xd8xx")
        session.close()
        body = srv.requests[-1]["body"]
        assert "image_url" not in json.dumps(body)
        assert "base64" not in json.dumps(body)
        assert all(isinstance(m["content"], str) for m in body["messages"])


def test_upstream_header_captured(monkeypatch):
    with FakeLocalLLM(upstream="127.0.0.1:8411") as srv:
        _env(monkeypatch, srv.url)
        session = local_llm.LocalSession()
        assert session.upstream is None
        session.ask("hi")
        assert session.upstream == "127.0.0.1:8411"
        session.close()


def test_history_multi_turn(monkeypatch):
    with FakeLocalLLM() as srv:
        _env(monkeypatch, srv.url)
        session = local_llm.LocalSession("be brief")
        session.ask("first")
        session.ask("second")
        session.close()
        roles = [(m["role"], m["content"]) for m in srv.requests[-1]["body"]["messages"]]
        assert roles == [("system", "be brief"), ("user", "first"),
                         ("assistant", "Hello there."), ("user", "second")]


def test_trim_history_drops_oldest_pairs():
    history = [{"role": "user", "content": "u1" * 40}, {"role": "assistant", "content": "a1" * 40},
               {"role": "user", "content": "u2" * 40}, {"role": "assistant", "content": "a2" * 40}]
    snapshot = [dict(m) for m in history]
    out = local_llm.trim_history("sys", history, "new question", cap=50)
    assert [m["content"] for m in out] == ["u2" * 40, "a2" * 40]
    assert history == snapshot  # input not mutated
    assert out is not history
    # The newest user text is never dropped even when it alone busts the cap.
    tiny = local_llm.trim_history("sys", [{"role": "user", "content": "x" * 400}], "new", cap=1)
    assert tiny == []


def test_503_raises_unavailable(monkeypatch):
    with FakeLocalLLM(mode="503") as srv:
        _env(monkeypatch, srv.url)
        session = local_llm.LocalSession("be brief")
        try:
            session.ask("hi")
            raise AssertionError("expected LeanError")
        except lean.LeanError as exc:
            assert exc.reason == "local_unavailable"
        # History unchanged: the failed ask sent system + user only, no assistant follow-up.
        assert [m["role"] for m in srv.requests[-1]["body"]["messages"]] == ["system", "user"]
        assert session.turns == 0
        session.close()


def test_500_raises_unavailable(monkeypatch):
    with FakeLocalLLM(mode="500") as srv:
        _env(monkeypatch, srv.url)
        session = local_llm.LocalSession("be brief")
        try:
            session.ask("hi")
            raise AssertionError("expected LeanError")
        except lean.LeanError as exc:
            assert exc.reason == "local_unavailable"
            assert session.upstream == srv.upstream  # header recorded on error status too
        session.close()


def test_connect_refused_unavailable(monkeypatch):
    _env(monkeypatch, f"http://127.0.0.1:{_closed_port()}")
    session = local_llm.LocalSession()
    try:
        session.ask("hi")
        raise AssertionError("expected LeanError")
    except lean.LeanError as exc:
        assert exc.reason == "local_unavailable"
    session.close()


def test_hang_times_out(monkeypatch):
    with FakeLocalLLM(mode="hang", hang_s=30.0) as srv:
        _env(monkeypatch, srv.url, ttft_s=0.5)
        session = local_llm.LocalSession()
        start = time.monotonic()
        try:
            session.ask("hi")
            raise AssertionError("expected LeanError")
        except lean.LeanError as exc:
            assert exc.reason == "local_timeout"
        assert time.monotonic() - start < 5.0
        session.close()


def test_close_aborts_and_is_idempotent(monkeypatch):
    with FakeLocalLLM(mode="hang", hang_s=60.0) as srv:
        _env(monkeypatch, srv.url)  # default long timeouts: only close() can end it
        session = local_llm.LocalSession()
        result = {}

        def _ask():
            try:
                session.ask("hi")
                result["outcome"] = "returned"
            except lean.LeanError as exc:
                result["outcome"] = exc.reason
            except Exception as exc:  # any other escape is a failure
                result["outcome"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=_ask, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while not srv.requests and time.monotonic() < deadline:
            time.sleep(0.02)  # the request reached the hanging server
        session.close()  # from another thread, while ask holds the lock
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert result["outcome"] == "local_unavailable"
        session.close()  # idempotent
        try:
            session.ask("again")
            raise AssertionError("expected LeanError")
        except lean.LeanError as exc:
            assert exc.reason == "local_unavailable"


def test_settings_read_at_call_time(monkeypatch):
    for name in ("OVERLAY_LOCAL_URL", "OVERLAY_LOCAL_MODEL", "OVERLAY_LOCAL_TTFT_S", "OVERLAY_LOCAL_WALL_S"):
        monkeypatch.delenv(name, raising=False)
    names = {"minipc.example": ("10.0.0.5",), "jarvis.example.com": ("8.8.8.8",), "split.example": ("10.0.0.5", "8.8.8.8")}
    monkeypatch.setattr(local_llm, "_resolve", lambda host: names.get(host, ()))
    monkeypatch.setenv("OVERLAY_COMPANION_URL", "https://minipc.example:8790")  # a tailnet-style name
    first = local_llm.settings()
    assert first == {"url": "http://minipc.example:8410", "model": local_llm.DEFAULT_MODEL,
                     "ttft_s": 420.0, "wall_s": 600.0}
    monkeypatch.setenv("OVERLAY_COMPANION_URL", "https://10.0.0.5:8790")
    assert local_llm.settings()["url"] == "http://10.0.0.5:8410"  # a non-global IP: private network
    # Gateway on this Mac, unset, or a public host (plain http would cross the internet): no local model.
    for off in ("http://127.0.0.1:8787", "http://localhost:8787", "", "https://jarvis.example.com:8790",
                "https://8.8.8.8:8790", "https://split.example:8790", "https://unresolved.example:8790"):
        monkeypatch.setenv("OVERLAY_COMPANION_URL", off)
        assert local_llm.settings()["url"] == "", off
    _env(monkeypatch, "http://127.0.0.1:9999/", model="m2", ttft_s="bad", wall_s="1.5")
    second = local_llm.settings()
    assert second["url"] == "http://127.0.0.1:9999"  # trailing / stripped
    assert second["model"] == "m2"
    assert second["ttft_s"] == 420.0  # non-numeric falls back
    assert second["wall_s"] == 1.5
