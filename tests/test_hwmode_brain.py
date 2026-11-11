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


class _FakePopen:
    def __init__(self, *a, **kw):
        self.cwd = kw["cwd"]
        self.stdin = self
    def poll(self):
        return None
    def close(self):
        pass
    def terminate(self):
        pass
    def wait(self, timeout=None):
        return 0
    def kill(self):
        pass


def test_close_removes_session_temp_dir(monkeypatch):
    import os
    from hwmode.brain import LeanSession
    monkeypatch.setattr("hwmode.brain.subprocess.Popen", _FakePopen)
    s = LeanSession()
    assert os.path.isdir(s._cwd)
    s.close()
    assert not os.path.exists(s._cwd)
    s.close()  # second close is a no-op


def test_failed_start_removes_temp_dir(monkeypatch):
    import os
    from hwmode import brain
    made = []
    real = brain.tempfile.mkdtemp
    monkeypatch.setattr("hwmode.brain.tempfile.mkdtemp", lambda **kw: made.append(real(**kw)) or made[-1])
    def boom(*a, **kw):
        raise FileNotFoundError("no claude")
    monkeypatch.setattr("hwmode.brain.subprocess.Popen", boom)
    with pytest.raises(FileNotFoundError):
        brain.LeanSession()
    assert made and not os.path.exists(made[0])


# Tail of SYSTEM_PROMPT at d0b060f (git show d0b060f:hwmode/brain.py): the JSON contract must not drift.
_TAIL_D0B060F = """Reply with ONLY one JSON object, no prose, no code fence, keys in this order:
{"next_ready": true only when this reply gives a NEW instruction because the user said the current one is done
   or the image shows it done, else false,
 "done_seen": true only if THIS image clearly shows the current instruction already finished, else false,
 "say": what you say,
 "shape": null or {"kind": "rect"|"circle"|"triangle", "box": [x1,y1,x2,y2] pixel box in the image you see,
   tight around the exact ONE part to touch, "label": at most 4 words}}
The shape kind: "rect" by default; "circle" for round parts (screws, round buttons, LEDs); "triangle" only to mark
a hazard. The phone draws the shape: never describe its colour or look in "say" ("outlined in red").
Instructions are given one at a time: never number them, never say how many there are, never say "step" with a
number.
Answer what the user just asked FIRST, even if it is about something other than the current instruction ("point at
the SATA drive" midway): box that thing and answer; then, only if useful, one short reminder of the current
instruction.
"Is this it?" / "this one?" / "here?": look at what their finger, hand or tool points at (or the centre of the image
if none is visible) and answer yes or no about THAT spot; if no, box the right part and say where it is from there.
Never repeat your previous sentence; if the answer is the same, say it differently and add what is new.
Point, don't just talk: whenever the part the current instruction needs (or the user asks about) is visible in the
image, give a shape on it. Use null only when it is not in view, and then say where to aim the camera.
The box is for ONE part: if an instruction touches several (e.g. "unplug every cable"), box the first one and say so.
Plan ONLY what the user asked for, nothing they did not ask. "Find / show me / where is / what is X" is the whole
job: that one instruction (point at it), no extra jobs (no unplugging, powering down, opening a case) unless they
ask for that.
Add a safety instruction (power off before unplugging) only when the asked job itself needs it. When the asked job
is done, say so in one line and give no more instructions: just answer questions.
Give a NEW instruction (next_ready true) ONLY when the user says the current one is done ("done", "finished",
"next", "skip"), or in a [watch] check when the image clearly SHOWS it finished (then also set done_seen true).
Instructions whose result can't be seen (power off, unplugged at the wall) need the user's "done".
A question is never a reason to move on. If the user asks to go back, guide the earlier instruction again.
A [locate] message means the phone lost the part: box the same part again in the new image, with "say": ""
(or shape null and say where to aim if it is not in view). Never give a new instruction for it.
A [zoom] message is a close-up crop: box the same part again in that crop's pixels, same JSON."""

_PERSONA_CAP = 120
_PROMPT_CAP = 705


def _persona_part(prompt: str) -> str:
    return prompt[prompt.index("How you speak"):prompt.index("Reply with ONLY one JSON object")]


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_system_prompt_persona_rules_present():
    from hwmode.brain import SYSTEM_PROMPT
    p = _flat(_persona_part(SYSTEM_PROMPT))
    for phrase in (
        "composed and fond of the user",
        "about one reply in four",
        "never two in a row",
        '"Sir" at most once in a reply, not every reply',
        "No humour, plain and brief",
        "hazard",
        "triangle",
        "damage",
        "lost or broken part",
        "curt",
        "Never claim what you did not see or do",
        "say when you do not know",
    ):
        assert phrase in p, phrase


def test_system_prompt_old_butler_gone_and_kept_rules():
    from hwmode.brain import SYSTEM_PROMPT
    assert "butler" not in SYSTEM_PROMPT
    assert "impeccably" not in SYSTEM_PROMPT
    assert "Always, sir" not in SYSTEM_PROMPT
    assert SYSTEM_PROMPT.startswith(
        "You are JARVIS, a hands-on hardware coach looking through the user's phone camera."
    )
    flat = _flat(SYSTEM_PROMPT)
    assert "one short sentence, two at most" in flat
    assert "No exclamation marks, no emoji, no filler" in flat
    assert "never repeat the user's words back" in flat


def test_system_prompt_word_caps():
    from hwmode.brain import SYSTEM_PROMPT
    assert len(_persona_part(SYSTEM_PROMPT).split()) <= _PERSONA_CAP
    assert len(SYSTEM_PROMPT.split()) <= _PROMPT_CAP


def test_system_prompt_json_contract_tail_unchanged():
    from hwmode.brain import SYSTEM_PROMPT
    assert SYSTEM_PROMPT[SYSTEM_PROMPT.index("Reply with ONLY one JSON object"):] == _TAIL_D0B060F


def test_research_prompt_persona():
    from hwmode.research import RESEARCH_PROMPT
    assert "butler" not in RESEARCH_PROMPT
    assert "impeccably" not in RESEARCH_PROMPT
    flat = _flat(RESEARCH_PROMPT)
    assert 'Say "sir" at most once in a reply, not every reply' in flat
    assert "about one reply in four" in flat
    assert "No humour, plain and brief" in flat
    assert "two to four short sentences" in flat
    assert "Reply with ONLY one JSON object" in RESEARCH_PROMPT


def test_hwmode_does_not_import_gateway_overlay():
    import pathlib
    import hwmode
    for f in pathlib.Path(hwmode.__file__).parent.glob("*.py"):
        assert "gateway.overlay" not in f.read_text(encoding="utf-8"), f.name
