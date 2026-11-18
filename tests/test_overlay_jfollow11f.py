"""jfollow11f: "take me to <place>", a misheard name matched by sound against Overpass names near the rounded
point ("Did you mean Dishoom?" when unsure), and the drawing's own grow line.

Network is always mocked (httpx.MockTransport, the Net of test_overlay_jfollow10f). The Overpass fixture is
the real answer for the 2-decimal Trafalgar point (public names only). Clocks are fake.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from common import hud  # noqa: E402
from gateway import overlay  # noqa: E402
from mac.voice import geo, intent_pick, intents, nearby  # noqa: E402
from test_overlay_jfollow10f import (HOME, ROW_RE, cards, client, clock, deps_with, fresh, fx,  # noqa: E402,F401
                                     last_line, net, phrase, steps, vault)
from test_overlay_service import says, turn  # noqa: E402

FX = ROOT / "tests" / "fixtures"
OVERPASS_HOST = httpx.URL(nearby.OVERPASS).host
POINT = (51.51, -0.13)
WORDS = ("dishoom", "decim", "disham", "dessun", "dessu", "this hun")
THREAD_SPAWN = nearby._spawn  # the real daemon-thread start (the net fixture swaps in a no-op)


@pytest.fixture(autouse=True)
def offers_clear():
    overlay._place_offer.clear()
    overlay._expired.clear()
    overlay._drawing_ids.clear()
    yield
    overlay._place_offer.clear()
    overlay._expired.clear()


@pytest.fixture
def world(net):
    """Nominatim knows "Dishoom" only (a misheard name finds nothing); Overpass answers the real pull."""
    def nominatim(request):
        q = request.url.params["q"].lower()
        return httpx.Response(200, json=fx("nominatim-dishoom.json") if q == "dishoom" else [])
    net.override["nominatim.openstreetmap.org"] = nominatim
    net.override[OVERPASS_HOST] = lambda r: httpx.Response(
        200, content=(FX / "places" / "overpass-trafalgar.txt").read_bytes())
    return net


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def place_deps(**kw):
    d = deps_with(**kw)
    d.clock = Clock()
    return d


def detail():
    return steps("place")[-1]["detail"]


def ask_turn(client, conv="C", words="Decim"):
    events = turn(client, f"take me to {words}", conv_id=conv)
    assert last_line(events) == "Did you mean Dishoom?" and " near=ask " in detail(), detail()
    assert conv in overlay._place_offer
    return events


# --- A1: the form ---------------------------------------------------------------------------------

def test_take_me_to_forms():
    apps = {"Safari": "/Applications/Safari.app"}

    def parse(t):
        return intents.parse(t, apps=apps, place_names=("home", "gym"))
    for t in ("take me to Dishoom", "take me to the nearest Dishoom", "take me to Decim"):
        assert parse(t).kind == "place", t
        assert intents.place_request(t, ("home", "gym"), apps)["query"] == t.split()[-1].lower()
    ask = intents.place_request("take me to the closest pharmacy", ("home", "gym"), apps)
    assert parse("take me to the closest pharmacy").kind == "place" and ask["mode"] == "category"
    assert ask["query"] == "pharmacy"
    for t in ("take me home", "take me to the gym", "take me to SW1A 2AA"):
        assert parse(t).kind == "geo_map", t
    for t in ("take me back", "take me through the changes", "take me to the top of the page",
              "take me to the next change", "take me to settings", "take me to Safari",
              "take me to https://example.com", "take me to it"):
        assert parse(t).kind != "place", t
        assert intents.place_request(t, ("home", "gym"), apps) is None, t
    assert parse("stop").kind == "stop"
    assert intents.PLACE_CUE_RE.search("take me to the gym")  # the service loads saved names for it


def test_take_me_to_keeps_other_intents():
    """Round 1 (code lens): "take me to <x>" loses to every other intent and never takes a screen/dev thing."""
    apps = {"Safari": "/Applications/Safari.app"}

    def parse(t):
        return intents.parse(t, apps=apps, place_names=("home", "gym"))
    for t in ("take me to my notes", "take me to the inbox", "take me to github", "take me to my mail",
              "take me to my calendar"):
        assert parse(t).kind == "agent", t  # as at BASE
    for t in ("take me to the dashboard", "take me to the file", "take me to the repo", "take me to the video",
              "take me to the window", "take me to the workspace", "take me to the overlay", "take me to the card",
              "take me to the map", "take me to the code", "take me to main", "take me to two minutes in",
              "take me to youtube", "take me to bed", "take me to lunch", "take me to my mum's"):
        assert parse(t).kind == "answer", t  # as at BASE
        assert intents.place_request(t, ("home", "gym"), apps) is None, t
    assert parse("take me to my gym").kind == "geo_map"  # a saved name with "my" still maps
    assert parse("take me to Dishoom").kind == "place"


def test_place_pick_wired(vault, client, world, monkeypatch):
    assert intent_pick.KIND_TO_PICK["place"] == "place"
    calls = []

    def pick(msg):
        calls.append(msg)
        return json.dumps({"pick": "place", "sub": "", "confidence": 0.95,
                           "slots": {"query": "evil words", "to": "elsewhere"}})
    d = place_deps()
    d.intent_pick = pick
    asks = []
    real = overlay._place_answer
    monkeypatch.setattr(overlay, "_place_answer", lambda ask, budget: asks.append(dict(ask)) or real(ask, budget))
    events = turn(client, "take me to Dishoom", conv_id="p1")
    assert asks == [intents.place_request("take me to Dishoom")]  # the owner's words, never the model slots
    assert cards(events) and "Dishoom" in last_line(events)
    # a place pick on words that are no place ask: an answer, as before
    word = intents.Intent("answer", "tell me about Dishoom's history")
    p = intent_pick.Pick("place", "", 95, {"query": "dishoom"})
    got, _extra, used = overlay._intent_from_pick(p, word, "tell me about Dishoom's history", "p1", d)
    assert got.kind == "answer" and used == 0
    p = intent_pick.Pick("place", "", 95, {})
    got, _extra, used = overlay._intent_from_pick(p, word, "take me to Dishoom", "p1", d)
    assert got == intents.Intent("place", "name") and used == 0


# --- A2/A3: the Overpass request and the match (security) ------------------------------------------

def test_overpass_request_is_fixed(vault, client, world):
    place_deps()
    turn(client, "take me to Decim", conv_id="o1")
    reqs = [r for r in world.requests if r.url.host == OVERPASS_HOST]
    assert len(reqs) == 1
    [req] = reqs
    assert req.method == "GET" and req.url.scheme == "https"
    assert str(req.url) == nearby.overpass_url(POINT)
    assert req.url.params["data"] == nearby.OVERPASS_QUERY.format(
        radius=nearby.OVERPASS_RADIUS_M, lat=51.51, lon=-0.13, cap=nearby.OVERPASS_CAP)
    assert set(req.url.params) == {"data"}
    seen = (str(req.url) + json.dumps(dict(req.headers)) + req.content.decode()).lower()
    assert "decim" not in seen and "51.508" not in seen and "-0.1281" not in seen


def test_hosts_with_overpass(net):
    base = {"valhalla1.openstreetmap.de", "routing.openstreetmap.de", "nominatim.openstreetmap.org",
            "api.postcodes.io", "api.tfl.gov.uk", "tile.openstreetmap.org"}
    assert geo.HOSTS == base | {"photon.komoot.io", "clusterflick.com", "overpass-api.de"}
    net.override[OVERPASS_HOST] = lambda r: httpx.Response(302, headers={"Location": "https://evil.example/"})
    with pytest.raises(geo.GeoError):
        geo._get(nearby.overpass_url(POINT), "overpass")
    assert len(net.requests) == 1
    net.requests.clear()
    with pytest.raises(geo.GeoError) as e:
        geo._get(nearby.overpass_url(POINT).replace("https://", "http://"), "overpass")
    assert e.value.reason == "bad_host" and net.requests == []
    # too big, or a timeout: op=down and the Photon retry runs
    for fail in (lambda r: httpx.Response(200, content=b"x\n" * (65 * 1024)),
                 lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r))):
        nearby.reset()
        net.requests.clear()
        net.override["nominatim.openstreetmap.org"] = lambda r: httpx.Response(200, json=[])
        net.override[OVERPASS_HOST] = fail
        b = nearby.Budget()
        src, found = nearby.search("name", "dishoom", HOME, b)
        assert b.op == "down" and src == "photon" and found and net.count("photon.komoot.io") == 1
    src = "\n".join(p.read_text() for p in (ROOT / "mac").rglob("*.py")) + "\n".join(
        p.read_text() for d in ("gateway", "common") for p in (ROOT / d).rglob("*.py"))
    assert src.count("overpass-api.de") == 2  # geo.HOSTS and the nearby.OVERPASS constant


def test_sound_alike_rows(vault, client, world):
    place_deps()
    table = {}
    for i, words in enumerate(("disham", "this hun", "decim", "dessun", "dessu")):
        nearby.reset()
        geo.reset()
        overlay._place_offer.clear()
        events = turn(client, f"take me to {words}", conv_id=f"s{i}")
        d = detail()
        assert ROW_RE.match(d) and " op=ok " in d
        if " near=hit " in d:
            [card] = cards(events)
            assert card["title"] == "Nearest Dishoom" and "src=overpass" in d and d.endswith("reason=ok")
            assert all(r["name"] == "Dishoom" for r in card["data"]["rows"])
            table[words] = "hit"
        else:
            assert " near=ask " in d and d.endswith("reason=ask") and not cards(events)
            assert last_line(events) == "Did you mean Dishoom?"
            table[words] = "ask"
    assert table == {"disham": "hit", "this hun": "ask", "decim": "ask", "dessun": "ask", "dessu": "ask"}
    nearby.reset()
    geo.reset()
    events = turn(client, "take me to purple walrus", conv_id="sm")
    assert not cards(events) and last_line(events) == phrase("place_none", query="Purple Walrus")
    assert " near=miss " in detail() and detail().endswith("reason=none_near")
    # "take me to the" trails off (the service waits for the rest), so "the" is checked on the search itself
    b = nearby.Budget()
    with pytest.raises(nearby.SearchFailed) as e:
        nearby.search("name", "the", HOME, b)
    assert e.value.key == "place_none" and b.near == "miss" and b.op == "cache"  # R6: names from the turn before
    nearby.reset()
    geo.reset()
    world.requests.clear()
    events = turn(client, "take me to Dishoom", conv_id="sn")
    assert cards(events) and " op=no near=no " in detail() and world.count(OVERPASS_HOST) == 0
    # a key two names share is never a hit (only an ask), and the keys behave as documented
    assert nearby.sound_alike("disham", ["Dishoom", "Dashama"])[0] == "ask"
    assert nearby.sound_alike("disham", ["Dishoom"]) == ("hit", "Dishoom")
    assert {nearby.sound_key(w) for w in ("dishoom", "disham", "this hun", "decim", "dessun")} == {"TSN"}
    assert nearby.sound_alike("the", ["The Two Brewers"]) == ("miss", "")


# --- A4: ask and act ---------------------------------------------------------------------------------

def test_offer_flow(vault, client, world):
    d = place_deps()
    calls = []
    d.intent_pick = lambda msg: calls.append(msg) or json.dumps(
        {"pick": "place" if "take me" in msg else "answer", "sub": "", "confidence": 0.9, "slots": {}})
    # (a) a follow-up "yes" that passes the level gate: the place turn with the offered name
    ask_turn(client, "C")
    events = turn(client, "yes", conv_id="C", followup=True)
    [card] = cards(events)
    assert card["type"] == "web" and card["page"] == "map"
    assert all(r["name"] == "Dishoom" for r in card["data"]["rows"]) and card["title"] == "Nearest Dishoom"
    assert "Dishoom" in last_line(events) and " near=yes " in detail() and detail().endswith("reason=ok")
    assert "C" not in overlay._place_offer
    # (b) "no": the one line, no request
    ask_turn(client, "C")
    world.requests.clear()
    events = turn(client, "no", conv_id="C", followup=True)
    assert says(events) == [phrase("place_offer_no")] and world.requests == [] and not cards(events)
    assert " near=declined " in detail() and detail().endswith("reason=declined")
    # (c) anything else: handled normally, the offer is gone
    ask_turn(client, "C")
    turn(client, "what time is it", conv_id="C")
    assert steps("intent")[-1]["kind"] == "clock" and "C" not in overlay._place_offer
    # (d) "yes" after 31 s: no place turn
    ask_turn(client, "C")
    n = len(steps("place"))
    d.clock.t += 31
    turn(client, "yes", conv_id="C")
    assert len(steps("place")) == n and "C" not in overlay._place_offer
    # (e) a barge-in "yes": geo_barge, no request, offer dropped
    ask_turn(client, "C")
    world.requests.clear()
    events = turn(client, "yes", conv_id="C", barge_in=True)
    assert says(events) == [phrase("geo_barge")] and world.requests == [] and "C" not in overlay._place_offer
    # (f) an agent Confirm pending on C: "yes" settles only the Confirm, the offer is dropped
    ask_turn(client, "C")
    n = len(steps("place"))
    # (the Confirm timed out a moment ago: the preflight still owns the yes and says the Confirm's line)
    overlay._expired["C"] = (d.clock(), "Push the branch")
    world.requests.clear()
    events = turn(client, "yes", conv_id="C")
    assert len(steps("place")) == n and world.requests == [] and "C" not in overlay._place_offer
    assert "C" not in overlay._expired and says(events) and "Dishoom" not in " ".join(says(events))
    # (g) an offer on C never answers a "yes" on D
    ask_turn(client, "C")
    n = len(steps("place"))
    turn(client, "yes", conv_id="D")
    assert len(steps("place")) == n and "C" in overlay._place_offer
    # (h) the picker is never asked while the offer is open
    calls.clear()
    turn(client, "tell me a joke about cats", conv_id="C")
    assert calls == [] and steps("intent_pick")[-1]["detail"].endswith("reason=pick_open")
    turn(client, "tell me a joke about dogs", conv_id="C")
    assert len(calls) == 1  # no offer now: the picker runs again


def test_offer_quiet_room_yes_is_dropped(vault, client, world):
    """A room "yes" below the owner's level never runs a search (the offer's yes passes the level gate)."""
    place_deps()
    turn(client, "take me to Decim", conv_id="Q", levels=[0.3] * 60)
    world.requests.clear()
    events = turn(client, "yes", conv_id="Q", followup=True, levels=[0.001] * 60)
    assert says(events) == [] and world.requests == [] and steps("followup_dropped")


def test_offer_privacy(vault, client, world):
    place_deps()
    events = ask_turn(client, "C")
    assert all(e.get("private") for e in events if e["type"] in ("say", "audio"))
    assert "C" in overlay._auto_tainted
    events = turn(client, "yes", conv_id="C", followup=True)
    assert all(e.get("private") for e in events if e["type"] in ("say", "audio"))
    turn(client, "take me to Disham", conv_id="C")
    assert {s["text"] for s in steps("spoke")} == {"[geo line]"}
    tags = [s["text"] for s in steps("transcribed") + steps("continued")]
    assert tags and set(tags) == {"[geo place]"}
    history = json.dumps(list(overlay._history["C"])).lower()
    assert history.count("(place question asked)") == 1 and "(place answer shown)" in history
    assert not any(w in history for w in ("dishoom", "decim", "disham"))
    for s in steps("place"):
        d = s["detail"]
        assert ROW_RE.match(d) and len(d) <= 200 and set(s) <= {"step", "at", "detail"}
        assert not any(w in d.lower() for w in WORDS + ("51.5", "-0.12"))
    # untrusted names: cleaned and capped before speech; links and card JSON stay plain words
    for name, check in (("Dishoom <script>alert(1)</script> ‮evil​ " + "y" * 300,
                         lambda line: "‮" not in line and "​" not in line and len(line) <= 60),
                        ('Dishoom https://evil.example {"card": 1}', lambda line: "https://evil.example" in line)):
        nearby.reset()
        overlay._place_offer.clear()
        world.override[OVERPASS_HOST] = lambda r, n=name: httpx.Response(200, content=n.encode() + b"\n")
        events = turn(client, "take me to Decim", conv_id="U")
        line = last_line(events)
        assert line.startswith("Did you mean Dishoom") and check(line), line
        assert not [e for e in events if e["type"] in ("card", "link")]


def test_calls_per_question(vault, client, world, tmp_path):
    place_deps()
    before = sorted(p for p in tmp_path.rglob("*") if p.is_file())

    def asked(words, conv, **kw):
        world.requests.clear()
        turn(client, words, conv_id=conv, **kw)
        return list(world.requests)

    reqs = asked("take me to Dishoom", "k1")
    assert len(reqs) <= 6
    nearby.reset()
    geo.reset()
    reqs = asked("take me to Disham", "k2")
    assert len(reqs) <= 7 and " near=hit " in detail()
    nearby.reset()
    geo.reset()
    world.override[OVERPASS_HOST] = lambda r: httpx.Response(503)
    reqs = asked("take me to Disham", "k3")  # R5-PHOTON: Photon's "Dishoom" is not the heard name: an offer
    assert len(reqs) <= 3 and " op=down near=ask " in detail()
    nearby.reset()
    geo.reset()
    world.override["nominatim.openstreetmap.org"] = lambda r: httpx.Response(200, json=[])
    reqs = asked("take me to Dishoom", "k3b")  # Overpass down, Photon has the heard name: the down hit
    assert len(reqs) <= 7 and " op=down near=hit " in detail()
    assert [r for r in reqs if r.url.host in ("api.tfl.gov.uk", "valhalla1.openstreetmap.de")]
    world.override["nominatim.openstreetmap.org"] = lambda r: httpx.Response(
        200, json=fx("nominatim-dishoom.json") if r.url.params["q"].lower() == "dishoom" else [])
    world.override[OVERPASS_HOST] = lambda r: httpx.Response(
        200, content=(FX / "places" / "overpass-trafalgar.txt").read_bytes())
    nearby.reset()
    geo.reset()
    reqs = asked("take me to Decim", "k4")
    assert len(reqs) <= 3 and " near=ask " in detail()
    assert not [r for r in reqs if r.url.host in ("api.tfl.gov.uk", "valhalla1.openstreetmap.de")]
    reqs = asked("yes", "k4", followup=True)
    assert len(reqs) <= 5 and " near=yes " in detail()
    reqs = asked("take me to Dessun", "k5")  # within 10 min: the cached names, no second Overpass call
    assert not [r for r in reqs if r.url.host == OVERPASS_HOST] and " near=ask " in detail()
    after = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    assert after == before  # nothing written to disk


def test_photon_fallback_offers(vault, client, world):
    """Hub ruling R5-PHOTON: Overpass slow (op=down) and Photon has "Decimo" for "decim": asked, never stated.
    Only a Photon name that IS the heard name is a sure hit. Nominatim never knows the offered name here, so
    the yes turn's card comes from the Photon hits the offer turn kept (no second offer, no extra call)."""
    place_deps()
    offered = {"name": "Decimo"}

    def photon(request):
        feats = fx("photon-dishoom.json")["features"]
        name = offered["name"] if request.url.params["q"] == "decim" else "Dessun"
        return httpx.Response(200, json={"features": [
            {**f, "properties": {**f["properties"], "name": name}} for f in feats]})

    world.override[OVERPASS_HOST] = lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r))
    world.override["photon.komoot.io"] = photon
    slow = ("api.tfl.gov.uk", "valhalla1.openstreetmap.de")

    def offer(conv):
        nearby.reset()
        world.requests.clear()
        events = turn(client, "take me to Decim", conv_id=conv)
        assert last_line(events) == f"Did you mean {offered['name']}?" and not cards(events)
        assert " op=down near=ask " in detail() and detail().endswith("reason=ask") and ROW_RE.match(detail())
        assert len(world.requests) <= 3 and not [r for r in world.requests if r.url.host in slow]
        assert conv in overlay._place_offer and conv in overlay._auto_tainted
        assert all(e.get("private") for e in events if e["type"] in ("say", "audio"))
        assert not any(w in detail().lower() for w in ("decim", "decimo"))

    for name in ("Decimo", "The Decimo"):  # a leading "the" must not turn the yes into a second offer
        offered["name"] = name
        offer("P")
        world.requests.clear()
        events = turn(client, "yes", conv_id="P", followup=True)
        [card] = cards(events)
        assert card["title"] == f"Nearest {name}" and all(r["name"] == name for r in card["data"]["rows"])
        assert name in last_line(events) and " near=yes " in detail() and detail().endswith("reason=ok")
        assert len(world.requests) <= 5 and not [r for r in world.requests
                                                 if r.url.host in (OVERPASS_HOST, "photon.komoot.io")]
        assert "P" not in overlay._place_offer
    offered["name"] = "Decimo"
    offer("P")
    world.requests.clear()
    events = turn(client, "no", conv_id="P", followup=True)
    assert says(events) == [phrase("place_offer_no")] and world.requests == [] and not cards(events)
    assert " near=declined " in detail() and detail().endswith("reason=declined")
    # Photon has the very name that was heard: a sure hit, as before
    nearby.reset()
    b = nearby.Budget()
    src, found = nearby.search("name", "dessun", HOME, b)
    assert src == "photon" and found[0]["name"] == "Dessun" and b.op == "down" and b.near == "hit"
    # below NEAR_RATIO: a miss, as before
    nearby.reset()
    with pytest.raises(nearby.SearchFailed) as e:
        nearby.search("name", "purple walrus", HOME, nearby.Budget())
    assert e.value.key == "place_none"


