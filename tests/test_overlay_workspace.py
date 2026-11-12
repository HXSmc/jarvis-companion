"""Tests for the saved-workspace store (mac/voice/workspaces.py) and its voice turns.

Every test points OVERLAY_WORKSPACE_DIR at a tmp_path dir; the real
~/Library/Application Support dir is never touched.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from conftest import AUTH_HEADER  # noqa: E402
from gateway import overlay  # noqa: E402
from mac.voice import acks, workspaces  # noqa: E402
# The agent tests' fixtures (`reset` is autouse: state cleared, HOME = a fake folder) and fakes.
from test_overlay_agent import client, reset  # noqa: F401,E402
from test_overlay_service import make_deps, says, turn  # noqa: E402


@pytest.fixture
def ws_dir(tmp_path, monkeypatch):
    path = tmp_path / "workspaces"
    monkeypatch.setenv("OVERLAY_WORKSPACE_DIR", str(path))
    return path


def test_store_round_trip(ws_dir):
    cards = [{"id": f"c{i}", "kind": "info", "title": f"Card {i}"} for i in range(1, 4)]
    pages = [{"url": "https://example.com/a", "title": "Page A"},
             {"url": "https://example.org/b", "title": "Page B"}]
    doc = workspaces.save("our battery research", cards, pages, "c2")
    assert (ws_dir / "battery.json").exists()
    assert stat.S_IMODE((ws_dir / "battery.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(ws_dir.stat().st_mode) == 0o700
    assert doc["name"] == "battery research"
    assert doc["enlarged"] == "c2"
    back = workspaces.load("battery")
    assert back["slug"] == "battery"
    assert back["cards"] == cards
    assert back["pages"] == pages
    assert back["enlarged"] == "c2"
    assert workspaces.exists("battery") is True
    assert workspaces.load("no-such-workspace") is None


def test_store_caps(ws_dir):
    # cards are capped
    cards = [{"id": f"c{i}", "kind": "info", "title": f"C{i}"} for i in range(25)]
    doc = workspaces.save("plan one", cards, [], None)
    assert len(doc["cards"]) == workspaces.WORKSPACE_MAX_CARDS
    # pages are filtered to safe http/https URLs and capped
    pages = [{"url": "file:///etc/passwd", "title": "x"},
             {"url": "javascript:alert(1)", "title": "x"},
             {"url": "ftp://example.com", "title": "x"},
             {"url": "https://example.com/ok", "title": "kept"},
             {"url": "http://", "title": "x"}]
    doc = workspaces.save("plan two", [], pages, None)
    assert [p["url"] for p in doc["pages"]] == ["https://example.com/ok"]
    # a path-like spoken name still lands inside the dir
    workspaces.save("../../x", [], [], None)
    assert (ws_dir / "x.json").exists()
    # an oversize card list is trimmed under the byte cap
    big = [{"id": f"b{i}", "text": "z" * 10000} for i in range(50)]
    doc = workspaces.save("big sheet", big, [], None)
    assert len(json.dumps(doc, ensure_ascii=False).encode("utf-8")) <= workspaces.WORKSPACE_MAX_BYTES
    assert len(doc["cards"]) < 50
    # 30 saved, a 31st NEW name is refused as full; overwriting still works
    for f in ws_dir.glob("*.json"):
        f.unlink()
    for i in range(30):
        workspaces.save(f"sheet {i} plan", [], [], None)
    assert len(list(ws_dir.glob("*.json"))) == 30
    with pytest.raises(workspaces.WorkspaceError) as err:
        workspaces.save("fresh idea box", [], [], None)
    assert err.value.reason == "full"
    again = workspaces.save("sheet 0 plan", [], [], None)
    assert again["slug"] == "sheet-0-plan"


def test_store_name_match(ws_dir):
    workspaces.save("battery research", [], [], None)
    workspaces.save("battery notes", [], [], None)
    assert sorted(workspaces.match("battery")) == ["battery", "battery-notes"]
    # "our battery research" spells the saved name exactly: it wins alone (slug "battery")
    assert workspaces.match("our battery research") == ["battery"]
    assert workspaces.match("kitchen sink") == []
    assert workspaces.match("the research") == []


def test_store_delete(ws_dir):
    workspaces.save("battery research", [], [], None)
    assert workspaces.exists("battery") is True
    assert workspaces.delete("battery") is True
    assert workspaces.exists("battery") is False
    assert workspaces.delete("battery") is False
    assert workspaces.delete("../../x") is False
    assert workspaces.load("battery") is None
    assert not (ws_dir / "battery-research.json").exists()


# --- the voice turns (jfollow4 H) ---------------------------------------------


@pytest.fixture(autouse=True)
def fresh_ws_state():
    overlay._host_ask.clear()
    overlay._ws_pages.clear()
    overlay._ws_nonces.clear()
    yield
    overlay._host_ask.clear()
    overlay._ws_pages.clear()
    overlay._ws_nonces.clear()


def _deps(**kw):
    # no synth: the fixed ack lines then arrive as say events the test can read
    deps = make_deps(**kw)
    overlay.set_deps(deps)
    return deps


def _rows(step="workspace"):
    return [s for t in reversed(overlay.debug_log.snapshot()["turns"])
            for s in t["steps"] if s["step"] == step]


def save_turn(client, text, cards=(), pages=(), conv_id="c1"):
    """A save turn: the app's snapshot POST is answered while the turn stream is open."""
    events = []
    with client.stream("POST", "/overlay/utterance",
                       json={"text": text, "conv_id": conv_id}, headers=AUTH_HEADER) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") == "workspace_save":
                answer = client.post(f"/overlay/workspace/{event['nonce']}",
                                     json={"cards": list(cards), "pages": list(pages)},
                                     headers=AUTH_HEADER)
                assert answer.status_code == 200 and answer.json() == {"ok": True}
            events.append(event)
    return events


