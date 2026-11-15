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


def test_barge_yes_sent_as_stop_never_allows(client):
    """Security R1: on speakers the app sends a barge turn without a fresh "jarvis" as "stop"
    (BargeWords.bargeTurnText). That stop is silent and denies the pending Confirm, never allows it."""
    clock = Clock()
    pool, _ = setup(clock, push_script(clock))
    turn(client, "commit and push my changes", conv_id="s1")
    events = turn(client, "stop", conv_id="s1", followup=True, barge_in=True)
    assert "allow" not in [r["behavior"] for r in pool.made[0].responses]
    assert says(events) == [] and not [e for e in events if e["type"] == "audio"]
    assert [e["card"]["title"] for e in events if e["type"] == "card"] == ["Not done"]
    assert overlay._take_confirm("s1") is None  # nothing left for a later yes to approve


def test_barge_yes_never_allows_and_stays_pending(client):
    """Hub ruling (security R2 finding 1): a turn with barge_in=true and a yes never approves a
    pending Confirm on any route: nothing is said, the run stays pending, and a later plain
    follow-up yes approves it as usual."""
    clock = Clock()
    pool, _ = setup(clock, push_script(clock))
    turn(client, "commit and push my changes", conv_id="y1")
    for text in ("yes", "Jarvis, yes", "hey jarvis go ahead"):
        events = turn(client, text, conv_id="y1", followup=True, barge_in=True)
        assert pool.made[0].responses == []
        assert says(events) == [] and not [e for e in events if e["type"] in ("audio", "card")]
        assert "end_listening" not in [e["type"] for e in events]  # the follow-up window stays open
        assert "y1" in overlay._confirm
        assert _steps("agent_confirm")[-1]["state"] == "pending"
    events = turn(client, "yes", conv_id="y1", followup=True)
    assert [r["behavior"] for r in pool.made[0].responses] == ["allow"]
    assert "Pushed." in says(events)


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
        ("barge_aec", "on route=speakers in=builtin rate=48000 ch=3 src=ch0 agc=off ducking=min gain_db=6 start_ms=310"),
        ("barge_aec", "levels src=ch0 ch_rms=0.031,0.002,0.002"),
        ("wake_ignored", "reason=echo_cancel"),
        ("playback", "ended audio_ms=12400"),
        ("barge_in", "at=4100 reason=aec rule=stop dropped_sentences=3 stop_ms=2 stream_open=1 voice_to_stop_ms=640"),
        ("barge_gate", "over=5 rule=name aec=1 mic_p90=0.042 out_p90=0.120 confirmed=1"),
        ("barge_turn", "sent=stop reason=no_name aec=1 words=2"),
        ("wake_capture", "restarted reason=reconfigure"),
    ]
    got = _page_rows_with_node([{"step": step, "detail": detail} for step, detail in details],
                               [step for step, _ in details])
    assert got["d"] == [detail for _, detail in details]
    assert got["l"][3] == "wake ignored"  # wake_ignored
    assert got["l"][4] == "playback"
    assert got["l"][7] == "barge-in turn"  # a speakers barge turn cut down to a silent stop
    assert got["l"][8] == "wake mic capture"
    # The setup row is built inside render() (no standalone function), so read the page text:
    assert "barge-in: on (echo canceller on speakers; headphones as before)" in \
        (ROOT / "gateway" / "overlay_debug.html").read_text()


# --- E: round 2 Swift wiring pins (source checks; the logic is in EchoCancel/BargeWords tests) ---

def _swift_func(name: str, file: str) -> str:
    src = (ROOT / "mac/overlay/Sources/JarvisOverlay" / file).read_text()
    start = src.index(f"func {name}(")
    nxt = re.search(r"\n    (?:private |public |static |@MainActor )*func |\n    // MARK", src[start + 1:])
    return src[start:start + 1 + nxt.start()] if nxt else src[start:]


def test_barge_final_keeps_wake_words():
    """Security R3 note: a confirmed barge final reaches bargeTurnText with its "Hey Jarvis", else
    the speakers rule finds no name and a real request goes out as "stop"."""
    body = _swift_func("completeTurn", "SpeechInput.swift")
    assert "turn.barge != nil ? raw.trimmingCharacters(in: .whitespacesAndNewlines) : stripped" in body
    assert "guard !stripped.isEmpty" in body  # a wake word alone is still nothing


