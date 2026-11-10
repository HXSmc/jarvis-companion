"""Hardware Mode brain: reply parsing, system-prompt contract, zoom-crop round trip (no model calls)."""

import pytest

from hwmode.brain import parse_reply


def test_parse_reply_plain_json():
    d = parse_reply('{"next_ready": false, "say": "Press it.", "shape": {"kind": "rect", "box": [1, 2, 3, 4], "label": "latch"}}')
    assert d["shape"]["box"] == [1, 2, 3, 4] and d["next_ready"] is False


def test_parse_reply_tolerates_code_fence_and_prose():
    d = parse_reply('Sure:\n```json\n{"say": "ok", "shape": null}\n```')
    assert d == {"say": "ok", "shape": None}


@pytest.mark.parametrize("text", ["", "no json here", "{broken"])
def test_parse_reply_returns_none_on_garbage(text):
    assert parse_reply(text) is None


def test_system_prompt_contract_keys_in_order():
    from hwmode.brain import SYSTEM_PROMPT
    i = SYSTEM_PROMPT.index('"next_ready"')
    pos = [SYSTEM_PROMPT.index(f'"{k}"', i) for k in ("next_ready", "done_seen", "say", "shape")]
    assert pos == sorted(pos)  # the JSON contract lists its keys in this order


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
