"""General places near home (jfollow10f): search, rank by door-to-door time, cinemas by what he can still catch.

Fixed code only, no model. Every request goes through geo._get / geo._post (https, an exact host from
geo.HOSTS, no redirects, a byte cap per kind). What each host sees:
- Nominatim / Photon: the owner's place words and a box (or point) from home rounded to 2 decimals (~1 km);
  never the exact home. Cinemas skip place search: the Clusterflick index is ranked here, locally.
- Clusterflick: two fixed static paths (the index, a venue file built from a checked venue id); no query.
- Valhalla matrix and TfL Journey: numbers only (home and the picked places), as stage B already sends.
Untrusted web text (names, roads, hours, film titles) is cleaned with hud.place_text and capped; it is shown
and spoken only, never put in a URL, a prompt or a row. Nothing here logs a name, query, film or coordinate.
"""

from __future__ import annotations

import math
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from string import capwords
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from common import hud
from mac.voice import geo

LONDON = ZoneInfo("Europe/London")
LONDON_BOX = (51.28, -0.52, 51.70, 0.34)  # south, west, north, east
PHOTON = "https://photon.komoot.io/api/"
CF_INDEX = "https://clusterflick.com/datasets/index.json"
CF_VENUE = "https://clusterflick.com/datasets/venues/{}.json"
_VENUE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,79}$")
MAX_KM, MAX_ROWS, TFL_ROWS = 25.0, 5, 3
INDEX_TTL_S, VENUE_TTL_S, SEARCH_TTL_S = 24 * 3600, 15 * 60, 10 * 60
STALE_S = 36 * 3600
TURN_S = 15.0  # the whole place turn gives up here with what it has
BYTES_PER_QUESTION = 1536 * 1024
REACH_PAD_MIN = 10
SHOWS_DOWN_LEAD = "I can't see showtimes right now."
NOT_LISTED = "hours not listed"
MODES = ("name", "category", "cinema", "film", "whats_on")
HOW = {"walk": "on foot", "bus": "by bus", "tube": "by tube", "train": "by train"}
_TFL_MODES = {"bus": "bus", "tube": "tube", "dlr": "tube", "overground": "tube", "elizabeth-line": "tube",
              "national-rail": "train"}

now = lambda: datetime.now(LONDON)  # noqa: E731 (tests replace the wall clock; geo.clock stays monotonic)
_lock = threading.Lock()
_index: list = []  # [stored at, parsed index]: the last good index, kept past its TTL for the down path
_venues: dict[str, tuple[float, list]] = {}
_searches: dict[tuple, tuple[float, str, list]] = {}


def reset() -> None:
    """Tests: drop every cache (memory only; nothing here is ever written to disk)."""
    with _lock:
        _index.clear()
        _venues.clear()
        _searches.clear()


class Budget(geo.Budget):
    """geo.Budget (calls, TfL, Nominatim) + bytes fetched, the 15 s deadline and the row flags."""

    def __init__(self) -> None:
        super().__init__()
        self.bytes = self.matrix = self.shows = 0
        self.deadline = geo.clock() + TURN_S
        self.timed_out = False

    def left(self) -> bool:
        if geo.clock() > self.deadline:
            self.timed_out = True
        return not self.timed_out


def _fetch(url: str, kind: str, budget: Budget, body: dict | None = None) -> bytes:
    if not budget.left():
        raise geo.GeoError("timeout")
    budget.calls += 1
    data = geo._post(url, body, kind) if body is not None else geo._get(url, kind)
    budget.bytes += len(data)
    return data


