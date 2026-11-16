"""Maps, roads, TfL and geocoding for the voice overlay (jfollow10b). Fixed code only, no model.

Every outbound request goes through `_request` (via `_get` / `_post`): https, an exact host from
HOSTS, no redirects, 5 s connect / 8 s total, a byte cap per kind. Place text goes only to a
geocoder (once, then cached); routing and TfL get numbers only. Nothing here logs a place,
coordinate, stop name or URL: errors carry a reason word.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from mac.voice import media_lookup

logger = logging.getLogger(__name__)

HOSTS = frozenset({"valhalla1.openstreetmap.de", "routing.openstreetmap.de", "nominatim.openstreetmap.org",
                   "api.postcodes.io", "api.tfl.gov.uk", "tile.openstreetmap.org"})
USER_AGENT = media_lookup.USER_AGENT  # client name/version + the public repo; no personal data
CONNECT_S, TOTAL_S = 5.0, 8.0
CAPS = {"tile": 256 * 1024, "route": 512 * 1024, "tfl": 512 * 1024, "nominatim": 64 * 1024,
        "postcodes": 16 * 1024}
VALHALLA = "https://valhalla1.openstreetmap.de/route"
OSRM_CAR = "https://routing.openstreetmap.de/routed-car/route/v1/driving/"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
POSTCODES = "https://api.postcodes.io/postcodes/"
TFL = "https://api.tfl.gov.uk/"
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_LINE = "Routes © OpenStreetMap contributors"
TFL_LINE = "Powered by TfL Open Data"
TRAFFIC_LINE = "Car times ignore live traffic and the Congestion Charge."
MAX_POINTS = 300
TFL_PER_QUESTION, TFL_PER_MINUTE, NOMINATIM_PER_QUESTION = 6, 20, 2
ROUTE_TTL_S, STOPS_TTL_S, ARRIVALS_TTL_S, STATUS_TTL_S = 600, 12 * 3600, 30, 60
TFL_APP_KEY = ""  # a free TfL key goes here later (owner-only signup); empty = anonymous
POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$", re.I)
GOOGLE_MAPS_RE = re.compile(r"(?:https?://)?(?:maps\.app\.goo\.gl|goo\.gl/maps|(?:www\.)?google\.[a-z.]+/maps"
                            r"|maps\.google\.[a-z.]+)", re.I)

_transport = None  # tests inject an httpx.MockTransport
clock = time.monotonic  # tests replace clock + sleep (the Nominatim spacing, TTLs, rate windows)
sleep = time.sleep
_lock = threading.Lock()
_nominatim_lock = threading.Lock()
_nominatim_last: list[float] = []
_tfl_calls: deque = deque()
_cache: dict[tuple, tuple[float, object]] = {}  # (kind, key) -> (stored at, value)
_geocodes: dict[str, tuple[float, float]] = {}  # typed text -> lat/lon, process life


class GeoError(Exception):
    """reason: api_error | timeout | too_big | rate_cap | bad_host | no_route | no_place"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Budget:
    """Per-question counters: total calls (for the row), TfL calls (<= 6), Nominatim calls (<= 2)."""

    def __init__(self) -> None:
        self.calls = self.tfl = self.nominatim = 0
        self.src = "none"


def tfl_params() -> dict:
    """The only place a TfL key is ever added; never an empty or fake one (TfL answers 429)."""
    return {"app_key": TFL_APP_KEY} if TFL_APP_KEY else {}


def reset() -> None:
    """Tests: drop every cache and rate window."""
    with _lock:
        _cache.clear()
        _geocodes.clear()
        _tfl_calls.clear()
        _nominatim_last.clear()