# --- R6-NAMES: the names fetched in the background after a slow Overpass, kept 6 hours ----------

def _slow(request):
    raise httpx.ReadTimeout("slow", request=request)


def _search(words, b=None):
    """nearby.search for a name; a place_offer / place_none is a normal outcome here."""
    b = b or nearby.Budget()
    try:
        return b, nearby.search("name", words, HOME, b)[1]
    except nearby.SearchFailed as e:
        return b, e


def test_names_cache_after_slow_overpass(vault, client, world, monkeypatch):
    """Turn 1 times out on Overpass (op=down) and starts ONE background fetch; once it is done the next
    misheard turn matches from the cache (op=cache, no Overpass request), with the yes and the sure hit."""
    place_deps()
    nearby.reset()
    geo.reset()
    jobs = []
    monkeypatch.setattr(nearby, "_spawn", jobs.append)
    good = world.override[OVERPASS_HOST]
    world.override[OVERPASS_HOST] = _slow
    turn(client, "take me to Decim", conv_id="r1")
    assert " op=down " in detail() and ROW_RE.match(detail()) and len(jobs) == 1
    turn(client, "take me to Dessun", conv_id="r2")  # a second timeout while the fetch is alive
    assert " op=down " in detail() and len(jobs) == 1
    world.override[OVERPASS_HOST] = good
    world.requests.clear()
    jobs[0]()  # the background fetch finishes
    assert world.count(OVERPASS_HOST) == 1 and len(nearby._overpass) == 1 and not nearby._bg_alive[0]
    assert world.requests[0].url == nearby.overpass_url(nearby.search_point(HOME))  # the same GET, nothing new
    world.requests.clear()
    events = turn(client, "take me to Decim", conv_id="r3")
    assert last_line(events) == "Did you mean Dishoom?" and not cards(events)
    assert " op=cache near=ask " in detail() and detail().endswith("reason=ask") and ROW_RE.match(detail())
    assert world.count(OVERPASS_HOST) == 0 and not any(w in detail().lower() for w in ("decim", "dishoom"))
    events = turn(client, "yes", conv_id="r3", followup=True)
    [card] = cards(events)
    assert card["title"] == "Nearest Dishoom" and " near=yes " in detail() and detail().endswith("reason=ok")
    world.requests.clear()
    events = turn(client, "take me to disham", conv_id="r4")
    [card] = cards(events)
    assert card["title"] == "Nearest Dishoom" and " op=cache near=hit " in detail()
    assert world.count(OVERPASS_HOST) == 0 and len(jobs) == 1


