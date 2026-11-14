"""jfollow7: a second wake inside a request folds into the open turn; debug rows show their
fields; injected-instruction notes are never read aloud; every sessions question reaches the
sessions intent."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from conftest import AUTH_HEADER  # noqa: E402
from gateway import overlay  # noqa: E402
from mac.voice import intents  # noqa: E402
from test_overlay_service import FakeConversations, client, make_deps, turn  # noqa: F401,E402


@pytest.fixture(autouse=True)
def fresh_debug_log():
    overlay.debug_log.clear()
    yield
    overlay.debug_log.clear()


# --- A: a wake inside the request (criteria 7-8) ---

def test_second_wake_folds_into_open_turn(client, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(overlay.time, "time", lambda: now[0])
    overlay.set_deps(make_deps(chunks=['{"say": "Here it is."}\n'], synth=lambda t: (b"\x01\x02", 24000)))
    client.post("/overlay/wake", json={"score": 0.79}, headers=AUTH_HEADER)
    now[0] += 4
    client.post("/overlay/wake", json={"score": 0.66}, headers=AUTH_HEADER)
    turn(client, "show me a card titled Jarvis capabilities")
    turns = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"]
    assert len(turns) == 1
    steps = turns[0]["steps"]
    assert [s["step"] for s in steps][:3] == ["wake", "voice", "transcribed"]
    assert steps[0]["again"] == 1 and steps[0]["score"] == 0.79
    assert "titled Jarvis capabilities" in steps[2]["text"]
    # A hit 31 s after a waiting wake opens a new turn.
    overlay.debug_log.clear()
    client.post("/overlay/wake", json={"score": 0.79}, headers=AUTH_HEADER)
    now[0] += overlay.WAKE_OPEN_S + 1
    client.post("/overlay/wake", json={"score": 0.7}, headers=AUTH_HEADER)
    turns = client.get("/overlay/debug/turns", headers=AUTH_HEADER).json()["turns"]
    assert len(turns) == 2 and all("again" not in t["steps"][0] for t in turns)


@pytest.mark.parametrize("text, name", [("show me a card titled Jarvis capabilities", "Jarvis capabilities"),
                                        ("what can Jarvis Companion do", "Jarvis Companion")])
def test_name_inside_request_reaches_model(client, text, name):
    intent = intents.parse(text, apps={})
    assert intent.kind == "answer" and name in intent.arg
    loud = FakeConversations(['{"say": "Sure."}\n'])
    overlay.set_deps(make_deps(conversations=loud, synth=lambda t: (b"\x01\x02", 24000)))
    turn(client, text)
    prompts = [p for s in loud.sessions.values() for p in s.prompts]
    assert prompts and any(text.split(" ", 2)[-1] in p for p in prompts)
    assert any(name in p for p in prompts)


# --- B: debug rows render their fields (criterion 11) ---

def _describe_with_node(rows: list[dict]) -> list[str]:
    page = (ROOT / "gateway" / "overlay_debug.html").read_text()
    label = re.search(r"const LABEL = \{.*?\};", page, re.S).group(0)
    describe = re.search(r"function describe\(s\) \{.*?\n\}\n", page, re.S).group(0)
    script = f"{label}\n{describe}\nconsole.log(JSON.stringify({json.dumps(rows)}.map(describe)));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_debug_page_describes_rows():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    rows = [
        {"step": "card_remove", "card": "b52eda1c", "match": "title"},
        {"step": "card_remove", "cards": 2, "match": "pick"},
        {"step": "card_remove", "detail": "refused reason=none"},
        {"step": "card_size", "detail": "refused reason=ambiguous"},
        {"step": "card_size", "action": "max", "card": "a1", "match": "title"},
        {"step": "answer", "claim": "dropped"},
        {"step": "answer", "note": "dropped", "sentences": 2},
        {"step": "wake", "score": 0.79, "source": "mac", "again": 1},
        {"step": "wake", "score": 0.79, "source": "mac"},
    ]
    got = _describe_with_node(rows)
    assert got[0] == "card=b52eda1c match=title"
    assert got[1] == "cards=2 match=pick"
    assert got[2] == "refused reason=none"
    assert got[3] == "refused reason=ambiguous"
    assert got[4] == "action=max card=a1 match=title"
    assert "claim=dropped" in got[5]
    assert "note=dropped" in got[6] and "2" in got[6]
    assert "score 0.79" in got[7] and "heard again 1" in got[7]
    assert got[8] == "score 0.79 (mac)"
    page = (ROOT / "gateway" / "overlay_debug.html").read_text()
    assert 'card_remove: "card remove"' in page and 'answer: "answer check"' in page
