"""C-HIGH-4 check: gateway URL resolution is env -> file -> error, never the
hardcoded PC loopback, and any configured base URL normalizes to the
role=windows WS URL. Pure config functions — no network."""

from __future__ import annotations

import asyncio

import pytest

from windows.agent import main as agent_main


@pytest.fixture(autouse=True)
def no_env_gateway(monkeypatch):
    monkeypatch.delenv("JARVIS_GATEWAY_URL", raising=False)


def _url_file(tmp_path, monkeypatch, content: str | None) -> None:
    path = tmp_path / "gateway_url"
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(agent_main, "gateway_url_file_path", lambda: path)


# --- resolution order: JARVIS_GATEWAY_URL env -> file first line -> None ----

def test_env_wins_over_file(monkeypatch, tmp_path):
    _url_file(tmp_path, monkeypatch, "https://from-file.<tld>\n")
    monkeypatch.setenv("JARVIS_GATEWAY_URL", "https://from-env.<tld>")
    assert agent_main.load_gateway_url() == "https://from-env.<tld>"


def test_file_first_line_used_when_no_env(monkeypatch, tmp_path):
    _url_file(tmp_path, monkeypatch, "https://mac.tail1234.<tld>\nsecond line ignored\n")
    assert agent_main.load_gateway_url() == "https://mac.tail1234.<tld>"


def test_missing_file_and_env_is_none(monkeypatch, tmp_path):
    _url_file(tmp_path, monkeypatch, None)  # file does not exist
    assert agent_main.load_gateway_url() is None


def test_empty_file_is_none(monkeypatch, tmp_path):
    _url_file(tmp_path, monkeypatch, "   \n")
    assert agent_main.load_gateway_url() is None


# --- _ws_url normalization --------------------------------------------------

@pytest.mark.parametrize("base, expected", [
    ("https://mac.tail1234.<tld>", "wss://mac.tail1234.<tld>/ws?role=windows"),
    ("https://mac.tail1234.<tld>:8790/", "wss://mac.tail1234.<tld>:8790/ws?role=windows"),
    ("http://<lan>.1.10:8788", "ws://<lan>.1.10:8788/ws?role=windows"),
    ("ws://127.0.0.1:8788", "ws://127.0.0.1:8788/ws?role=windows"),
    ("<lan>.1.10:8788", "ws://<lan>.1.10:8788/ws?role=windows"),  # bare host:port
    ("https://host/ignore/this/path", "wss://host/ws?role=windows"),  # path dropped
])
def test_ws_url_normalization(base, expected):
    assert agent_main._ws_url(base) == expected


def test_run_without_any_url_raises_system_exit(monkeypatch, tmp_path):
    """No env, no file: the agent refuses to start (never falls back to the
    PC's own 127.0.0.1 — C-HIGH-4)."""
    _url_file(tmp_path, monkeypatch, None)
    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(agent_main.run(token="some-token"))
    assert exc_info.value.code == 1
