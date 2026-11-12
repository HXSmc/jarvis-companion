"""Wake mic choice (the "no response to Jarvis" root cause) and the /overlay/debug ring buffer."""

from __future__ import annotations

import importlib
import re
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from conftest import AUTH_HEADER
from gateway import overlay
from test_overlay_service import POOL_TEXTS, client, make_app, make_deps, reset_overlay_state, turn  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_debug_log():
    overlay.debug_log.clear()
    yield
    overlay.debug_log.clear()


def _wakeword(monkeypatch, mic=None):
    # openwakeword is a Mac-only dependency: stub it so the module imports in CI.
    fake = types.ModuleType("openwakeword.model")
    fake.Model = object
    monkeypatch.setitem(sys.modules, "openwakeword", types.ModuleType("openwakeword"))
    monkeypatch.setitem(sys.modules, "openwakeword.model", fake)
    if mic is None:
        monkeypatch.delenv("OVERLAY_MIC", raising=False)
    else:
        monkeypatch.setenv("OVERLAY_MIC", mic)
    sys.modules.pop("mac.voice.wakeword", None)
    return importlib.import_module("mac.voice.wakeword")


def test_wake_listens_on_system_default_mic(monkeypatch):
    """avfoundation index 0 was an iPhone Continuity mic on the owner's Mac: never a bare index."""
    argv = _wakeword(monkeypatch).ffmpeg_argv()
    assert argv[argv.index("-i") + 1] == ":default"
    argv = _wakeword(monkeypatch, mic="MacBook Air Microphone").ffmpeg_argv()
    assert argv[argv.index("-i") + 1] == ":MacBook Air Microphone"


def test_debug_turn_steps_newest_first(client):
    overlay.set_deps(make_deps(chunks=['{"say": "Paris."}\n',
                                       '{"card": {"type": "stat", "title": "Capital", "value": "Paris"}}\n'],
                               synth=lambda t: (b"\x01\x02", 24000)))
    # A reply just ended in c1: the wake turn continues it (no fresh-start row), in any test order.
    overlay._last_reply["c1"] = (overlay.get_deps().clock(), False)
    assert client.post("/overlay/wake", json={"type": "wake", "score": 0.91},
                       headers=AUTH_HEADER).status_code == 200
    turn(client, "what is the capital of France", audio_ms=1800, stt_final_ms=120)
    turn(client, "open Safari")  # no wake before it: its own turn

    data = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()
    assert len(data["turns"]) == 2
    newest, first = data["turns"]
    assert newest["steps"][0]["step"] == "voice"
    steps = {s["step"]: s for s in first["steps"]}
    assert [s["step"] for s in first["steps"]][:4] == ["wake", "voice", "transcribed", "intent"]
    assert steps["wake"]["score"] == 0.91
    assert steps["voice"]["audio_ms"] == 1800
    assert steps["transcribed"]["text"] == "what is the capital of France"
    assert steps["intent"]["kind"] == "answer"
    spoken = [s["text"] for s in first["steps"] if s["step"] == "spoke"]
    assert spoken[0] in POOL_TEXTS and spoken == [spoken[0], "Paris."]  # pool ack, then the answer
    assert all(s["audio_ms"] is not None for s in first["steps"] if s["step"] == "spoke")  # all voiced
    assert steps["done"]["cards"] == 1 and steps["done"]["error"] is None
    assert steps["done"]["first_audio_ms"] is not None


def test_debug_note_lands_on_the_waiting_wake(client):
    client.post("/overlay/wake", json={"score": 0.7}, headers=AUTH_HEADER)
    client.post("/overlay/debug/note", json={"step": "no_speech", "detail": "nothing heard"},
                headers=AUTH_HEADER)
    (only,) = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"]
    assert [s["step"] for s in only["steps"]] == ["wake", "no_speech"]


def test_debug_ring_keeps_last_50():
    for i in range(60):
        overlay.debug_log.wake(i / 100, "test")
    turns = overlay.debug_log.snapshot()["turns"]
    assert len(turns) == 50 and turns[0]["steps"][0]["score"] == 0.59


def test_debug_gates(client):
    remote = TestClient(make_app(), client=("203.0.113.5", 50000))
    assert remote.get("/overlay/debug").status_code == 403
    assert remote.get("/overlay/debug/turns", headers=AUTH_HEADER).status_code == 403
    assert client.get("/overlay/debug/turns").status_code == 401
    assert client.post("/overlay/debug/note", json={"step": "x"}).status_code == 401
    page = client.get("/overlay/debug")  # data-free shell: loopback only, token comes from the fragment
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Bearer" in page.text and "location.hash" in page.text