def test_ended_barge_run_restarts_on_next_clip():
    """Security R3 finding 2 (I1): a barge run whose turn ended mid-reply is replaced, both on the
    next clip (before startBargeListeningIfNeeded) and one tick after the run ends (queued clips
    still playing), through EchoCancel.restartBargeRun."""
    src = (ROOT / "mac/overlay/Sources/JarvisOverlay/AppController.swift").read_text()
    audio = src[src.index("case .audio(let text"):src.index("case .shape(")]
    assert audio.index('restartEndedBargeRun(bargeRun, reason: "clip")') < audio.index("startBargeListeningIfNeeded()")
    start = _swift_func("startBargeListeningIfNeeded", "AppController.swift")
    assert 'Task { @MainActor in self?.restartEndedBargeRun(run, reason: "ended") }' in start
    body = _swift_func("restartEndedBargeRun", "AppController.swift")
    assert "run === ended" in body and "EchoCancel.restartBargeRun(confirmed: run.confirmed" in body
    assert body.index("bargeRun = nil") < body.index("startBargeListeningIfNeeded()")
    assert 'detail: "restart reason=run_ended after=' in body


def test_failed_reconfigure_is_retried():
    """Code lens R4 finding 1: a reconfigure that threw leaves isRunning false; the stopped flag must
    still read true (started once, engine not running), so the next configuration/route change
    rebuilds the engine instead of leaving a dead mic. A recovered mic clears the sticky banner."""
    src = (ROOT / "mac/overlay/Sources/JarvisOverlay/SpeechInput.swift").read_text()
    assert ("var engineStoppedOnItsOwn: Bool { EchoCancel.engineStopped(startedOnce: startedOnce, "
            "running: engine.isRunning) }") in src
    assert "isRunning && !engine.isRunning" not in src
    start = _swift_func("startEngine", "SpeechInput.swift")
    assert start.index("try engine.start()") < start.index("startedOnce = true")
    unavailable = _swift_func("micUnavailable", "AppController.swift")
    assert "guard !micDown else { return }" in unavailable
    assert 'detail: "failed reason=no_input"' in unavailable
    reconf = _swift_func("reconfigureEngine", "AppController.swift")
    assert reconf.index("try speech.reconfigure") < reconf.index("micDown = false")
    assert "model.banner = nil" in reconf


def test_multichannel_voice_processing_feeds_channel0():
    """Mac run 1 (round 5): the MacBook Air mic is 3 channels with voice processing on; the old
    mono-only guard threw EngineFail.format so the canceller never ran. Any format with a rate and a
    channel is usable; the tap keeps the node's format (no conversion on the input node) and hands on
    channel 0 in a mono format, which the recogniser's converter and the levels get."""
    start = _swift_func("startEngine", "SpeechInput.swift")
    assert "EchoCancel.formatUsable(rate: format.sampleRate, channels: Int(format.channelCount))," in start
    assert "voiceProcessing: on)" not in start
    assert "tap.install(on: input, format: format, mono: mono)" in start
    assert start.index("EchoCancel.formatUsable") < start.index("tap.install(") < start.index("attach(on)")
    assert start.index("tap.install(") < start.index("micFormat = mono")
    listen = _swift_func("listenAfterWake", "SpeechInput.swift")
    assert "let format = micFormat ?? engine.inputNode.outputFormat(forBus: 0)" in listen
    src = (ROOT / "mac/overlay/Sources/JarvisOverlay/SpeechInput.swift").read_text()
    assert "let copy = Self.channel0(buffer, mono: mono)" in src
    assert "Self.copy(buffer)" not in src  # the old all-channel copy fed the recogniser 3 channels
    note = _swift_func("noteEchoCancel", "AppController.swift")
    assert "ch=\\(setup.channels) src=\\(setup.src)" in note
    levels = _swift_func("noteChannelLevels", "AppController.swift")
    assert 'detail: "levels src=\\(src) ch_rms=\\(rms)"' in levels


def test_young_barge_run_gets_one_delayed_retry():
    """Code lens R4 finding 3: the run's age counts from its creation (not its first gate block), and
    a run that ended younger than minBargeRunS is retried once after the rest of it, not left dead
    until the next clip."""
    body = _swift_func("restartEndedBargeRun", "AppController.swift")
    assert "ranFor: Date().timeIntervalSince(run.createdAt)" in body
    assert "retried: run.retryQueued" in body
    assert "gateThrough" not in body
    assert body.index("run.retryQueued = true") < body.index("Task.sleep(")
    assert 'self?.restartEndedBargeRun(run, reason: "retry")' in body
    assert body.index("return\n") < body.index("bargeRun = nil")


# --- F: round 5, the wake word's ffmpeg restarts after the app rebuilds its audio engine ---