def test_save_and_open_round_trip(client, ws_dir):
    _deps()
    gate = {"id": "c1", "type": "info", "title": "Gate", "body": ["code 4471"]}
    notes = {"id": "c2", "type": "info", "title": "Notes", "body": ["in the vault"]}
    pages = [{"url": "https://example.com/a", "title": "Page A"},
             {"url": "file:///etc/passwd", "title": "not a page"}]
    events = save_turn(client, "save this as battery research",
                       cards=[{"card": gate, "enlarged": True}, {"card": notes}], pages=pages)
    assert says(events) == ["Saved as battery research, sir. Say open battery research to bring it back."]
    (info,) = [e["card"] for e in events if e["type"] == "card"]
    assert info["title"] == "battery research"
    assert info["body"] == ["2 cards, 1 pages", "Positions and boxes are not saved."]
    (drop,) = [e for e in events if e["type"] == "drop_cards"]
    assert drop["ids"] == ["c1", "c2"]
    assert _rows()[-1]["detail"] == "save name_words=2 cards=2 pages=1"
    assert "battery" not in json.dumps(_rows())  # rows carry counts, never names

    events = turn(client, "open the battery research")
    assert says(events) == ["Here is battery research, sir."]
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert [c["title"] for c in cards] == ["Gate", "Notes", "Saved pages"]
    assert all(c["id"].startswith("ws") and len(c["id"]) == 8 for c in cards[:2])  # fresh ids
    assert {c["id"] for c in cards[:2]}.isdisjoint({"c1", "c2"})  # a stored id never comes back
    assert cards[2]["body"] == ["Page A · example.com"]  # the file:// page is dropped
    (size,) = [e for e in events if e["type"] == "card_size"]
    assert size == {"type": "card_size", "id": cards[0]["id"], "size": "max"}
    assert _rows()[-1]["detail"] == "open cards=2 pages=1"
    assert "c1" in overlay._tainted  # saved card text is model text
    assert overlay._ws_pages["c1"] == ["https://example.com/a"]

    events = turn(client, "open the pages")
    (open_pages,) = [e for e in events if e["type"] == "open_pages"]
    assert open_pages["urls"] == ["https://example.com/a"]
    assert says(events) == ["Opening them."]
    assert _rows()[-1]["detail"] == "pages opened=1"


def test_open_the_pages_with_none_saved(client, ws_dir):
    _deps()
    events = turn(client, "open the pages")
    assert says(events) == [acks.PHRASES["workspace_no_pages"]]
    assert not [e for e in events if e["type"] == "open_pages"]


def test_save_asks_before_replacing(client, ws_dir):
    _deps()
    workspaces.save("battery research", [{"id": "old", "type": "info", "title": "Old"}], [], None)
    gate = {"id": "c1", "type": "info", "title": "Gate", "body": ["code 4471"]}
    events = save_turn(client, "save this as battery research", cards=[{"card": gate}])
    assert says(events) == ["Replace battery research?"]
    (card,) = [e["card"] for e in events if e["type"] == "card"]
    assert card["id"] == overlay._CONFIRM_CARD_ID and card["title"] == "Confirm"
    events = turn(client, "yes")
    assert says(events) == ["Saved as battery research, sir. Say open battery research to bring it back."]
    assert _rows()[-1]["detail"] == "save name_words=2 cards=1 pages=0"
    assert [c["title"] for c in workspaces.load("battery")["cards"]] == ["Gate"]

    save_turn(client, "save this as battery research", cards=[{"card": gate}])
    turn(client, "no")  # cancel: the saved workspace keeps the old card
    assert _rows()[-1]["detail"] == "replace cancelled"
    assert [c["title"] for c in workspaces.load("battery")["cards"]] == ["Gate"]