def test_wake_post_is_json_and_accepted(client, monkeypatch):
    """The wake POST had no JSON content type: FastAPI answered 422 and the listener thread died."""
    ww = _wakeword(monkeypatch)
    sent = []
    monkeypatch.setattr(ww.urllib.request, "urlopen", lambda req, timeout: sent.append(req))
    ww.notify_wake(1.0, dry_run=False, score=0.8)
    (req,) = sent
    assert req.get_header("Content-type") == "application/json"
    headers = {**AUTH_HEADER, "Content-Type": req.get_header("Content-type")}
    assert client.post("/overlay/wake", content=req.data, headers=headers).status_code == 200
    assert client.post("/overlay/wake", content=req.data, headers={
        **AUTH_HEADER, "Content-Type": "application/x-www-form-urlencoded"}).status_code == 422


def test_wake_service_agent_is_not_throttled():
    """Without ProcessType launchd throttles the agent and the wake word stops hearing words."""
    plist = (Path(__file__).resolve().parent.parent / "launchd/com.jarvis.overlay.service.plist").read_text()
    assert "<key>ProcessType</key>\n\t<string>Interactive</string>" in plist


def test_debug_turns_report_setup_booleans_only(client, monkeypatch, tmp_path):
    """jtools2 item 4: the page shows a red row when the `code` CLI is not on the service PATH."""
    monkeypatch.setattr(overlay.shutil, "which", lambda name: None)
    monkeypatch.delenv("OVERLAY_VAULT", raising=False)
    monkeypatch.delenv("OVERLAY_BARGE_IN", raising=False)
    data = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()
    assert data["setup"] == {"code_cli": False, "vault": False, "barge_in": False,
                             "barge_mode": "off", "sessions_access": False}
    monkeypatch.setattr(overlay.shutil, "which", lambda name: str(tmp_path / name))
    monkeypatch.setenv("OVERLAY_VAULT", str(tmp_path))
    assert client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["setup"] == {"code_cli": True,
                                                                                        "vault": True,
                                                                                        "vault_excluded": ["Secrets"],
                                                                                        "barge_in": False,
                                                                                        "barge_mode": "off",
                                                                                        "sessions_access": False}
    page = client.get("/overlay/debug").text
    assert "VS Code command line tool (code) missing: the editor will not scroll" in page and "code_cli === false" in page
    assert "`code`" not in page  # R1 design 6: textContent would show literal backticks
    assert "vault === false" in page and "Notes vault not set" in page


def test_debug_setup_vault_row_names_only(client, monkeypatch, tmp_path):
    """jfollow4 H: a set vault says which dirs are excluded by NAME; no path ever reaches the JSON."""
    monkeypatch.delenv("OVERLAY_BARGE_IN", raising=False)
    monkeypatch.setenv("OVERLAY_VAULT", str(tmp_path))
    data = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()
    assert data["setup"]["vault"] is True and data["setup"]["vault_excluded"] == ["Secrets"]
    assert str(tmp_path) not in data["setup"]  # booleans and names only, never the vault path
    page = client.get("/overlay/debug").text
    assert "Notes vault set; excluded:" in page
    assert "run the installer" in page  # sessions access not set: the page says how to fix it


def test_setup_rows_have_page_padding(client):
    """jfollow item 8: the red setup rows line up with the turn cards (not flush at x=0)."""
    page = client.get("/overlay/debug").text
    rule = re.search(r"#setup\s*\{([^}]*)\}", page)
    inside_main = re.search(r"<main[^>]*>.*id=\"setup\"", page, re.S)
    assert inside_main or (rule and re.search(r"padding:\s*0\s+[1-9]\d*px", rule.group(1)))


def test_code_cli_setup_finds_the_bundled_cli(client, monkeypatch, tmp_path):
    """jfollow item 6: the bundled VS Code CLI counts; the JSON holds a boolean, never a path."""
    from mac.voice import gitdiff
    exe = tmp_path / "bundle" / "code"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(gitdiff.shutil, "which", lambda name: None)
    monkeypatch.setattr(gitdiff, "EDITOR_CANDIDATES", {"code": (str(exe),), "cursor": ()})
    resp = client.get("/overlay/debug/turns", headers=AUTH_HEADER)
    assert resp.json()["setup"]["code_cli"] is True and str(tmp_path) not in resp.text
    monkeypatch.setattr(gitdiff, "EDITOR_CANDIDATES", {"code": (str(tmp_path / "none"),), "cursor": ()})
    assert client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["setup"]["code_cli"] is False