def test_engine_note_restarts_wake_capture(client, monkeypatch):
    """Mac run 1: the service's ffmpeg, open while the app built its voice-processing device, went
    deaf. The app's one state note per reconfigure (`barge_aec on|off|failed`) restarts it; the
    paired `reconfigured`, `deferred` and `levels` notes do not (one restart per rebuild)."""
    asked = []
    monkeypatch.setitem(sys.modules, "mac.voice.wakeword", SimpleNamespace(restart_capture=asked.append))
    for detail in ("reconfigured reason=mode", "deferred reason=busy", "levels src=ch0 ch_rms=0.1,0.0,0.0"):
        assert client.post("/overlay/debug/note", json={"step": "barge_aec", "detail": detail},
                           headers=AUTH_HEADER).status_code == 200
    client.post("/overlay/debug/note", json={"step": "lines", "detail": "on target"}, headers=AUTH_HEADER)
    assert asked == []
    for detail in ("on route=speakers in=builtin rate=48000 ch=3 src=ch0 agc=off ducking=min gain_db=6 start_ms=900",
                   "off reason=headphones", "failed reason=format"):
        client.post("/overlay/debug/note", json={"step": "barge_aec", "detail": detail}, headers=AUTH_HEADER)
    assert asked == ["reconfigure"] * 3
    # no wake thread (module never imported): the note is just a row
    monkeypatch.delitem(sys.modules, "mac.voice.wakeword")
    assert client.post("/overlay/debug/note", json={"step": "barge_aec", "detail": "off reason=mode"},
                       headers=AUTH_HEADER).status_code == 200


_FAKE_FFMPEG = {
    # 80 ms of silence every 20 ms, forever
    "stream": "import sys,time\nwhile True:\n    sys.stdout.buffer.write(bytes(2560)); sys.stdout.flush(); time.sleep(0.02)",
    "stall": "import sys,time\nsys.stdout.buffer.write(bytes(2560)); sys.stdout.flush(); time.sleep(60)",
    "exit": "pass",
}


class _Model:
    def __init__(self):
        self.resets = 0

    def predict(self, pcm):
        return {"hey_jarvis": 0.0}

    def reset(self):
        self.resets += 1


class _Stop(Exception):
    pass


def _live(monkeypatch, kinds, on_restart, procs=None, **consts):
    """run_live with a python stand-in for ffmpeg (kinds[i] = the i-th process's behaviour)."""
    pytest.importorskip("numpy")
    pytest.importorskip("openwakeword")
    from mac.voice import wakeword
    for name, value in consts.items():
        monkeypatch.setattr(wakeword, name, value)
    procs = [] if procs is None else procs

    def popen(argv, **kw):
        assert argv == wakeword.ffmpeg_argv()  # the same argv every time
        kind = kinds[min(len(procs), len(kinds) - 1)]
        procs.append(subprocess.Popen([sys.executable, "-c", _FAKE_FFMPEG[kind]], **kw))
        return procs[-1]

    model = _Model()
    with pytest.raises((_Stop, SystemExit)) as err:
        wakeword.run_live(on_restart=on_restart, popen=popen, model=model)
    return wakeword, procs, model, err.value


def test_restart_capture_replaces_the_one_ffmpeg(monkeypatch):
    """restart_capture terminates the running ffmpeg; run_live waits for it, starts a fresh one
    (same argv), resets the model and writes reason=reconfigure. Never two alive, none left."""
    import threading
    import time
    pytest.importorskip("openwakeword")
    from mac.voice import wakeword as ww
    rows, procs = [], []

    def on_restart(reason):
        rows.append(reason)
        assert [p.poll() is None for p in procs] == [False] * (len(procs) - 1) + [True]
        if len(rows) == 2:
            raise _Stop

    def asker():
        for n in (1, 2):
            deadline = time.monotonic() + 10
            while len(procs) < n and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.2)  # audio flowing
            assert ww.restart_capture("reconfigure")

    threading.Thread(target=asker, daemon=True).start()
    _, _, model, _ = _live(monkeypatch, ["stream"], on_restart, procs, RESTART_GAP_S=0.0)
    assert rows == ["reconfigure", "reconfigure"]
    assert len(procs) == 3 and model.resets == 2
    assert all(p.poll() is not None for p in procs)  # the finally stopped the last one
    assert ww._capture["proc"] is None
    assert ww.restart_capture("reconfigure") is False  # no capture running


def test_stalled_capture_restarts(monkeypatch):
    """An ffmpeg that stops sending audio (no exit) is replaced after STALL_S: reason=stalled."""
    rows = []

    def on_restart(reason):
        rows.append(reason)
        raise _Stop

    _, procs, _, _ = _live(monkeypatch, ["stall", "stream"], on_restart, STALL_S=0.3, HEAL_WAIT_S=0.0)
    assert rows == ["stalled"]
    assert all(p.poll() is not None for p in procs)


def test_exiting_capture_self_heals_bounded(monkeypatch):
    """ffmpeg that keeps exiting is restarted HEAL_MAX times (reason=exited), then the thread ends
    with the old 'mic capture ended' exit; no process is left."""
    rows = []
    _, procs, _, err = _live(monkeypatch, ["exit"], rows.append, HEAL_WAIT_S=0.0, HEAL_MAX=3)
    assert isinstance(err, SystemExit) and "mic capture ended" in str(err)
    assert rows == ["exited"] * 3 and len(procs) == 4
    assert all(p.poll() is not None for p in procs)
