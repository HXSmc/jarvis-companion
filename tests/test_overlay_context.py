"""Round 2 (owner: "no access to Clock", "my service is not running", TCC resets).

- "what time is it" is answered locally from the Mac clock, no brain call.
- Every brain turn carries the local date/time, time zone, frontmost app and battery.
- "open the clock app" opens Clock.
- The installer signs with a stable local identity, never ad hoc.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from test_overlay_service import (  # noqa: E402  (shared stubs + fixtures)
    FakeConversations, client, make_deps, reset_overlay_state, says, turn,
)
from gateway import overlay  # noqa: E402
from mac.voice.intents import open_app, parse  # noqa: E402

__all__ = ["client", "reset_overlay_state"]  # pytest fixtures re-exported

NOW = datetime(2026, 11, 10, 16, 5, tzinfo=timezone(timedelta(hours=3)))


def test_clock_questions_parse_to_the_local_clock_intent():
    for text in ("Jarvis, what time is it?", "what's the time", "What is the time now"):
        assert (parse(text).kind, parse(text).arg) == ("clock", "time"), text
    for text in ("what's the date", "what day is it today", "what is today's date"):
        assert (parse(text).kind, parse(text).arg) == ("clock", "date"), text
    # Anything more specific still goes to the brain (which has the context below).
    assert parse("what time is it in Tokyo").kind == "answer"


def test_time_answered_locally_without_the_brain(client):
    pool = FakeConversations()
    deps = make_deps(conversations=pool)
    deps.now = lambda: NOW
    overlay.set_deps(deps)
    assert says(turn(client, "what time is it")) == ["It's 4:05 PM."]
    assert says(turn(client, "what's the date")) == ["It's Tuesday, 10 November 2026."]
    assert pool.sessions == {}  # no brain call


def test_every_brain_turn_carries_local_context(client):
    pool = FakeConversations(chunks=['{"say": "ok"}\n'])
    deps = make_deps(conversations=pool)
    deps.now = lambda: NOW
    deps.system = lambda: "battery 87%, charging"
    overlay.set_deps(deps)
    turn(client, "how long until midnight", conv_id="t1", screen={"app": "Safari"})
    prompt = pool.sessions["t1"].prompts[0]
    assert "Tuesday 10 November 2026, 16:05" in prompt
    assert "UTC+0300" in prompt
    assert "frontmost app: Safari" in prompt
    assert "battery 87%, charging" in prompt
    assert "clock" in overlay.ANSWER_PROMPT.lower()


def test_open_the_clock_app():
    apps = {"clock": "Clock", "safari": "Safari"}
    for text in ("open Clock", "open the clock", "open the clock app", "open clock.app"):
        intent = parse(text, apps=apps)
        assert (intent.kind, intent.arg.lower()) == ("open_app", "clock"), text
    calls = []
    assert open_app("clock", run=lambda argv, **k: calls.append(argv), apps=apps) == "Clock"
    assert calls == [["open", "-a", "Clock"]]


def test_installer_signs_with_a_stable_identity_not_ad_hoc():
    out = subprocess.run(["bash", str(ROOT / "scripts/install-mac-overlay.sh"), "--dry-run"],
                         capture_output=True, text=True, check=True).stdout
    assert "mac-overlay-sign.sh" in out
    assert "--sign -" not in out
    sign = (ROOT / "scripts/mac-overlay-sign.sh").read_text()
    assert 'NAME="JarvisOverlay Local Signing"' in sign
    assert "codeSigning" in sign  # EKU on the self-signed cert