def _check(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in HOSTS or parts.port or parts.username or parts.password:
        raise GeoError("bad_host")


def _request(method: str, url: str, kind: str, params: dict | None = None,
             json_body: dict | None = None) -> bytes:
    """One allow-listed request -> body bytes, or GeoError(reason). Checked before anything is sent."""
    _check(url)
    cap = CAPS[kind]
    deadline = clock() + TOTAL_S
    try:
        with httpx.Client(timeout=httpx.Timeout(TOTAL_S, connect=CONNECT_S), follow_redirects=False,
                          transport=_transport,
                          headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}) as client:
            with client.stream(method, url, params=params, json=json_body) as resp:
                if resp.status_code != 200:  # a redirect is an error too: never followed
                    logger.info("geo %s: HTTP %s", kind, resp.status_code)
                    raise GeoError("api_error")
                body = bytearray()
                for chunk in resp.iter_raw():
                    body += chunk
                    if len(body) > cap:
                        raise GeoError("too_big")
                    if clock() > deadline:
                        raise GeoError("timeout")
    except httpx.TimeoutException:
        raise GeoError("timeout") from None
    except httpx.HTTPError as e:
        logger.info("geo %s: %s", kind, type(e).__name__)
        raise GeoError("api_error") from None
    return bytes(body)


def _get(url: str, kind: str = "route", params: dict | None = None) -> bytes:
    return _request("GET", url, kind, params=params)


def _post(url: str, body: dict, kind: str = "route") -> bytes:
    return _request("POST", url, kind, json_body=body)


def _json(data: bytes):
    import json
    try:
        return json.loads(data)
    except ValueError:
        raise GeoError("api_error") from None


def _cached(kind: str, key, ttl: float):
    with _lock:
        hit = _cache.get((kind, key))
        if hit and clock() - hit[0] < ttl:
            return hit[1]
    return None


def _store(kind: str, key, value) -> None:
    with _lock:
        _cache[(kind, key)] = (clock(), value)
        while len(_cache) > 256:
            _cache.pop(next(iter(_cache)))


# --- polylines -------------------------------------------------------------------------------

