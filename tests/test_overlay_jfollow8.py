"""jfollow8: barge-in answers on the speakers too (echo canceller default), a stale poster's
weak wake body never wakes anything, and a barge-in stop stays silent."""

from __future__ import annotations

import json
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from conftest import AUTH_HEADER  # noqa: E402
from gateway import overlay  # noqa: E402
from mac.voice import acks  # noqa: E402
from test_overlay_agent import BusRecorder, Clock, client, push_script, reset, setup  # noqa: F401,E402
from test_overlay_jfollow4 import _deps, _steps  # noqa: E402
from test_overlay_service import says, turn  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_debug_log():
    overlay.debug_log.clear()
    yield
    overlay.debug_log.clear()


# --- A: the echo canceller is the default barge mode (criterion 1) ---

def test_barge_mode_default_aec(client, monkeypatch):
    """No env: "aec" (speakers too). OVERLAY_BARGE_AEC=0 opts back to headphones only; any
    OVERLAY_BARGE_IN but "1" (here "0" and "2") is off. The launchd plist ships no barge env."""
    _deps()
    monkeypatch.delenv("OVERLAY_BARGE_IN", raising=False)
    monkeypatch.delenv("OVERLAY_BARGE_AEC", raising=False)

    def health():
        return client.get("/overlay/health", headers=AUTH_HEADER).json()

    assert (health()["barge_mode"], health()["barge_in"], health()["barge_aec"]) == ("aec", True, True)
    assert client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["setup"]["barge_mode"] == "aec"
    monkeypatch.setenv("OVERLAY_BARGE_AEC", "0")
    assert (health()["barge_mode"], health()["barge_aec"]) == ("headphones", False)
    for off in ("0", "2"):
        monkeypatch.setenv("OVERLAY_BARGE_IN", off)
        assert (health()["barge_mode"], health()["barge_in"]) == ("off", False)
    with (ROOT / "launchd" / "com.jarvis.overlay.service.plist").open("rb") as fh:
        assert list(plistlib.load(fh)["EnvironmentVariables"]) == ["PATH"]


# --- B: a stale poster's weak body gets a 200 and nothing else (criterion 3) ---

def test_weak_body_never_wakes(client, monkeypatch):
    """`{"score": 0.3, "weak": true}` on /overlay/wake: no bus event, no debug turn; a full score
    still publishes the wake. The listener keeps only the full-hit threshold (0.5)."""
    _deps()
    bus = BusRecorder()
    monkeypatch.setattr(overlay, "bus", bus)

    def turns():
        return client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"]

    before = turns()
    r = client.post("/overlay/wake", json={"score": 0.3, "weak": True}, headers=AUTH_HEADER)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert bus.events == [] and turns() == before
    client.post("/overlay/wake", json={"score": 0.8}, headers=AUTH_HEADER)
    assert bus.events == [("wake", {"source": "mac"})]
    pytest.importorskip("numpy")
    pytest.importorskip("openwakeword")
    from mac.voice import wakeword
    assert wakeword.THRESHOLD == 0.5
    assert not hasattr(wakeword, "WEAK_" + "THRESHOLD")


# --- C: a barge-in stop is silent (criterion 4) ---

def test_barge_stop_is_silent(client):
    """A barge-in "stop" ends listening without a word (follow-up window closed, busy agent
    settled quietly too); the same word without barge_in still says the stop ack."""
    _deps()
    events = turn(client, "stop", followup=True, barge_in=True)
    assert says(events) == [] and not [e for e in events if e["type"] == "audio"]
    assert [e["type"] for e in events].count("end_listening") == 1
    assert _steps("followup_closed")[-1]["detail"] == "stop (barge-in, silent)"
    overlay.debug_log.clear()
    events = turn(client, "Hey Jarvis, stop", followup=True, barge_in=True)
    assert says(events) == [] and not [e for e in events if e["type"] == "audio"]
    assert _steps("followup_closed")[-1]["detail"] == "stop (barge-in, silent)"
    overlay.debug_log.clear()
    overlay._agent_busy["c9"] = SimpleNamespace(cancelled=False)
    try:
        events = turn(client, "stop", conv_id="c9", followup=True, barge_in=True)
        assert says(events) == [] and not [e for e in events if e["type"] == "audio"]
        assert _steps("agent_confirm")[-1]["state"] == "stopped"
    finally:
        overlay._agent_busy.pop("c9", None)
    overlay.debug_log.clear()
    assert says(turn(client, "stop")) == [acks.PHRASES["stop"]]
    assert _steps("followup_closed")[-1]["detail"] == "stop"


