"""ground.parse_id: the one candidate-id parser, used by ground.choose and the service's _point_at."""

import pytest

from mac.voice.ground import build_candidates, choose, parse_id

CANDS = build_candidates([], [{"text": "Export", "box": [10, 10, 90, 30]},
                              {"text": "Share", "box": [110, 10, 190, 30]}])


def _choose(reply):
    return choose("highlight it", CANDS, None, lambda p, j: reply)


def test_odd_digit_strings_are_unknown():
    for reply in ('{"id": "²"}', '{"id": "٣"}', '{"id": "99999999999999999999"}',
                  '{"id": "1234567"}'):
        c, r = _choose(reply)
        assert (c, r) == (None, "unknown_id"), (reply, c, r)


def test_whole_float_picks():
    c, r = _choose('{"id": 2.0}')
    assert r == "picked" and c is not None and c["id"] == 2
    c, r = _choose('{"id": " 2 "}')
    assert r == "picked" and c is not None and c["id"] == 2


def test_bad_numbers_are_unknown():
    for reply in ('{"id": 2.5}', '{"id": true}', '{"id": false}', '{"id": "abc"}',
                  '{"id": 7}'):
        c, r = _choose(reply)
        assert (c, r) == (None, "unknown_id"), (reply, c, r)
    assert parse_id(float("nan")) is None
    assert parse_id(float("inf")) is None


def test_null_stays_null():
    assert _choose('{"id": null}') == (None, "null")


@pytest.mark.parametrize("value,expected", [
    (1, 1), (2.0, 2), ("000123", 123), ("1234567", None), ("²", None),
    ("٣", None), (True, None), (None, None), ([1], None),
])
def test_parse_id_table(value, expected):
    assert parse_id(value) == expected