def test_names_cache_calls_and_age(world, clock, monkeypatch):
    """op=cache is one call fewer than op=ok; an entry older than 6 h means a live call again; a live op=ok
    call fills the cache too; at most NAMES_KEEP points are kept."""
    monkeypatch.setattr(nearby, "_spawn", lambda fn: pytest.fail("no background fetch on a good Overpass"))
    nearby.reset()
    b_ok, _ = _search("decim")
    b_cache, _ = _search("decim")
    assert (b_ok.op, b_cache.op) == ("ok", "cache") and b_cache.calls == b_ok.calls - 1
    assert world.count(OVERPASS_HOST) == 1
    clock.t += nearby.NAMES_TTL_S + 1
    b, _ = _search("decim")
    assert b.op == "ok" and world.count(OVERPASS_HOST) == 2
    for i in range(nearby.NAMES_KEEP + 2):
        nearby._keep_names((51.0 + i / 100, 0.0), "Cafe\n")
    assert len(nearby._overpass) == nearby.NAMES_KEEP


def test_names_background_failure_and_caps(world, clock, monkeypatch):
    """A background fetch failing twice (502, then a timeout) leaves no entry and the next turn is op=down
    again, no exception; one fetch alive at a time; no more than 4 started per hour."""
    nearby.reset()
    jobs = []
    monkeypatch.setattr(nearby, "_spawn", jobs.append)
    answers = iter([httpx.Response(502, text="at capacity")])
    world.override[OVERPASS_HOST] = lambda r: next(answers, None) or _slow(r)
    b, _ = _search("decim")
    assert b.op == "down" and len(jobs) == 1
    world.requests.clear()
    jobs[0]()
    assert world.count(OVERPASS_HOST) == 2 and not nearby._overpass and not nearby._bg_alive[0]
    b, _ = _search("decim")
    assert b.op == "down" and len(jobs) == 2  # a new fetch: the last one ended
    _search("decim")
    assert len(jobs) == 2  # job 2 still alive: no second thread
    jobs[1]()
    for _ in range(4):
        _search("decim")
        jobs[-1]()
    assert len(jobs) == 4  # the hourly cap
    clock.t += 3600
    _search("decim")
    assert len(jobs) == 5
    # a thread that cannot start holds nothing
    nearby.reset()
    monkeypatch.setattr(nearby, "_spawn", lambda fn: (_ for _ in ()).throw(RuntimeError("no thread")))
    b, _ = _search("decim")
    assert b.op == "down" and not nearby._bg_alive[0]