def km_between(a: tuple, b: tuple) -> float:
    """Straight-line (haversine) km."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742.0 * math.asin(math.sqrt(h))


def in_london(point: tuple) -> bool:
    s, w, n, e = LONDON_BOX
    return s <= point[0] <= n and w <= point[1] <= e


def _point(lat, lon) -> tuple[float, float] | None:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


# --- place search (Nominatim, Photon fallback) -------------------------------------------------

def search_point(home: tuple) -> tuple[float, float]:
    """Home rounded to 2 decimals (~1 km): the only form of home a place search ever sees."""
    return round(home[0], 2), round(home[1], 2)


def search_box(point: tuple, wide: bool = False) -> tuple[float, float, float, float]:
    """(west, north, east, south) around the rounded point: ~11 km square, or ~33 km when widened."""
    dlat, dlon = (0.15, 0.24) if wide else (0.05, 0.08)
    return (round(point[1] - dlon, 2), round(point[0] + dlat, 2), round(point[1] + dlon, 2), round(point[0] - dlat, 2))


def nominatim_search_url(query: str, box: tuple) -> str:
    params = {"q": query, "format": "jsonv2", "viewbox": ",".join(f"{v:.2f}" for v in box), "bounded": "1",
              "extratags": "1", "addressdetails": "1", "countrycodes": "gb", "limit": "20"}
    return geo.NOMINATIM + "?" + urlencode(params, quote_via=quote)


def photon_url(query: str, point: tuple) -> str:
    params = {"q": query, "lat": f"{point[0]:.2f}", "lon": f"{point[1]:.2f}", "limit": "8"}
    return PHOTON + "?" + urlencode(params, quote_via=quote)


def _nominatim(url: str, budget: Budget):
    """One search under stage B's Nominatim rules: <= 2 per question, >= 1 s apart (shared lock)."""
    if budget.nominatim >= geo.NOMINATIM_PER_QUESTION:
        raise geo.GeoError("rate_cap")
    with geo._nominatim_lock:
        if geo._nominatim_last:
            wait = 1.0 - (geo.clock() - geo._nominatim_last[-1])
            if wait > 0:
                geo.sleep(wait)
        budget.nominatim += 1
        geo._nominatim_last[:] = [geo.clock()]
        return geo._json(_fetch(url, "nominatim_search", budget))


def _nominatim_hits(doc) -> list[dict]:
    out = []
    for h in doc if isinstance(doc, list) else []:
        if isinstance(h, dict):
            addr, tags = h.get("address"), h.get("extratags")
            out.append({"lat": h.get("lat"), "lon": h.get("lon"), "name": h.get("name"),
                        "road": addr.get("road") if isinstance(addr, dict) else "",
                        "hours": tags.get("opening_hours") if isinstance(tags, dict) else ""})
    return out


def _photon_hits(doc) -> list[dict]:
    out = []
    feats = doc.get("features") if isinstance(doc, dict) else None
    for f in feats if isinstance(feats, list) else []:
        if not isinstance(f, dict):
            continue
        coords = (f.get("geometry") or {}).get("coordinates") if isinstance(f.get("geometry"), dict) else None
        props = f.get("properties") if isinstance(f.get("properties"), dict) else {}
        if isinstance(coords, list) and len(coords) == 2:
            out.append({"lat": coords[1], "lon": coords[0], "name": props.get("name"),
                        "road": props.get("street"), "hours": ""})
    return out


def candidates(hits: list[dict], home: tuple, fallback_name: str = "") -> list[dict]:
    """Usable hits: in-range point, <= 25 km straight line from home, a name (category hits with none take
    the category word), same name within 50 m once; nearest first, <= 5."""
    found = []
    for h in hits:
        p = _point(h.get("lat"), h.get("lon"))
        name = hud.place_text(h.get("name"), hud.MAX_LABEL) or fallback_name
        if p is None or not name:
            continue
        km = km_between(home, p)
        if km > MAX_KM:
            continue
        hours = h.get("hours")
        found.append({"name": name, "road": hud.place_text(h.get("road"), hud.MAX_LABEL), "lat": p[0], "lon": p[1],
                      "km": km, "hours_raw": hours[:200] if isinstance(hours, str) else ""})
    found.sort(key=lambda c: c["km"])
    kept: list[dict] = []
    for c in found:
        if not any(k["name"].lower() == c["name"].lower() and km_between((k["lat"], k["lon"]), (c["lat"], c["lon"])) < 0.05
                   for k in kept):
            kept.append(c)
    return kept[:MAX_ROWS]


class SearchFailed(Exception):
    def __init__(self, key: str, reason: str):
        super().__init__(key)
        self.key, self.reason = key, reason


def _search_reason(e: geo.GeoError) -> str:
    return e.reason if e.reason in ("rate_cap", "timeout", "too_big") else "search_down"


