"""common/hud.py: shared shape/card schema (phone Hardware Mode + Mac overlay)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common import hud

FIXTURES = Path(__file__).parent / "fixtures" / "hud"
FIXTURE_FILES = sorted(p for p in FIXTURES.glob("*.json") if p.name != "schema.json")


@pytest.mark.parametrize("kind", hud.SHAPE_KINDS)
def test_every_shape_kind_accepted(kind):
    out = hud.validate_shape({"kind": kind, "box": [10, 20, 30, 40], "label": "x", "id": "s1"})
    assert out == {"kind": kind, "box": [10.0, 20.0, 30.0, 40.0], "label": "x", "id": "s1"}


@pytest.mark.parametrize("kind", ["arrow", "freeform", "mask", "line", "", None])
def test_other_shapes_rejected(kind):
    assert hud.validate_shape({"kind": kind, "box": [0, 0, 10, 10]}) is None


@pytest.mark.parametrize("box", [None, [1, 2, 3], [0, 0, 0, 10], [5, 5, 5, 5], [0, 0, "a", 1],
                                 [0, 0, float("nan"), 1], [0, 0, True, 1], "0,0,1,1"])
def test_bad_boxes_rejected(box):
    assert hud.validate_shape({"kind": "rect", "box": box}) is None


def test_swapped_box_corners_normalised():
    assert hud.validate_shape({"kind": "rect", "box": [30, 40, 10, 20]})["box"] == [10, 20, 30, 40]


def test_shape_label_clamped():
    out = hud.validate_shape({"kind": "circle", "box": [0, 0, 1, 1], "label": "L" * 100})
    assert len(out["label"]) == hud.MAX_LABEL


CARDS = {
    "info": {"body": ["one", "two"]},
    "stat": {"value": "81", "label": "endpoints"},
    "bar": {"items": [{"label": "a", "value": 1}, {"label": "b", "value": 2.5}], "caption": "src"},
    "flow": {"nodes": [{"id": "a", "text": "A"}, {"id": "b", "text": "B"}], "edges": [["a", "b", "go"]]},
    "link": {"url": "https://example.com/x?y=1", "source": "example.com"},
    "image": {"url": "https://example.com/p.jpg"},
}


@pytest.mark.parametrize("ctype", hud.CARD_TYPES)
def test_every_card_type_accepted(ctype):
    raw = {"id": "c1", "type": ctype, "title": "T", **CARDS[ctype]}
    out = hud.validate_card(raw)
    assert out is not None and out["type"] == ctype and out["id"] == "c1"
    for key, value in CARDS[ctype].items():
        assert out[key] == value


def test_unknown_card_type_dropped():
    assert hud.validate_card({"id": "c", "type": "video", "title": "T"}) is None
    assert hud.validate_card({"id": "c", "type": "info", "title": "", "body": ["x"]}) is None


@pytest.mark.parametrize("url", ["http://example.com", "javascript:alert(1)", "file:///etc/passwd",
                                 "https://", "https://exa mple.com", "data:text/html,hi", "", None, 5,
                                 "https://example.com/\nx", "HTTP://example.com"])
def test_non_https_urls_rejected(url):
    assert hud.validate_card({"id": "c", "type": "link", "title": "T", "url": url}) is None
    assert hud.validate_card({"id": "c", "type": "image", "title": "T", "url": url}) is None


def test_image_file_id_path_traversal_rejected():
    assert hud.validate_card({"id": "c", "type": "image", "title": "T", "file_id": "f_1.jpg"})["file_id"] == "f_1.jpg"
    assert hud.validate_card({"id": "c", "type": "image", "title": "T", "file_id": "../etc/passwd"}) is None


def test_overlong_text_clamped():
    out = hud.validate_card({"id": "i" * 99, "type": "info", "title": "T" * 99, "body": ["x" * 999] * 9})
    assert len(out["title"]) == hud.MAX_TITLE
    assert len(out["id"]) == hud.MAX_ID
    assert len(out["body"]) == hud.MAX_BODY_LINES
    assert all(len(line) == hud.MAX_LINE for line in out["body"])


def test_bar_and_flow_limits():
    bar = hud.validate_card({"id": "b", "type": "bar", "title": "T",
                             "items": [{"label": f"l{i}", "value": i} for i in range(20)] + [{"label": "x", "value": "9"}]})
    assert len(bar["items"]) == hud.MAX_ITEMS
    flow = hud.validate_card({"id": "f", "type": "flow", "title": "T",
                              "nodes": [{"id": str(i), "text": "n"} for i in range(20)],
                              "edges": [["0", "1"], ["0", "99"], "bad", ["1", "2", "L" * 99]]})
    assert len(flow["nodes"]) == hud.MAX_NODES
    assert flow["edges"] == [["0", "1"], ["1", "2", "L" * hud.MAX_EDGE_LABEL]]


def test_missing_card_id_generated():
    assert hud.validate_card({"type": "stat", "title": "T", "value": "1"})["id"]


def test_validate_hud_drops_invalid_and_never_raises():
    msg = {"shapes": [{"kind": "rect", "box": [0, 0, 1, 1]}, {"kind": "arrow", "box": [0, 0, 1, 1]}, 7],
           "cards": [{"id": "c", "type": "link", "title": "T", "url": "javascript:x"},
                     {"id": "d", "type": "stat", "title": "T", "value": 3}]}
    out = hud.validate_hud(msg)
    assert [s["kind"] for s in out["shapes"]] == ["rect"]
    assert [c["id"] for c in out["cards"]] == ["d"] and out["cards"][0]["value"] == "3"
    for junk in (None, [], "x", {"shapes": "no", "cards": None}):
        assert hud.validate_hud(junk) == {"shapes": [], "cards": []}


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixtures_are_already_valid(path):
    """Fixtures are shared with the Swift decoder: validating them must be a no-op."""
    msg = json.loads(path.read_text())
    assert hud.validate_hud(msg) == msg  # 120 == 120.0, so int/float boxes compare equal


def test_fixtures_cover_every_kind_and_type():
    shapes, cards = set(), set()
    for path in FIXTURE_FILES:
        msg = json.loads(path.read_text())
        shapes |= {s["kind"] for s in msg["shapes"]}
        cards |= {c["type"] for c in msg["cards"]}
    assert shapes == set(hud.SHAPE_KINDS) and cards == set(hud.CARD_TYPES)


def test_schema_file_matches_export():
    assert json.loads((FIXTURES / "schema.json").read_text()) == hud.schema_json()
