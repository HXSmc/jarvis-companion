"""Store-level tests for the saved-workspace store (mac/voice/workspaces.py).

Every test points OVERLAY_WORKSPACE_DIR at a tmp_path dir; the real
~/Library/Application Support dir is never touched.
"""

from __future__ import annotations

import json
import stat

import pytest

from mac.voice import workspaces


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