def search(mode: str, query: str, home: tuple, budget: Budget, widen: bool = True) -> tuple[str, list[dict]]:
    """(src, <= 5 candidates) for a name or category: Nominatim on the small box, then once more on the
    wide box when that had 0 usable hits; Photon (names only) when the first Nominatim call fails.
    At most 2 search calls. Raises SearchFailed(place_down | place_none, reason)."""
    point = search_point(home)
    box = search_box(point)
    key = (query, box)
    fallback = query if mode == "category" else ""
    with _lock:
        hit = _searches.get(key)
    if hit and geo.clock() - hit[0] < SEARCH_TTL_S:
        return hit[1], candidates(hit[2], home, fallback)
    src = "nominatim"
    try:
        hits = _nominatim_hits(_nominatim(nominatim_search_url(query, box), budget))
    except geo.GeoError as e:
        if mode != "name":  # Photon drifts off London for categories: say so instead
            raise SearchFailed("place_down", _search_reason(e)) from None
        try:
            src, hits = "photon", _photon_hits(geo._json(_fetch(photon_url(query, point), "photon", budget)))
        except geo.GeoError as e2:
            raise SearchFailed("place_down", _search_reason(e2)) from None
    found = candidates(hits, home, fallback)
    if not found and src == "nominatim" and widen:
        try:
            hits = _nominatim_hits(_nominatim(nominatim_search_url(query, search_box(point, wide=True)), budget))
        except geo.GeoError as e:
            raise SearchFailed("place_down", _search_reason(e)) from None
        found = candidates(hits, home, fallback)
    if not found:
        raise SearchFailed("place_none", "none_near")
    with _lock:
        _searches[key] = (geo.clock(), src, hits[:40])
        while len(_searches) > 32:
            _searches.pop(next(iter(_searches)))
    return src, found


# --- walk minutes (one Valhalla matrix call) and TfL for the top 3 ------------------------------

def walk_minutes(home: tuple, places: list[dict], budget: Budget) -> list[int | None]:
    """Walk minutes home -> each place from one matrix call; None where unknown (or the call failed)."""
    out: list[int | None] = [None] * len(places)
    if not places or not budget.left():
        return out
    try:
        doc = geo._json(_fetch(geo.MATRIX, "matrix", budget,
                               body=geo.valhalla_matrix_body(home, [(p["lat"], p["lon"]) for p in places])))
    except geo.GeoError:
        return out
    cells = doc.get("sources_to_targets") if isinstance(doc, dict) else None
    flat = [c for row in (cells if isinstance(cells, list) else []) for c in (row if isinstance(row, list) else [row])]
    for c in flat:
        t, i = (c.get("time"), c.get("to_index")) if isinstance(c, dict) else (None, None)
        if (type(i) is int and 0 <= i < len(out) and isinstance(t, (int, float)) and not isinstance(t, bool)
                and math.isfinite(t) and 0 <= t <= 36000):
            out[i] = math.ceil(t / 60)
    if any(m is not None for m in out):
        budget.matrix = 1
    return out


def line_points(text) -> list[list[float]] | None:
    """A TfL leg's path.lineString ("[[lat, lon], ...]") -> points, or None when any point is bad."""
    import json
    try:
        pts = json.loads(text) if isinstance(text, str) and len(text) <= 200_000 else None
    except ValueError:
        return None
    if not isinstance(pts, list) or len(pts) < 2:
        return None
    out = []
    for p in pts:
        c = (_point(p[0], p[1]) if isinstance(p, list) and len(p) == 2
             and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p) else None)
        if c is None:
            return None
        out.append([c[0], c[1]])
    return out


def journey(home: tuple, place: dict, budget: Budget) -> dict | None:
    """{min, mode, points} from the first TfL journey (stage B's Journey path and caps), or None."""
    if not budget.left():
        return None
    path = f"Journey/JourneyResults/{home[0]:.5f},{home[1]:.5f}/to/{place['lat']:.5f},{place['lon']:.5f}"
    try:
        doc = geo._tfl(path, None, budget)
    except geo.GeoError:
        return None
    js = doc.get("journeys") if isinstance(doc, dict) else None
    j = js[0] if isinstance(js, list) and js and isinstance(js[0], dict) else None
    mins = j.get("duration") if j else None
    if type(mins) is not int or not 0 <= mins <= 600:
        return None
    mode, points = "walk", []
    for leg in j.get("legs") if isinstance(j.get("legs"), list) else []:
        if not isinstance(leg, dict):
            continue
        lid = str((leg.get("mode") or {}).get("id") or "") if isinstance(leg.get("mode"), dict) else ""
        if mode == "walk" and lid != "walking":
            mode = _TFL_MODES.get(lid, "")
        pts = line_points((leg.get("path") or {}).get("lineString") if isinstance(leg.get("path"), dict) else None)
        if pts:
            points += pts
    if not mode:  # a ride TfL names no fixed word for (tram, river bus): this row stays walk-only
        return None
    return {"min": mins, "mode": mode, "points": geo.simplify(points) if len(points) >= 2 else []}