def decode_polyline6(text: str) -> list[list[float]]:
    """Google polyline with 6 decimals (Valhalla, OSRM geometries=polyline6) -> [[lat, lon], ...]."""
    points, index, lat, lon = [], 0, 0, 0
    while index < len(text):
        for axis in (0, 1):
            shift = result = 0
            while True:
                if index >= len(text):
                    return points
                b = ord(text[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        points.append([lat / 1e6, lon / 1e6])
    return points


def simplify(points: list[list[float]], limit: int = MAX_POINTS) -> list[list[float]]:
    """<= limit points, first and last kept, 5 decimals.
    shortcut: even stride, not Douglas-Peucker; fine at map zoom for a 5 km route, upgrade if long routes look jagged."""
    if len(points) > limit:
        step = (len(points) - 1) / (limit - 1)
        points = [points[round(i * step)] for i in range(limit - 1)] + [points[-1]]
    return [[round(p[0], 5), round(p[1], 5)] for p in points]


# --- routes ----------------------------------------------------------------------------------

_REF_RE = re.compile(r"^[A-Z]{1,2}\d{1,4}$")


def _roads(names_lengths: list[tuple[list[str], float]]) -> list[str]:
    """Road names of steps >= 0.3 km, a road number preferred, de-duplicated, <= 4."""
    out: list[str] = []
    for names, km in names_lengths:
        if km < 0.3 or not names:
            continue
        name = next((n for n in names if _REF_RE.fullmatch(n)), names[0])[:40]
        if name and name not in out:
            out.append(name)
        if len(out) == 4:
            break
    return out


def _valhalla_body(a: tuple, b: tuple, costing: str, alternates: int) -> dict:
    return {"locations": [{"lat": float(a[0]), "lon": float(a[1])}, {"lat": float(b[0]), "lon": float(b[1])}],
            "costing": costing, "alternates": alternates, "units": "kilometers", "language": "en-GB",
            "directions_options": {"units": "kilometers", "language": "en-GB"}}


def _valhalla_trip(trip: dict) -> dict:
    legs = trip.get("legs") or []
    points, steps = [], []
    for leg in legs:
        points += decode_polyline6(str(leg.get("shape") or ""))
        steps += [(list(m.get("street_names") or []), float(m.get("length") or 0)) for m in leg.get("maneuvers") or []]
    summary = trip.get("summary") or {}
    return {"points": simplify(points), "km": round(float(summary.get("length", 0)), 1),
            "min": round(float(summary.get("time", 0)) / 60, 1), "roads": _roads(steps)}


def _valhalla(a: tuple, b: tuple, costing: str, alternates: int, budget: Budget) -> list[dict]:
    budget.calls += 1
    doc = _json(_post(VALHALLA, _valhalla_body(a, b, costing, alternates)))
    if not isinstance(doc, dict) or not isinstance(doc.get("trip"), dict):
        raise GeoError("no_route")
    trips = [doc["trip"]] + [x["trip"] for x in doc.get("alternates") or [] if isinstance(x, dict) and "trip" in x]
    return [r for r in map(_valhalla_trip, trips) if len(r["points"]) >= 2]


def _osrm_car(a: tuple, b: tuple, budget: Budget) -> list[dict]:
    budget.calls += 1
    url = f"{OSRM_CAR}{float(a[1]):.6f},{float(a[0]):.6f};{float(b[1]):.6f},{float(b[0]):.6f}"
    doc = _json(_get(url, "route", {"overview": "full", "geometries": "polyline6", "alternatives": "2", "steps": "true"}))
    if not isinstance(doc, dict) or doc.get("code") != "Ok":
        raise GeoError("no_route")
    out = []
    for r in doc.get("routes") or []:
        steps = [([s.get("ref") or "", s.get("name") or ""], float(s.get("distance") or 0) / 1000)
                 for leg in r.get("legs") or [] for s in leg.get("steps") or []]
        steps = [([n for n in names if n], km) for names, km in steps]
        pts = simplify(decode_polyline6(str(r.get("geometry") or "")))
        if len(pts) >= 2:
            out.append({"points": pts, "km": round(float(r.get("distance", 0)) / 1000, 1),
                        "min": round(float(r.get("duration", 0)) / 60, 1), "roads": _roads(steps)})
    return out


def car_routes(a: tuple, b: tuple, budget: Budget) -> list[dict]:
    """Up to 3 car routes, best first: Valhalla, then OSRM. Cached 10 min by the two points."""
    key = (round(a[0], 5), round(a[1], 5), round(b[0], 5), round(b[1], 5))
    hit = _cached("car", key, ROUTE_TTL_S)
    if hit is not None:
        budget.src = "cache"
        return hit
    try:
        routes, budget.src = _valhalla(a, b, "auto", 2, budget), "valhalla"
    except GeoError:
        routes, budget.src = _osrm_car(a, b, budget), "osrm"
    if not routes:
        raise GeoError("no_route")
    routes = routes[:3]
    _store("car", key, routes)
    return routes


def walk_cycle(a: tuple, b: tuple, budget: Budget) -> dict:
    """{"walk": min, "cycle": min} where Valhalla answered; cached 10 min; a failure leaves it out."""
    key = (round(a[0], 5), round(a[1], 5), round(b[0], 5), round(b[1], 5))
    hit = _cached("other", key, ROUTE_TTL_S)
    if hit is not None:
        return hit
    out = {}
    for costing, name in (("pedestrian", "walk"), ("bicycle", "cycle")):
        try:
            trips = _valhalla(a, b, costing, 0, budget)
        except GeoError:
            continue
        if trips:
            out[name] = trips[0]["min"]
    _store("other", key, out)
    return out


# --- geocoding ---------------------------------------------------------------------------------

def is_postcode(text: str) -> bool:
    return bool(POSTCODE_RE.fullmatch(text.strip()))


def geocode(text: str, budget: Budget) -> tuple[float, float]:
    """Typed place text -> (lat, lon): a UK postcode via postcodes.io, else Nominatim (top hit).
    The text goes out once per process; repeats come from memory."""
    norm = " ".join(text.lower().split())
    with _lock:
        hit = _geocodes.get(norm)
    if hit:
        budget.src = "cache"
        return hit
    if is_postcode(norm):
        budget.calls += 1
        budget.src = "postcodes"
        doc = _json(_get(POSTCODES + quote(norm.replace(" ", "").upper()), "postcodes"))
        res = doc.get("result") if isinstance(doc, dict) else None
        if not isinstance(res, dict):
            raise GeoError("no_place")
        found = (float(res["latitude"]), float(res["longitude"]))
    else:
        if budget.nominatim >= NOMINATIM_PER_QUESTION:
            raise GeoError("rate_cap")
        with _nominatim_lock:  # Nominatim policy: at most 1 request per second
            if _nominatim_last:
                wait = 1.0 - (clock() - _nominatim_last[-1])
                if wait > 0:
                    sleep(wait)
            budget.calls += 1
            budget.nominatim += 1
            budget.src = "nominatim"
            _nominatim_last[:] = [clock()]
            doc = _json(_get(NOMINATIM, "nominatim",
                             {"q": norm, "format": "jsonv2", "countrycodes": "gb", "limit": "1"}))
        if not isinstance(doc, list) or not doc or not isinstance(doc[0], dict):
            raise GeoError("no_place")
        try:
            found = (float(doc[0]["lat"]), float(doc[0]["lon"]))
        except (KeyError, TypeError, ValueError):
            raise GeoError("no_place") from None
    if not (-90 <= found[0] <= 90 and -180 <= found[1] <= 180):
        raise GeoError("no_place")
    with _lock:
        _geocodes[norm] = found
    return found


# --- TfL ---------------------------------------------------------------------------------------

def _tfl(path: str, params: dict | None, budget: Budget):
    """One TfL GET under both caps: <= 6 per question, <= 20 per sliding minute."""
    now = clock()
    with _lock:
        while _tfl_calls and now - _tfl_calls[0] >= 60:
            _tfl_calls.popleft()
        if budget.tfl >= TFL_PER_QUESTION or len(_tfl_calls) >= TFL_PER_MINUTE:
            raise GeoError("rate_cap")
        _tfl_calls.append(now)
    budget.tfl += 1
    budget.calls += 1
    budget.src = "tfl"
    return _json(_get(TFL + path, "tfl", {**(params or {}), **tfl_params()}))


def _stops(lat: float, lon: float, mode: str, budget: Budget) -> list[dict]:
    """The nearest stops (bus) or stations (tube) as [{id, name, letter}], nearest first; cached 12 h."""
    key = (mode, round(lat, 4), round(lon, 4))
    hit = _cached("stops", key, STOPS_TTL_S)
    if hit is not None:
        return hit
    stop_type, radius = ("NaptanPublicBusCoachTram", 300) if mode == "bus" else ("NaptanMetroStation", 1000)
    doc = _tfl("StopPoint", {"lat": round(lat, 5), "lon": round(lon, 5), "stopTypes": stop_type,
                             "radius": radius}, budget)
    raw = doc.get("stopPoints") if isinstance(doc, dict) else None
    out = []
    for s in sorted((s for s in raw or [] if isinstance(s, dict)), key=lambda s: float(s.get("distance") or 9e9)):
        sid = str(s.get("naptanId") or s.get("id") or "")
        if re.fullmatch(r"[A-Za-z0-9]{4,20}", sid):
            out.append({"id": sid, "name": str(s.get("commonName") or "")[:60],
                        "letter": str(s.get("stopLetter") or "")[:4]})
    _store("stops", key, out)
    return out


def station_by_name(name: str, budget: Budget) -> dict | None:
    """A tube station from TfL's own search (never Nominatim); hubs off, so the id has arrivals."""
    key = " ".join(name.lower().split())
    hit = _cached("station", key, STOPS_TTL_S)
    if hit is not None:
        return hit or None
    doc = _tfl("StopPoint/Search/" + quote(key[:60]), {"modes": "tube", "includeHubs": "false"}, budget)
    matches = doc.get("matches") if isinstance(doc, dict) else None
    found = {}
    for m in matches or []:
        sid = str(m.get("id") or "") if isinstance(m, dict) else ""
        if re.fullmatch(r"[A-Za-z0-9]{4,20}", sid):
            found = {"id": sid, "name": str(m.get("name") or "")[:60], "letter": ""}
            break
    _store("station", key, found)
    return found or None


def _arrivals(stop_id: str, budget: Budget) -> list[dict]:
    hit = _cached("arrivals", stop_id, ARRIVALS_TTL_S)
    if hit is not None:
        return hit
    doc = _tfl(f"StopPoint/{stop_id}/Arrivals", None, budget)
    out = []
    for a in doc if isinstance(doc, list) else []:
        if not isinstance(a, dict) or not isinstance(a.get("timeToStation"), (int, float)):
            continue
        out.append({"line": str(a.get("lineName") or "")[:20], "to": str(a.get("destinationName") or "")[:60],
                    "platform": str(a.get("platformName") or "")[:40], "secs": int(a["timeToStation"])})
    out.sort(key=lambda a: a["secs"])
    _store("arrivals", stop_id, out)
    return out


def _short_place(name: str) -> str:
    return re.sub(r"\s+Underground Station$", "", name)


def next_departures(lat: float | None, lon: float | None, mode: str, budget: Budget,
                    station: str = "") -> tuple[list[str], list[dict]]:
    """(card lines, the next arrivals) for the nearest stops (bus: <= 2) or one station (tube).
    Raises GeoError("no_stops") when TfL has no stop near the place."""
    if station:
        found = station_by_name(station, budget)
        stops = [found] if found else []
    else:
        stops = _stops(lat, lon, mode, budget)
    stops = stops[:2] if mode == "bus" else stops[:1]
    if not stops:
        raise GeoError("no_stops")
    arrivals = []
    for stop in stops:
        for a in _arrivals(stop["id"], budget):
            arrivals.append({**a, "stop": stop})
    arrivals.sort(key=lambda a: a["secs"])
    if not arrivals:
        raise GeoError("api_error")
    lines = []
    if mode == "bus":
        for a in arrivals[:5]:
            where = f"stop {a['stop']['letter']}" if a["stop"]["letter"] else a["stop"]["name"][:30]
            lines.append(f"{a['line']} to {a['to']}, {a['secs'] // 60} min ({where})")
    else:
        lines.append(_short_place(stops[0]["name"]))
        seen: dict[tuple, int] = {}
        for a in arrivals:  # per line and direction: the next two
            k = (a["line"], a["platform"])
            if seen.get(k, 0) >= 2 or len(lines) >= 5:
                continue
            seen[k] = seen.get(k, 0) + 1
            platform = a["platform"].split(" - ")[0]
            lines.append(f"{a['line']} {platform} to {_short_place(a['to'])}, {a['secs'] // 60} min")
    return lines + [TFL_LINE], arrivals


def departure_line(arrivals: list[dict], mode: str) -> str:
    """The next one or two, spoken."""
    def one(a):
        m = a["secs"] // 60
        when = "due now" if m == 0 else f"in {m} minute{'s' if m != 1 else ''}"
        what = f"the {a['line']}" if mode == "bus" else f"a {a['line']} line train"
        return f"{what} {when}"
    first = arrivals[0]
    second = next((a for a in arrivals[1:] if a["line"] != first["line"] or a["secs"] - first["secs"] >= 60), None)
    text = "Next is " + one(first)
    return text + (", then " + one(second) + "." if second else ".")


def line_status(line_id: str, budget: Budget) -> list[dict]:
    """[{name, status, reason}] for one line or the whole tube ("tube"); cached 60 s."""
    hit = _cached("status", line_id, STATUS_TTL_S)
    if hit is not None:
        return hit
    path = "Line/Mode/tube/Status" if line_id == "tube" else f"Line/{line_id}/Status"
    doc = _tfl(path, None, budget)
    out = []
    for line in doc if isinstance(doc, list) else []:
        if not isinstance(line, dict):
            continue
        statuses = [s for s in line.get("lineStatuses") or [] if isinstance(s, dict)]
        worst = min(statuses, key=lambda s: s.get("statusSeverity", 10), default={})
        out.append({"name": str(line.get("name") or "")[:30],
                    "status": str(worst.get("statusSeverityDescription") or "Unknown")[:40],
                    "good": worst.get("statusSeverity") == 10})
    if not out:
        raise GeoError("api_error")
    _store("status", line_id, out)
    return out


def status_lines(statuses: list[dict]) -> tuple[list[str], str]:
    """(card lines, spoken line) for line status."""
    if len(statuses) == 1:
        s = statuses[0]
        return [f"{s['name']}: {s['status']}", TFL_LINE], f"The {s['name']} line has {s['status'].lower()}."
    bad = [s for s in statuses if not s["good"]]
    lines = [f"{s['name']}: {s['status']}" for s in bad[:5]] or ["Good service on every line"]
    if not bad:
        spoken = "Good service on every tube line."
    elif len(bad) == 1:
        spoken = f"Only the {bad[0]['name']} line has trouble: {bad[0]['status'].lower()}."
    else:
        spoken = f"{len(bad)} lines have trouble. They're on the card."
    return lines + [TFL_LINE], spoken


def journeys(a: tuple, b: tuple, bus_only: bool, budget: Budget) -> list[dict]:
    """<= 3 TfL journeys [{leave, min, legs: [(mode, line, min, to)]}]; numbers only go out."""
    path = f"Journey/JourneyResults/{a[0]:.5f},{a[1]:.5f}/to/{b[0]:.5f},{b[1]:.5f}"
    doc = _tfl(path, {"mode": "bus,walking"} if bus_only else None, budget)
    out = []
    for j in (doc.get("journeys") if isinstance(doc, dict) else None) or []:
        if not isinstance(j, dict):
            continue
        legs = []
        for leg in j.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            mode = str((leg.get("mode") or {}).get("id") or "")[:20]
            opts = leg.get("routeOptions") or [{}]
            legs.append((mode, str((opts[0] if isinstance(opts[0], dict) else {}).get("name") or "")[:12],
                         int(leg.get("duration") or 0),
                         str((leg.get("arrivalPoint") or {}).get("commonName") or "")[:40]))
        start = str(j.get("startDateTime") or "")
        leave = start[11:16] if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d.*", start) else ""
        if legs:
            out.append({"leave": leave, "min": int(j.get("duration") or 0), "legs": legs})
        if len(out) == 3:
            break
    if not out:
        raise GeoError("no_route")  # e.g. a disambiguation-only answer
    return out


def journey_lines(found: list[dict]) -> tuple[list[str], str]:
    def legs_text(j):
        parts = []
        for mode, line, mins, to in j["legs"]:
            parts.append(f"walk {mins}" if mode == "walking" or not line else f"{line} to {to}" if to else line)
        return ", ".join(parts)
    lines = [f"Leave {j['leave']}, {j['min']} min: {legs_text(j)}"[:200] for j in found]
    best = found[0]
    rides = [line for mode, line, _m, _t in best["legs"] if mode != "walking" and line]
    how = f"take the {' then the '.join(rides)}" if rides else "walk"
    when = f" leaving at {best['leave']}" if best["leave"] else ""
    return lines + [TFL_LINE], f"Best is to {how}{when}, about {best['min']} minutes."


# --- answers -----------------------------------------------------------------------------------

def map_data(routes: list[dict], pins: list[dict]) -> dict:
    return {"routes": [{"points": r["points"], "km": r["km"], "min": r["min"]} for r in routes[:3]],
            "pins": pins[:2]}


def map_line(routes: list[dict]) -> str:
    if not routes:
        return "Here it is on the map."
    return f"About {round(routes[0]['min'])} minutes by car. The route's on the map."


def roads_lines(routes: list[dict], other: dict) -> list[str]:
    lines = [f"{round(r['min'])} min, {r['km']:.1f} km: {', '.join(r['roads']) or 'local roads'}" for r in routes[:3]]
    extra = ", ".join(f"{'Walk' if k == 'walk' else 'cycle'} {round(v)} min" for k, v in other.items())
    if extra:
        lines.append(extra[0].upper() + extra[1:])
    return lines + [TRAFFIC_LINE, OSM_LINE]


def roads_line(routes: list[dict]) -> str:
    best = routes[0]
    via = " and ".join(best["roads"][:2]) or "local roads"
    tail = f" {len(routes) - 1} other route{'s are' if len(routes) > 2 else ' is'} on the card." if len(routes) > 1 else ""
    return f"Fastest is via {via}, about {round(best['min'])} minutes.{tail}"


# --- map tiles (the gateway's /overlay/tiles route) ----------------------------------------------

TILE_FRESH_S = 7 * 86400
TILE_CAP_BYTES = 100 * 1024 * 1024
TILE_PER_MINUTE = 120
TILE_MAX_PX = 512
TILE_STATS = {"served": 0, "cache_hits": 0, "fetched": 0, "refused": 0}
_tile_fetches: deque = deque()
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def tile_ok(z: int, x: int, y: int) -> bool:
    return (all(type(v) is int for v in (z, x, y)) and 0 <= z <= 19
            and 0 <= x < 2 ** z and 0 <= y < 2 ** z)


def tile_path(root: Path, z: int, x: int, y: int) -> Path:
    """<root>/tiles/<z>/<x>/<y>.png from ints only; refuses a symlinked tiles dir or an escape."""
    if not tile_ok(z, x, y):
        raise GeoError("bad_tile")
    tiles = Path(root) / "tiles"
    if tiles.is_symlink():
        raise GeoError("bad_tile")
    path = tiles / str(z) / str(x) / f"{y}.png"
    real_root = os.path.realpath(tiles)
    if os.path.commonpath([real_root, os.path.realpath(path)]) != real_root:
        raise GeoError("bad_tile")
    return path


def _png_ok(data: bytes) -> bool:
    if len(data) < 24 or not data.startswith(_PNG_SIG) or data[12:16] != b"IHDR":
        return False
    w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0 < w <= TILE_MAX_PX and 0 < h <= TILE_MAX_PX


def _prune(tiles: Path) -> None:
    files = [(p.stat().st_mtime, p.stat().st_size, p) for p in tiles.rglob("*.png") if p.is_file() and not p.is_symlink()]
    total = sum(f[1] for f in files)
    if total <= TILE_CAP_BYTES:
        return
    for _mtime, size, p in sorted(files):
        if total <= TILE_CAP_BYTES * 0.8:
            break
        p.unlink(missing_ok=True)
        total -= size


def tile(root: Path, z: int, x: int, y: int) -> bytes:
    """One OSM tile: from the disk cache when fresh (7 days), else one upstream GET (<= 120/min),
    PNG signature + IHDR <= 512 px checked before it is cached. GeoError(reason) otherwise."""
    path = tile_path(root, z, x, y)
    tiles = Path(root) / "tiles"
    now = time.time()
    if path.is_file() and not path.is_symlink() and now - path.stat().st_mtime < TILE_FRESH_S:
        TILE_STATS["cache_hits"] += 1
        TILE_STATS["served"] += 1
        return path.read_bytes()
    with _lock:
        mono = clock()
        while _tile_fetches and mono - _tile_fetches[0] >= 60:
            _tile_fetches.popleft()
        if len(_tile_fetches) >= TILE_PER_MINUTE:
            TILE_STATS["refused"] += 1
            raise GeoError("rate_cap")
        _tile_fetches.append(mono)
    data = _get(TILE_URL.format(z=z, x=x, y=y), "tile")
    if not _png_ok(data):
        TILE_STATS["refused"] += 1
        raise GeoError("bad_tile")
    TILE_STATS["fetched"] += 1
    for d in (tiles, tiles / str(z), tiles / str(z) / str(x)):
        d.mkdir(mode=0o700, exist_ok=True)
        if d.is_symlink():
            raise GeoError("bad_tile")
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    _prune(tiles)
    TILE_STATS["served"] += 1
    return data
