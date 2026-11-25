"""j3d: spoken animated/physics create phrases ("make a bouncing ball", "animate a paper plane",
"drop five boxes on a table") -> model_create, through the same gates as the typed 3D create.
Companion and network are faked; the vault is a tmp dir with invented names."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from gateway import overlay  # noqa: E402
from mac.voice import acks, intents, models3d  # noqa: E402
from test_overlay_jfollow10b import _companion, client, deps_with, fresh  # noqa: E402,F401
from test_overlay_jfollow10d import mvault  # noqa: E402,F401
from test_overlay_service import says, turn  # noqa: E402

NEGATIVE_KINDS = {"draw a bouncing ball": "draw", "drop a pin on the map": "answer",
                  "show me a picture of a bouncing ball": "answer", "stop the animation": "answer",
                  "make a note": "answer", "animate": "answer", "what time is it": "clock",
                  "make a rolling pin": "answer", "make a spinning wheel": "answer",
                  "throw a party on the roof": "answer"}
MOTION_WORDS = ("make a bouncing ball", "can you make me a spinning top",
                "please create an animated windmill", "animate a paper plane",
                "could you animate the test mug", "drop five boxes on a table",
                "drop a ball onto the floor", "make a 3D model of a bouncing ball, call it bouncy")


# --- criterion 14: the phrases and the kinds they must keep ----------------------------------------

def test_animated_create_phrases():
    p = intents.parse
    pinned = {"make a bouncing ball": "bouncing ball",
              "animate a paper plane": "animated paper plane",
              "drop five boxes on a table": "drop five boxes on a table"}
    for words, arg in pinned.items():
        a = p(words, apps={})
        assert (a.kind, a.arg) == ("model_create", arg), words
    for words in ("can you make me a spinning top", "please create an animated windmill",
                  "could you animate the test mug", "drop a ball onto the floor"):
        assert p(words, apps={}).kind == "model_create", words
    # the "3d" word still takes the old _MODEL_CREATE_RE path, and a name splits off it
    a = p("make a 3d bouncing ball", apps={})
    assert (a.kind, a.arg) == ("model_create", "bouncing ball")
    a = p("make a 3D model of a bouncing ball, call it bouncy", apps={})
    assert (a.kind, a.sub) == ("model_create", "bouncy")
    # non-3D things ("gif", "chart") never start a job
    for words in ("make an animated gif", "animate the chart"):
        assert p(words, apps={}).kind == "answer", words
    for words, kind in NEGATIVE_KINDS.items():
        assert p(words, apps={}).kind == kind, words


def test_animated_phrases_have_motion():
    if not hasattr(models3d, "motion"):  # the builder deletes this line when H1 lands
        pytest.skip("models3d.motion lands with H1")
    for words in MOTION_WORDS:
        arg = intents.parse(words, apps={}).arg
        assert models3d.motion(arg) != "none", words


# --- criterion 15: the create gates on the spoken path ---------------------------------------------

def _cancel_jobs():
    for t in list(overlay._jobs):
        t.cancel()


def test_animated_create_gates(client, monkeypatch, tmp_path, mvault):
    seen = _companion(monkeypatch, tmp_path, lambda r: httpx.Response(200, json={"session_id": "s9"}))
    deps_with()
    words = "make a bouncing ball"
    assert says(turn(client, words, conv_id="g1", barge_in=True)) == [acks.PHRASES["geo_barge"]] and seen == []
    now = overlay.get_deps().clock()
    overlay._session_calls["job"] = [now] * 3  # e.g. two "watch it" + one 3D job this hour
    assert says(turn(client, words, conv_id="g1")) == [acks.PHRASES["model_limit"]] and seen == []
    overlay._session_calls.clear()
    assert says(turn(client, words, conv_id="g1")) == [acks.PHRASES["job_started"]]
    posts = [body for m, _p, body in seen if m == "POST"]
    assert len(posts) == 1
    posted = json.loads(posts[0])["text"]  # _job_runner POSTs {"msg_id", ..., "text": prompt}
    slug = models3d.new_slug(str(mvault), "", "bouncing ball")
    assert posted == models3d.blender_job_prompt(slug, "bouncing ball")
    assert posted.count("<object_description") == 1 and posted.count("</object_description") == 1
    _cancel_jobs()