def prelim_order(rows: list[dict]) -> list[dict]:
    """(group, known walk minutes first, walk minutes, km): who gets one of the 3 TfL calls."""
    return sorted(rows, key=lambda r: (r.get("group", 0), r["walk"] is None, r["walk"] or 0, r["km"]))


def settle(rows: list[dict]) -> list[dict]:
    """Row minutes = min(TfL, walk) when both are known (mode walk when walking wins), else the known one;
    final order (group, minutes, km); unknown minutes sort last."""
    for r in rows:
        tfl, walk = r.get("tfl"), r["walk"]
        if tfl and (walk is None or tfl["min"] < walk):
            r["min"], r["mode"] = tfl["min"], tfl["mode"]
        elif walk is not None:
            r["min"], r["mode"] = walk, "walk"
        else:
            r["min"], r["mode"] = None, None
    return sorted(rows, key=lambda r: (r.get("group", 0), r["min"] is None, r["min"] or 0, r["km"]))


def price(home: tuple, rows: list[dict], budget: Budget) -> list[dict]:
    """TfL for the top 3 of the preliminary order, then settle."""
    for r in prelim_order(rows)[:TFL_ROWS]:
        r["tfl"] = journey(home, r, budget)
    return settle(rows)


# --- opening hours (a small subset of OSM opening_hours) ----------------------------------------

_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_DAY = r"(?:Mo|Tu|We|Th|Fr|Sa|Su)"
_RULE_RE = re.compile(rf"({_DAY}(?:-{_DAY})?(?:,{_DAY}(?:-{_DAY})?)*) ([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-4]):([0-5]\d)")


