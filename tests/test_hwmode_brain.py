"""Hardware Mode brain: reply parsing and zoom-crop coordinate round trip (no model calls)."""

import pytest

from hwmode.brain import parse_reply


def test_parse_reply_plain_json():
    d = parse_reply('{"say": "Press it.", "overlay": {"target_px": [1, 2, 3, 4], "action": "press"}}')
    assert d["overlay"]["target_px"] == [1, 2, 3, 4]


def test_parse_reply_tolerates_code_fence_and_prose():
    d = parse_reply('Sure:\n```json\n{"say": "ok", "overlay": null}\n```')
    assert d == {"say": "ok", "overlay": None}


@pytest.mark.parametrize("text", ["", "no json here", "{broken"])
def test_parse_reply_returns_none_on_garbage(text):
    assert parse_reply(text) is None


def test_zoom_unzoom_round_trip():
    Image = pytest.importorskip("PIL.Image")
    from hwmode.bench_ground import unzoom, zoom_crop

    im = Image.new("RGB", (1280, 960))
    box = [600.0, 400.0, 640.0, 430.0]
    crop, origin = zoom_crop(im, box)
    left, top, k = origin
    in_crop = [(box[0] - left) * k, (box[1] - top) * k, (box[2] - left) * k, (box[3] - top) * k]
    assert max(crop.size) == 768
    assert unzoom(in_crop, origin) == pytest.approx(box)