def test_names_background_real_thread(world, monkeypatch):
    """The real start: a daemon thread fills the cache while the turn has already returned."""
    import threading
    import time
    nearby.reset()
    gate = threading.Event()
    good = world.override[OVERPASS_HOST]
    first = []

    def overpass(r):
        if not first:  # the turn's own call times out
            first.append(1)
            return _slow(r)
        assert gate.wait(5)  # the background call waits until the turn is over
        return good(r)

    world.override[OVERPASS_HOST] = overpass
    monkeypatch.setattr(nearby, "_spawn", THREAD_SPAWN)
    b, _ = _search("decim")
    assert b.op == "down" and nearby._bg_alive[0] and not nearby._overpass
    gate.set()
    end = time.monotonic() + 5
    while nearby._bg_alive[0] and time.monotonic() < end:
        time.sleep(0.01)
    assert not nearby._bg_alive[0] and len(nearby._overpass) == 1
    b, e = _search("decim")
    assert b.op == "cache" and isinstance(e, nearby.SearchFailed) and e.name == "Dishoom"


# --- D: the drawing's grow line -------------------------------------------------------------------

def test_drawing_grow_line(client):
    deps_with()
    svg = json.loads((FX / "hud" / "web_drawing.json").read_text())["cards"][0]["data"]["svg"]
    drawing = hud.drawing_card("Drawing", svg, ["1 a"])
    overlay._index_card("g1", drawing)
    events = turn(client, "make the drawing bigger", conv_id="g1")
    assert says(events) == [phrase("drawing_no_enlarge")]
    assert not [e for e in events if e["type"] == "card_size"]
    overlay._index_card("g2", {"id": "pl-12345678", "type": "web", "page": "map", "title": "Map"})
    events = turn(client, "make the map bigger", conv_id="g2")
    assert says(events) == [phrase("web_no_enlarge")]
    overlay._index_card("g3", {"id": "im1", "type": "image", "title": "Cat"})
    events = turn(client, "make the cat bigger", conv_id="g3")
    assert [e for e in events if e["type"] == "card_size"] == [{"type": "card_size", "id": "im1", "size": "max"}]
    assert re.fullmatch(r"[A-Za-z .]+", phrase("drawing_no_enlarge"))