def _days(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        i, j = _DAYS.index(a), _DAYS.index(b or a)
        out += [(i + k) % 7 for k in range((j - i) % 7 + 1)]
    return out


def hours_line(raw, t: datetime) -> str:
    """"open 24 hours" / "open until HH:MM" ("midnight" for 24:00) / "closed now" from a simple rule set
    (`DAYS HH:MM-HH:MM; ...`, one range per day); anything else -> "hours not listed". Never "open now"."""
    raw = raw.strip() if isinstance(raw, str) else ""
    if raw == "24/7":
        return "open 24 hours"
    rules: dict[int, tuple[int, int]] = {}
    for part in raw.split(";") if raw else []:
        m = _RULE_RE.fullmatch(part.strip())
        if not m:
            return NOT_LISTED
        start, end = int(m[2]) * 60 + int(m[3]), int(m[4]) * 60 + int(m[5])
        if end > 1440 or end <= start:
            return NOT_LISTED
        for d in _days(m[1]):
            if d in rules:
                return NOT_LISTED
            rules[d] = (start, end)
    today = rules.get(t.weekday())
    if today is None:
        return NOT_LISTED
    at = t.hour * 60 + t.minute
    if today[0] <= at < today[1]:
        return "open until midnight" if today[1] == 1440 else f"open until {today[1] // 60:02d}:{today[1] % 60:02d}"
    return "closed now"


# --- cinemas: the Clusterflick index (ranked here) and venue files ------------------------------

def venue_url(vid) -> str | None:
    """The only venue URL ever fetched: built from a checked id. The index's own `data` path is never used."""
    if not isinstance(vid, str) or not _VENUE_ID_RE.fullmatch(vid) or ".." in vid:
        return None
    return CF_VENUE.format(vid)


def _parse_index(doc) -> dict:
    venues = doc.get("venues") if isinstance(doc, dict) else None
    if not isinstance(venues, dict):
        raise geo.GeoError("api_error")
    cinemas = []
    for vid, v in venues.items():
        if not isinstance(v, dict) or v.get("type") != "Cinema":  # exact type; flag (b) in the report
            continue
        g = v.get("geo") if isinstance(v.get("geo"), dict) else {}
        p, name = _point(g.get("lat"), g.get("lon")), hud.place_text(v.get("name"), hud.MAX_LABEL)
        if p and name and isinstance(vid, str):
            road = str(v.get("address") or "").split(",")[0]
            cinemas.append({"id": vid[:100], "name": name, "road": hud.place_text(road, hud.MAX_LABEL),
                            "lat": p[0], "lon": p[1]})
    generated = None
    try:
        generated = datetime.fromisoformat(str(doc.get("generatedAt") or "").replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return {"cinemas": cinemas, "generated": generated}


def clusterflick_index(budget: Budget) -> tuple[dict | None, bool]:
    """(index, fetched_or_fresh). A fresh cached index (24 h) costs no request; a failed fetch hands back the
    last good index if any (False: showtimes are not trusted then)."""
    with _lock:
        hit = _index[1] if _index else None
        fresh = bool(_index) and geo.clock() - _index[0] < INDEX_TTL_S
    if fresh:
        return hit, True
    try:
        idx = _parse_index(geo._json(_fetch(CF_INDEX, "cf_index", budget)))
    except geo.GeoError:
        return hit, False
    with _lock:
        _index[:] = [geo.clock(), idx]
    return idx, True


def venue_showings(vid: str, budget: Budget) -> list[tuple[datetime, str]] | None:
    """[(start, title)] for one cinema, sold-out dropped, booking URLs and TMDB fields never kept; None when
    the id is bad, the file failed, or the question's byte budget has no room for one more file."""
    url = venue_url(vid)
    if url is None:
        return None
    with _lock:
        hit = _venues.get(vid)
    if hit and geo.clock() - hit[0] < VENUE_TTL_S:
        return hit[1]
    if budget.bytes + geo.CAPS["cf_venue"] > BYTES_PER_QUESTION:
        return None
    try:
        doc = geo._json(_fetch(url, "cf_venue", budget))
    except geo.GeoError:
        return None
    shows = []
    for film in doc if isinstance(doc, list) else []:
        if not isinstance(film, dict):
            continue
        title = hud.place_text(film.get("title"), hud.MAX_LABEL)  # bookingUrl and themoviedb are dropped here
        perfs = film.get("performances")
        for p in perfs if isinstance(perfs, list) else []:
            if not isinstance(p, dict) or (isinstance(p.get("status"), dict) and p["status"].get("soldOut") is True):
                continue
            ms = p.get("time")
            if title and isinstance(ms, (int, float)) and not isinstance(ms, bool) and math.isfinite(ms):
                try:
                    shows.append((datetime.fromtimestamp(ms / 1000, LONDON), title))
                except (OverflowError, OSError, ValueError):
                    continue
    shows = sorted(shows)[:2000]
    with _lock:
        _venues[vid] = (geo.clock(), shows)
        while len(_venues) > 64:
            _venues.pop(next(iter(_venues)))
    return shows


def window(when: str, t: datetime) -> tuple[datetime, datetime]:
    """London time: today = now .. next 04:00; tonight = max(now, 17:00 of this evening) .. next 04:00;
    tomorrow = next 04:00 .. the 04:00 after."""
    four = t.replace(hour=4, minute=0, second=0, microsecond=0)
    nxt = four if t < four else four + timedelta(days=1)
    if when == "tomorrow":
        return nxt, nxt + timedelta(days=1)
    if when == "tonight":
        return max(t, (nxt - timedelta(days=1)).replace(hour=17)), nxt
    return t, nxt


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def film_matches(words: str, title: str) -> bool:
    """The owner's words, as whole words, inside the title (both lower case, punctuation off)."""
    w = _norm(words)
    return bool(w) and f" {w} " in f" {_norm(title)} "


def _reach_minutes(row: dict) -> int:
    known = row.get("min") if row.get("min") is not None else row["walk"]
    # shortcut: no walk time known -> straight km at 4.5 km/h; good enough to sort showings, upgrade with real data
    return known if known is not None else math.ceil(row["km"] / 4.5 * 60)


def _showings(row: dict, ask: dict, t: datetime) -> list[tuple[datetime, str]]:
    start, end = window(ask["when"], t)
    pad = None if ask["when"] == "tomorrow" else t + timedelta(minutes=_reach_minutes(row) + REACH_PAD_MIN)
    out = [(s, title) for s, title in row["shows"] or []
           if start <= s < end and (pad is None or s >= pad)]
    if ask["mode"] == "film":
        out = [x for x in out if film_matches(ask["film"], x[1])]
    return out


# --- the answer -----------------------------------------------------------------------------------

@dataclass
class Answer:
    key: str = "place_down"
    fmt: dict = field(default_factory=dict)
    reason: str = "ok"
    src: str = "none"
    title: str = ""
    rows: list[dict] = field(default_factory=list)  # card rows (hud._place_row shape)
    route: dict | None = None
    top: dict | None = None
    credits: list[str] = field(default_factory=list)
    asof: str = ""
    web: bool = False  # the card or line holds web text: the service marks the conversation tainted
    home: tuple | None = None


def spoken_time(t: datetime, when: str = "today") -> str:
    """"6pm", or "6pm tomorrow" for a tomorrow ask (the owner must hear the day)."""
    h = t.hour % 12 or 12
    text = f"{h}{'' if t.minute == 0 else f':{t.minute:02d}'}{'am' if t.hour < 12 else 'pm'}"
    return text + " tomorrow" if when == "tomorrow" else text


def _query_words(ask: dict) -> str:
    """"Dishoom" for a name; "a pharmacy" / "an atm" for a category."""
    q = ask["query"]
    if ask["mode"] != "category":
        return capwords(q)
    return ("an " if q[:1] in "aeiou" else "a ") + q


def _minutes_text(n: int) -> str:
    return f"{n} minute{'s' if n != 1 else ''}"


def _spoken_name(rows: list[dict]) -> str:
    top = rows[0]
    name = " ".join(top["name"].split()[:6])
    if top["road"] and sum(r["name"].lower() == top["name"].lower() for r in rows) > 1:
        name += " on " + " ".join(top["road"].split()[:6])
    return name


def _card_rows(rows: list[dict], t: datetime, cinema: bool, shows_ok: bool, ask: dict) -> list[dict]:
    out = []
    for r in rows:
        row = {"name": r["name"], "km": round(r["km"], 1)}
        if r["road"]:
            row["road"] = r["road"]
        if r["min"] is not None:
            row["min"], row["mode"] = r["min"], r["mode"]
        if not cinema:
            row["hours"] = hours_line(r.get("hours_raw"), t)
        elif shows_ok and r.get("shows") is not None:
            picked = r["picked"][:3]
            if picked:
                row["times"] = [{"at": s.strftime("%H:%M"), "film": title} for s, title in picked]
            else:
                row["note"] = "none_tomorrow" if ask.get("when") == "tomorrow" else "none"
        else:
            row["note"] = "times_not_listed"
        out.append(row)
    return out


def _route(top: dict) -> dict | None:
    pts = (top.get("tfl") or {}).get("points") or []
    if len(pts) < 2:
        return None
    km = sum(km_between(a, b) for a, b in zip(pts, pts[1:]))
    return {"points": pts, "km": round(km, 1), "min": float(top["tfl"]["min"])}


def _line(ans: Answer, rows: list[dict], cinema: bool, shows_ok: bool, ask: dict) -> None:
    top = rows[0]
    name = _spoken_name(rows)
    if top["min"] is None:  # neither the matrix nor TfL answered: straight km
        ans.key = "shows_down_km" if cinema and not shows_ok else "place_best_km"
        ans.fmt = {"name": name, "km": f"{top['km']:.1f}"}
        return
    base = {"name": name, "mins": _minutes_text(top["min"]), "how": HOW[top["mode"]]}
    if not cinema:
        ans.key, ans.fmt = "place_best", base
    elif not shows_ok:
        ans.key, ans.fmt = "shows_down", base
    elif ask["mode"] == "film":
        ans.key, ans.fmt = "film_best", {**base, "film": capwords(ask["film"]), "time": spoken_time(top["picked"][0][0], ask["when"])}
    elif top["picked"]:
        ans.key, ans.fmt = "cinema_best", {**base, "time": spoken_time(top["picked"][0][0], ask["when"])}
    else:
        ans.key, ans.fmt = "cinema_none_left", {**base, "when": ask["when"]}


def answer(ask: dict, home: tuple, budget: Budget) -> Answer:
    """The whole place answer for one ask (runs in a thread; the caller checked home and London)."""
    ans = Answer(home=home)
    t = now()
    cinema = ask["mode"] in ("cinema", "film", "whats_on")
    shows_ok = False
    try:
        if not cinema:
            ans.src, found = search(ask["mode"], ask["query"], home, budget)
            ans.title = f"Nearest {capwords(ask['query'])}"[:hud.MAX_TITLE]
        else:
            idx, fresh = clusterflick_index(budget)
            if idx is None:  # cold cache and Clusterflick down: one bounded Nominatim "cinema" search
                ans.src, found = search("category", "cinema", home, budget, widen=False)
            else:
                ans.src = "clusterflick"
                gen = idx["generated"]
                shows_ok = fresh and gen is not None and (t - gen).total_seconds() <= STALE_S
                found = sorted(({**c, "km": km_between(home, (c["lat"], c["lon"]))} for c in idx["cinemas"]),
                               key=lambda c: c["km"])
                found = [c for c in found if c["km"] <= MAX_KM][:MAX_ROWS]
                if not found:
                    raise SearchFailed("place_none", "none_near")
                ans.asof = gen.astimezone(LONDON).strftime("%H:%M") if gen else ""
            ans.title = (f"{capwords(ask['film'])} near you" if ask["mode"] == "film" else "Cinemas near you")[:hud.MAX_TITLE]
    except SearchFailed as e:
        ans.key, ans.reason = e.key, e.reason
        ans.fmt = {"query": _query_words(ask) if not cinema else "a cinema"} if e.key == "place_none" else {}
        return ans
    rows = [{**c, "walk": None, "tfl": None, "min": None, "mode": None, "shows": None, "picked": []} for c in found]
    if cinema and shows_ok:
        for r in rows:
            r["shows"] = venue_showings(r.get("id", ""), budget)
        if all(r["shows"] is None for r in rows):
            shows_ok = False
    budget.shows = int(shows_ok)
    walks = walk_minutes(home, rows, budget)
    for r, w in zip(rows, walks):
        r["walk"] = w
    if cinema and shows_ok:
        for r in rows:
            r["picked"] = _showings(r, ask, t)
            r["group"] = 0 if r["picked"] else 1
        if ask["mode"] == "film":
            rows = [r for r in rows if r["picked"]]
            if not rows:
                ans.key, ans.reason, ans.fmt = "film_none", "film_no_match", {"film": capwords(ask["film"]), "when": ask["when"]}
                return ans
    rows = price(home, rows, budget)
    if cinema and shows_ok:  # re-checked with the final minutes
        for r in rows:
            r["picked"] = _showings(r, ask, t)
            r["group"] = 0 if r["picked"] else 1
        if ask["mode"] == "film":
            rows = [r for r in rows if r["picked"]]
            if not rows:
                ans.key, ans.reason, ans.fmt = "film_none", "film_no_match", {"film": capwords(ask["film"]), "when": ask["when"]}
                return ans
        rows = sorted(rows, key=lambda r: (r["group"], r["min"] is None, r["min"] or 0, r["km"]))
    ans.rows = _card_rows(rows, t, cinema, shows_ok, ask)
    ans.top = {"lat": rows[0]["lat"], "lon": rows[0]["lon"], "name": rows[0]["name"]}
    ans.route = _route(rows[0])
    ans.credits = ["osm"] + (["tfl"] if any(r.get("tfl") for r in rows) else []) + (
        ["clusterflick"] if ans.src == "clusterflick" else [])
    if ans.src != "clusterflick":
        ans.asof = ""
    _line(ans, rows, cinema, shows_ok, ask)
    ans.web = True
    if cinema and not shows_ok:
        ans.reason = "shows_down"
    if budget.timed_out and ans.reason == "ok":
        ans.reason = "timeout"
    return ans


def card(ans: Answer) -> dict | None:
    """The one place card: stage B's map page with the ranked rows, the top pick's route and two pins.
    Service code only (allow_web); every field is re-checked by hud._map_data."""
    if not ans.rows or not ans.top or not ans.home:
        return None
    pins = [{"lat": ans.home[0], "lon": ans.home[1], "label": "Home"},
            {"lat": ans.top["lat"], "lon": ans.top["lon"], "label": ans.top["name"]}]
    data = {"routes": [ans.route] if ans.route else [], "pins": pins, "rows": ans.rows, "credits": ans.credits,
            **({"asof": ans.asof} if ans.asof else {})}
    return hud.validate_card({"id": "pl-" + uuid.uuid4().hex[:8], "type": "web", "title": ans.title,
                              "page": "map", "data": data}, allow_web=True)

