"""Model-driven app open: forgiving app names, tie question, owner-intent gate. Stubs only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from gateway import overlay
from mac.voice import intents
from test_overlay_service import client, make_deps, reset_overlay_state, says, turn  # noqa: F401

NAMES = ["Visual Studio Code", "Xcode", "Code Runner", "Code Helper", "Notes", "Music", "Messages",
         "Terminal", "iTerm", "System Settings", "Safari", "Google Chrome", "Clock", "Calendar",
         "Mail", "Maps", "Photos", "Shortcuts", "Numbers"]
APPS = {n.lower(): n for n in NAMES}


def opens(*names):
    return [json.dumps({"open": n}) + "\n" for n in names]


def run_turn(client, text, chunks, conv_id="c1"):
    ran: list[list] = []
    overlay.set_deps(make_deps(chunks=chunks, apps=APPS, run=lambda argv, **k: ran.append(list(argv))))
    events = turn(client, text, conv_id=conv_id)
    return ran, events


# --- resolver ------------------------------------------------------------------

@pytest.mark.parametrize("name,want", [
    ("Visual Studio Code", "Visual Studio Code"), ("visual code studio", "Visual Studio Code"),
    ("vscode", "Visual Studio Code"), ("VS Code", "Visual Studio Code"), ("a VS code", None),
    ("xcode", "Xcode"), ("the clock app", "Clock"), ("visual", "Visual Studio Code"),
    ("x", "Xcode"), ("t", "Terminal"), ("i", "iTerm"), ("settings", "System Settings"),
    ("google", "Google Chrome"), ("Photoshop", None),
])
def test_resolver(name, want):
    got = intents.app_candidates(name, APPS)
    assert (got[0] if len(got) == 1 else None) == want


def test_ties_return_every_match():
    assert intents.app_candidates("code", APPS) == ["Code Helper", "Code Runner", "Visual Studio Code"]
    assert intents.app_candidates("the code app", APPS) == intents.app_candidates("code", APPS)
    for letter in "mns":
        assert len(intents.app_candidates(letter, APPS)) > 1
    assert intents.open_app("code", run=lambda *a, **k: pytest.fail("tie must not launch"), apps=APPS) is None


def test_turn_c_parse_and_launch():
    intent = intents.parse("Open visual code studio", apps=APPS)
    assert intent.kind == "open_app"
    ran = []
    assert intents.open_app(intent.arg, run=lambda argv, **k: ran.append(argv), apps=APPS) == "Visual Studio Code"
    assert ran == [["open", "-a", "Visual Studio Code"]]


# --- intent path ---------------------------------------------------------------

def test_tie_asks(client):
    ran, events = run_turn(client, "open code", [])
    assert ran == []
    assert says(events)[1:] == ["Did you mean Code Helper or Code Runner?"]


def test_unknown_app_unchanged(client):
    ran, events = run_turn(client, "open Photoshop", [])
    assert ran == [] and says(events)[1:] == ["I couldn't find Photoshop."]


# --- model open ----------------------------------------------------------------

def test_turn_a_pull_a_vs_code(client):
    ran, events = run_turn(client, "Can you pull a VS code for me", opens("VS Code"))
    assert ran == [["open", "-a", "Visual Studio Code"]]
    assert "Opening Visual Studio Code." in says(events)
    assert not any("please say" in json.dumps(e).lower() for e in events)


@pytest.mark.parametrize("raw", ["Photoshop", "/Applications/Safari.app", "https://evil.example",
                                 "Xcode; rm -rf ~", "$(open x)", "", "x" * 61])
def test_bad_model_names_never_launch_or_echo(client, raw):
    ran, events = run_turn(client, "could you open Safari", opens(raw))
    assert ran == []
    assert says(events)[1:] == ["I couldn't find that app."]
    if raw:
        assert raw not in json.dumps(events)


def test_one_open_per_turn_and_fresh_allowance(client):
    ran, _ = run_turn(client, "could you open Notes", opens("Notes", "Music"))
    assert ran == [["open", "-a", "Notes"]]
    ran, _ = run_turn(client, "could you open Notes", opens("Notes"), conv_id="c2")
    assert ran == [["open", "-a", "Notes"]]


def test_model_tie_asks(client):
    ran, events = run_turn(client, "could you open code", opens("code"))
    assert ran == [] and says(events)[1:] == ["Did you mean Code Helper or Code Runner?"]


@pytest.mark.parametrize("text,app", [
    ("tell me a joke", "Messages"), ("tell me a joke", "Xcode"),
    ("what is in my notes about the budget", "Notes"), ("play some music", "Music"),
    ("what does this error in Terminal mean", "Terminal"),
])
def test_no_owner_intent_no_launch(client, text, app):
    ran, events = run_turn(client, text, opens(app))
    assert ran == [] and says(events)[1:] == []
    assert not overlay._history["c1"][-1][1].count("find")


def test_model_tie_needs_owner_intent(client):
    ran, events = run_turn(client, "tell me a joke", opens("code"))
    assert ran == [] and says(events)[1:] == []
    assert not any(n in json.dumps(events) for n in ("Code Helper", "Code Runner"))


def test_bad_name_speaks_only_after_open_intent(client):
    ran, events = run_turn(client, "could you open photoshop", opens("Photoshop"))
    assert ran == [] and says(events)[1:] == ["I couldn't find that app."]
    ran, events = run_turn(client, "tell me a joke", opens("Photoshop"), conv_id="c2")
    assert ran == [] and says(events)[1:] == []


def test_no_owner_intent_when_tainted(client):
    overlay._tainted.add("t1")
    ran, _ = run_turn(client, "tell me a joke", opens("Xcode"), conv_id="t1")
    assert ran == []


@pytest.mark.parametrize("text,app,conv", [
    ("pull up Xcode", "Xcode", "t1"), ("could you open Notes", "Notes", "c1"), ("bring up the terminal", "Terminal", "c1"),
    ("launch iTerm", "iTerm", "c1"), ("fire up Safari", "Safari", "c1"),
])
def test_owner_intent_launches(client, text, app, conv):
    overlay._tainted.add("t1")
    ran, _ = run_turn(client, text, opens(app), conv_id=conv)
    assert ran == [["open", "-a", app]]


def test_prompt_text():
    for prompt in (overlay.ANSWER_PROMPT, overlay.QUIET_PROMPT):
        assert "is built in" not in prompt and "say to ask" not in prompt
        assert '{"open"' in prompt


def test_cached_apps_path(client, monkeypatch):
    calls = []
    monkeypatch.setattr(overlay, "_apps_cache", None)
    monkeypatch.setattr(overlay.intents, "list_apps", lambda: calls.append(1) or APPS)
    ran: list[list] = []
    overlay.set_deps(make_deps(chunks=opens("VS Code"), run=lambda argv, **k: ran.append(list(argv))))
    turn(client, "Can you pull a VS code for me", conv_id="a1")
    turn(client, "Can you pull a VS code for me", conv_id="a2")
    assert ran == [["open", "-a", "Visual Studio Code"]] * 2
    assert len(calls) == 1