def test_speakers_barge_yes_sent_as_stop_never_allows(client):
    """Security R1: on speakers the app sends a barge "Jarvis, yes" as "stop"
    (BargeWords.speakersTurnText). That stop is silent and never approves the pending Confirm."""
    clock = Clock()
    pool, _ = setup(clock, push_script(clock))
    turn(client, "commit and push my changes", conv_id="s1")
    events = turn(client, "stop", conv_id="s1", followup=True, barge_in=True)
    assert "allow" not in [r["behavior"] for r in pool.made[0].responses]
    assert says(events) == [] and not [e for e in events if e["type"] == "audio"]
    assert [e["card"]["title"] for e in events if e["type"] == "card"] == ["Not done"]
    assert overlay._take_confirm("s1") is None  # nothing left for a later yes to approve


# --- D: a barge-in real request goes through the pipeline once (criterion 4, A5 side) ---

def test_barge_request_answered_once(client):
    """A barge-in that is not a stop is never dropped: the clock intent runs, exactly one line
    is spoken, and the follow-up window does not report a drop."""
    _deps()
    events = turn(client, "Hey Jarvis, what time is it", followup=True, barge_in=True)
    assert _steps("intent")[-1]["kind"] == "clock"
    assert len(says(events)) == 1
    assert not _steps("followup_dropped")


# --- E: the debug page shows the new rows and the aec setup line (criterion 5) ---

def _page_rows_with_node(rows: list[dict], labels: list[str]) -> dict:
    page = (ROOT / "gateway" / "overlay_debug.html").read_text()
    label = re.search(r"const LABEL = \{.*?\};", page, re.S).group(0)
    describe = re.search(r"function describe\(s\) \{.*?\n\}\n", page, re.S).group(0)
    script = (f"{label}\n{describe}\n"
              f"console.log(JSON.stringify({{d: {json.dumps(rows)}.map(describe),"
              f" l: {json.dumps(labels)}.map(k => LABEL[k])}}));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_debug_page_rows_jfollow8():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    details = [
        ("barge_aec", "on route=speakers in=builtin rate=48000 ch=1 agc=off ducking=min gain_db=6 start_ms=310"),
        ("wake_ignored", "reason=echo_cancel"),
        ("playback", "ended audio_ms=12400"),
        ("barge_in", "at=4100 reason=aec rule=stop dropped_sentences=3 stop_ms=2 stream_open=1 voice_to_stop_ms=640"),
        ("barge_gate", "over=5 rule=name aec=1 mic_p90=0.042 out_p90=0.120 confirmed=1"),
        ("barge_turn", "sent=stop words=4"),
    ]
    got = _page_rows_with_node([{"step": step, "detail": detail} for step, detail in details],
                               [step for step, _ in details])
    assert got["d"] == [detail for _, detail in details]
    assert got["l"][1] == "wake ignored"  # wake_ignored
    assert got["l"][2] == "playback"
    assert got["l"][5] == "barge-in turn"  # a speakers barge turn cut down to a silent stop
    # The setup row is built inside render() (no standalone function), so read the page text:
    assert "barge-in: on (echo canceller on speakers; headphones as before)" in \
        (ROOT / "gateway" / "overlay_debug.html").read_text()