def test_save_timeout_and_empty_screen(client, ws_dir, monkeypatch):
    _deps()
    monkeypatch.setattr(overlay, "WORKSPACE_SNAPSHOT_WAIT_S", 0.05)
    events = turn(client, "save this as battery research")  # the app never answers
    assert says(events) == [acks.PHRASES["workspace_failed"]]
    assert _rows()[-1]["detail"] == "save failed reason=timeout"
    assert workspaces.list_all() == []
    # nothing on screen (or every card invalid) is not a workspace
    events = save_turn(client, "save this as battery research",
                       cards=[{"card": {"id": "x", "type": "bogus", "title": "nope"}}])
    assert says(events) == [acks.PHRASES["workspace_empty"]]
    assert _rows()[-1]["detail"] == "save name_words=2 cards=0 pages=0"
    assert workspaces.list_all() == []


def test_open_is_ambiguous_or_missing(client, ws_dir):
    _deps()
    workspaces.save("battery research", [], [], None)
    workspaces.save("battery notes", [], [], None)
    events = turn(client, "open the battery")
    assert says(events) == ["Which one: battery notes or battery research?"]  # by slug order
    assert not [e for e in events if e["type"] in ("card", "card_size")]
    assert _rows()[-1]["detail"] == "open ambiguous count=2"
    events = turn(client, "open the holiday research")
    assert says(events) == [acks.PHRASES["workspace_none"]]


def test_delete_needs_the_host_confirm(client, ws_dir):
    _deps()
    workspaces.save("battery research", [], [], None)
    events = turn(client, "delete the battery research")
    assert says(events) == ["Delete battery research?"]
    (card,) = [e["card"] for e in events if e["type"] == "card"]
    assert card["title"] == "Confirm"
    events = turn(client, "yes")
    assert says(events) == [acks.PHRASES["workspace_deleted"]]
    assert _rows()[-1]["detail"] == "delete confirmed"
    assert workspaces.exists("battery") is False
    workspaces.save("battery research", [], [], None)
    turn(client, "delete the battery research", conv_id="c2")
    turn(client, "no", conv_id="c2")
    assert _rows()[-1]["detail"] == "delete cancelled"
    assert workspaces.exists("battery") is True


def test_list_says_the_saved_names(client, ws_dir):
    _deps()
    events = turn(client, "what have i saved")
    assert says(events) == [acks.PHRASES["workspace_none_saved"]]
    workspaces.save("battery research", [], [], None)
    workspaces.save("battery notes", [], [], None)
    events = turn(client, "what have i saved")
    (card,) = [e["card"] for e in events if e["type"] == "card"]
    names = [line.split(" · ")[0] for line in card["body"]]
    assert sorted(names) == ["battery notes", "battery research"]
    assert card["title"] == "Saved"
    assert says(events) == [f"You have 2 saved: {', '.join(names)}."]
    assert _rows()[-1]["detail"] == "list count=2"
    assert "battery" not in json.dumps(_rows())  # rows carry counts, never names


def test_snapshot_route_guards(client):
    # unknown or malformed nonce: 404, nothing stored
    for nonce in ("0123456789abcdef", "abc", "Z" * 16):
        assert client.post(f"/overlay/workspace/{nonce}", json={"cards": [], "pages": []},
                           headers=AUTH_HEADER).status_code == 404
    # a nonce older than its window is dead, known or not
    nonce, _event, _slot = overlay._register_ws_nonce("c9")
    known = overlay._ws_nonces[nonce]
    overlay._ws_nonces[nonce] = (known[0], known[1] - overlay._WS_NONCE_S - 1, known[2], known[3])
    assert client.post(f"/overlay/workspace/{nonce}", json={"cards": [], "pages": []},
                       headers=AUTH_HEADER).status_code == 404
    # a live nonce hands its body to the waiting turn and dies
    nonce, event, slot = overlay._register_ws_nonce("c9")
    resp = client.post(f"/overlay/workspace/{nonce}",
                       json={"cards": [{"card": {"id": "c1"}, "enlarged": True}], "pages": []},
                       headers=AUTH_HEADER)
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert event.is_set() and slot["body"]["cards"][0]["card"]["id"] == "c1"
    assert nonce not in overlay._ws_nonces  # one shot only
