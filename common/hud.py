"""Shared HUD schema: on-screen shapes + explanation cards (phone Hardware Mode and Mac overlay).

Annotations are ONLY simple outline shapes (rect, circle, triangle). Explanations are cards.
Everything coming from a model is untrusted: the validators drop unknown kinds/types, clamp
text, and reject any URL that is not https. Swift models in mac/overlay decode the same JSON
(fixtures in tests/fixtures/hud/).

    shape = {kind: "rect"|"circle"|"triangle", box: [x0, y0, x1, y1] (px of the frame/screen),
             label?: str<=40, id?: str}
    card  = {id, type: "info"|"stat"|"bar"|"flow"|"link"|"image"|"video", title: str<=60, ...}
      info:  body: [str] <= 6 lines
      stat:  value: str, label: str
      bar:   items: [{label, value: number}] <= 12, caption?: str
      flow:  nodes: [{id, text}] <= 12, edges: [[from, to, label?]]
      link:  url: https only, source?: str
      image: url (https) or file_id; alt?: str<=120, caption?: str<=120 (attribution),
             source_page?: https, gallery items?: [{url, alt?, caption?, source_page?}] <= 6
             (kept only with 2+; url = first item)
      video: youtube_id, or url that is a YouTube watch/shorts/embed/youtu.be link or ends
             .mp4/.mov/.m4v (no HLS), thumbnail_url? (file urls only), source_page?;
             no usable media -> link card on source_page/url, else dropped
      web:   page: one of WEB_PAGES ("map", "drawing", "model"), data: that page's validated JSON. Service-only:
             dropped unless the caller passes allow_web=True (model, agent, workspace and fast-tier cards
             never do); drawing data = {svg: svgsafe output, legend: [str<=80] <= 12}; model data = {slug, view: model|preview}
      chart: chart: one of CHART_KINDS, labels: [str<=24] <= 12, series: [{name, values}] 1-4,
             unit?: str<=12, caption?: str<=120, min?/max?: gauge only (rules in _chart)
    hud   = {shapes: [shape], cards: [card]}
"""

from __future__ import annotations

import math
import re
import uuid
from urllib.parse import parse_qs, urlsplit

from common import svgsafe

SHAPE_KINDS = ("rect", "circle", "triangle")
CARD_TYPES = ("info", "stat", "bar", "flow", "link", "image", "video", "web", "chart")

MAX_LABEL = 40
MAX_TITLE = 60
MAX_LINE = 200
MAX_BODY_LINES = 6
MAX_VALUE = 24
MAX_CAPTION = 120
MAX_ALT = 120
MAX_MEDIA_ITEMS = 6
MAX_ITEMS = 12
MAX_NODES = 12
MAX_EDGES = 24
MAX_NODE_TEXT = 60
MAX_EDGE_LABEL = 30
MAX_SOURCE = 60
MAX_ID = 64
MAX_URL = 2048
MAX_SHAPES = 12
MAX_CARDS = 12

_FILE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _text(value, limit: int) -> str:
    """Untrusted value -> single-line string clamped to `limit` chars ('' if not text-like)."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return _CTRL.sub(" ", value).strip()[:limit]


def _id(value) -> str:
    return _text(value, MAX_ID)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def safe_https_url(value) -> str | None:
    """Return the URL if it is a well-formed https URL with a host, else None."""
    if not isinstance(value, str) or not value or len(value) > MAX_URL:
        return None
    if _CTRL.search(value) or any(c.isspace() for c in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname:
        return None
    return value


def youtube_id_from_url(value) -> str | None:
    """YouTube video id from an https watch/shorts/embed/youtu.be URL, else None."""
    url = safe_https_url(value)
    if not url:
        return None
    parts = urlsplit(url)
    host, path = parts.hostname, parts.path  # hostname is already lowercase
    vid = ""
    if host in ("www.youtube.com", "youtube.com", "m.youtube.com") and path == "/watch":
        vid = parse_qs(parts.query).get("v", [""])[0]
    elif host == "www.youtube.com":
        m = re.fullmatch(r"/(?:shorts|embed)/([^/]+)", path)
        vid = m.group(1) if m else ""
    elif host == "www.youtube-nocookie.com":  # embed only, same rule as the Swift parser
        m = re.fullmatch(r"/embed/([^/]+)", path)
        vid = m.group(1) if m else ""
    elif host == "youtu.be":
        m = re.fullmatch(r"/([^/]+)", path)
        vid = m.group(1) if m else ""
    return vid if _YOUTUBE_ID.fullmatch(vid) else None


def validate_shape(raw) -> dict | None:
    """One shape or None. Unknown kinds (arrow, freeform, mask, ...) are dropped."""
    if not isinstance(raw, dict) or raw.get("kind") not in SHAPE_KINDS:
        return None
    box = raw.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    nums = [_number(v) for v in box]
    if any(v is None for v in nums):
        return None
    x0, x1 = sorted((nums[0], nums[2]))
    y0, y1 = sorted((nums[1], nums[3]))
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    out = {"kind": raw["kind"], "box": [x0, y0, x1, y1]}
    label = _text(raw.get("label"), MAX_LABEL)
    if label:
        out["label"] = label
    sid = _id(raw.get("id"))
    if sid:
        out["id"] = sid
    return out


def _info(raw: dict, out: dict) -> dict | None:
    body = raw.get("body")
    if isinstance(body, str):
        body = body.splitlines()
    if not isinstance(body, list):
        return None
    lines = [t for t in (_text(b, MAX_LINE) for b in body) if t][:MAX_BODY_LINES]
    if not lines:
        return None
    out["body"] = lines
    return out


def _stat(raw: dict, out: dict) -> dict | None:
    value = _text(raw.get("value"), MAX_VALUE)
    if not value:
        return None
    out["value"] = value
    out["label"] = _text(raw.get("label"), MAX_LABEL)
    return out


def _bar(raw: dict, out: dict) -> dict | None:
    items = raw.get("items")
    if not isinstance(items, list):
        return None
    clean = []
    for it in items:
        if not isinstance(it, dict):
            continue
        label, value = _text(it.get("label"), MAX_LABEL), _number(it.get("value"))
        if label and value is not None:
            clean.append({"label": label, "value": value})
    if not clean:
        return None
    out["items"] = clean[:MAX_ITEMS]
    caption = _text(raw.get("caption"), MAX_CAPTION)
    if caption:
        out["caption"] = caption
    return out


def _flow(raw: dict, out: dict) -> dict | None:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return None
    clean, seen = [], set()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid, text = _id(n.get("id")), _text(n.get("text"), MAX_NODE_TEXT)
        if nid and text and nid not in seen:
            seen.add(nid)
            clean.append({"id": nid, "text": text})
        if len(clean) == MAX_NODES:
            break
    if not clean:
        return None
    edges = []
    for e in raw.get("edges") or []:
        if not isinstance(e, (list, tuple)) or len(e) not in (2, 3):
            continue
        src, dst = _id(e[0]), _id(e[1])
        if src in seen and dst in seen:
            label = _text(e[2], MAX_EDGE_LABEL) if len(e) == 3 else ""
            edges.append([src, dst, label] if label else [src, dst])
        if len(edges) == MAX_EDGES:
            break
    out["nodes"], out["edges"] = clean, edges
    return out


def _link(raw: dict, out: dict) -> dict | None:
    url = safe_https_url(raw.get("url"))
    if not url:
        return None
    out["url"] = url
    source = _text(raw.get("source"), MAX_SOURCE)
    if source:
        out["source"] = source
    return out


def _link_fallback(out: dict, url: str) -> dict:
    """An image/video with no usable media degrades to a link card on `url`."""
    return {"id": out["id"], "type": "link", "title": out["title"], "url": url,
            "source": urlsplit(url).hostname[:MAX_SOURCE]}


def _image(raw: dict, out: dict) -> dict | None:
    alt, source_page = _text(raw.get("alt"), MAX_ALT), safe_https_url(raw.get("source_page"))
    caption = _text(raw.get("caption"), MAX_CAPTION)
    if alt:
        out["alt"] = alt
    if caption:
        out["caption"] = caption
    if source_page:
        out["source_page"] = source_page
    items = raw.get("items")
    if isinstance(items, list):
        clean = []
        for it in items:
            if not isinstance(it, dict):
                continue
            iurl = safe_https_url(it.get("url"))
            if not iurl:
                continue
            item = {"url": iurl}
            ialt = _text(it.get("alt"), MAX_ALT)
            if ialt:
                item["alt"] = ialt
            icap = _text(it.get("caption"), MAX_CAPTION)
            if icap:
                item["caption"] = icap
            isp = safe_https_url(it.get("source_page"))
            if isp:
                item["source_page"] = isp
            clean.append(item)
            if len(clean) == MAX_MEDIA_ITEMS:
                break
        if len(clean) >= 2:
            out["items"], out["url"] = clean, clean[0]["url"]
            return out
        if len(clean) == 1:
            item = clean[0]
            if not alt and item.get("alt"):
                out["alt"] = item["alt"]
            if not caption and item.get("caption"):
                out["caption"] = item["caption"]
            if not source_page and item.get("source_page"):
                out["source_page"] = item["source_page"]
            out["url"] = item["url"]
            return out
    url = safe_https_url(raw.get("url"))
    file_id = raw.get("file_id")
    if url:
        out["url"] = url
    elif isinstance(file_id, str) and _FILE_ID.match(file_id):
        out["file_id"] = file_id
    elif source_page:
        return _link_fallback(out, source_page)
    else:
        return None
    return out


def _video(raw: dict, out: dict) -> dict | None:
    yid = raw.get("youtube_id")
    if not (isinstance(yid, str) and _YOUTUBE_ID.fullmatch(yid)):
        yid = youtube_id_from_url(raw.get("url"))
    raw_url, source_page = safe_https_url(raw.get("url")), safe_https_url(raw.get("source_page"))
    if isinstance(yid, str):  # YouTube cards never carry thumbnail_url
        out["youtube_id"] = yid
    elif raw_url and urlsplit(raw_url).path.lower().endswith((".mp4", ".mov", ".m4v")):
        out["url"] = raw_url
        thumb = safe_https_url(raw.get("thumbnail_url"))
        if thumb:
            out["thumbnail_url"] = thumb
    if "youtube_id" in out or "url" in out:
        if source_page:
            out["source_page"] = source_page
        return out
    link = source_page or raw_url
    if not link:
        return None
    return _link_fallback(out, link)


MAP_ATTRIBUTION = "© OpenStreetMap contributors"
MAX_ROUTES = 3
MAX_ROUTE_POINTS = 300
MAX_PINS = 2


def _coord(lat, lon) -> list[float] | None:
    """[lat, lon] rounded to 5 decimals, or None when not two in-range finite numbers."""
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lat, lon)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return [round(float(lat), 5), round(float(lon), 5)]


def _amount(value) -> float | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return round(float(value), 1) if ok else None


def _map_data(raw) -> dict | None:
    """The map page's data: <= 3 routes of <= 300 in-range points, <= 2 pins, the fixed attribution.
    Labels are plain text (the page puts them in with textContent only)."""
    if not isinstance(raw, dict):
        return None
    routes = []
    for r in raw["routes"] if isinstance(raw.get("routes"), list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("points"), list):
            continue
        pts = [c for c in (_coord(*p) if isinstance(p, (list, tuple)) and len(p) == 2 else None
                           for p in r["points"][:MAX_ROUTE_POINTS]) if c]
        km, mins = _amount(r.get("km")), _amount(r.get("min"))
        if len(pts) < 2 or km is None or mins is None:
            continue
        routes.append({"points": pts, "km": km, "min": mins})
        if len(routes) == MAX_ROUTES:
            break
    pins = []
    for p in raw["pins"] if isinstance(raw.get("pins"), list) else []:
        c = _coord(p.get("lat"), p.get("lon")) if isinstance(p, dict) else None
        if c:
            pins.append({"lat": c[0], "lon": c[1], "label": _text(p.get("label"), MAX_LABEL)})
        if len(pins) == MAX_PINS:
            break
    if not routes and not pins:
        return None
    out = {"routes": routes, "pins": pins, "attribution": MAP_ATTRIBUTION}
    # jfollow10f: the ranked places under the map; each field optional, absent stays absent.
    rows = [r for r in map(_place_row, raw["rows"] if isinstance(raw.get("rows"), list) else []) if r][:MAX_ROWS]
    if rows:
        out["rows"] = rows
    credits = [k for k in PLACE_CREDITS if isinstance(raw.get("credits"), list) and k in raw["credits"]]
    if credits:
        out["credits"] = credits
    if isinstance(raw.get("asof"), str) and _CLOCK.fullmatch(raw["asof"]):
        out["asof"] = raw["asof"]
    return out


MAX_ROWS = 5
PLACE_MODES = ("walk", "bus", "tube", "train")
PLACE_NOTES = ("times_not_listed", "none", "none_tomorrow")
PLACE_CREDITS = ("osm", "tfl", "clusterflick")  # the page maps each key to its fixed credit line
_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_HOURS = re.compile(r"open 24 hours|closed now|hours not listed|open until (?:midnight|(?:[01]\d|2[0-3]):[0-5]\d)")
# Bidi controls, zero-width marks and the BOM: web place names must not reorder or hide text.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def place_text(value, limit: int) -> str:
    """Untrusted web text (a place name, road, film title) -> plain single-line text within `limit`."""
    return _text(_INVISIBLE.sub("", value) if isinstance(value, str) else value, limit)


def _place_row(raw) -> dict | None:
    """One ranked place: name, road, minutes, mode, straight km, hours, <= 3 showings, a note.
    A row with no name, no km, or a mode/minutes outside the fixed set is dropped."""
    if not isinstance(raw, dict):
        return None
    name, km, mins = place_text(raw.get("name"), MAX_LABEL), raw.get("km"), raw.get("min")
    if not name or not (isinstance(km, (int, float)) and not isinstance(km, bool) and math.isfinite(km)
                        and 0 <= km <= 50):
        return None
    if "mode" in raw and raw["mode"] not in PLACE_MODES:
        return None
    if mins is not None and not (type(mins) is int and 0 <= mins <= 600):
        return None
    row = {"name": name, "km": round(float(km), 1)}
    road = place_text(raw.get("road"), MAX_LABEL)
    if road:
        row["road"] = road
    if mins is not None:
        row["min"] = mins
    if "mode" in raw:
        row["mode"] = raw["mode"]
    if isinstance(raw.get("hours"), str) and _HOURS.fullmatch(raw["hours"]):
        row["hours"] = raw["hours"]
    times = []
    for t in raw["times"] if isinstance(raw.get("times"), list) else []:
        if isinstance(t, dict) and isinstance(t.get("at"), str) and _CLOCK.fullmatch(t["at"]):
            times.append({"at": t["at"], "film": place_text(t.get("film"), MAX_LABEL)})
    if times:
        row["times"] = times[:3]
    if raw.get("note") in PLACE_NOTES:
        row["note"] = raw["note"]
    return row


# The bundled pages a web card may name -> their data validator. Adding a page: the files under
# mac/overlay/Resources/web/, one entry in WebCardPolicy.resources (Swift), one validator here.
MAX_LEGEND = 12
MAX_LEGEND_ITEM = 80


def _drawing_data(raw) -> dict | None:
    """The drawing page's data: an SVG that svgsafe.sanitize returns unchanged (it re-runs here; any
    difference drops the card) and a legend of <= 12 plain-text items of <= 80 chars."""
    if not isinstance(raw, dict) or not isinstance(raw.get("svg"), str):
        return None
    res = svgsafe.sanitize(raw["svg"])
    if res.reason != "ok" or res.svg != raw["svg"]:
        return None
    legend = raw.get("legend") if isinstance(raw.get("legend"), list) else []
    items = [t for t in (_text(x, MAX_LEGEND_ITEM) for x in legend) if t]
    return {"svg": res.svg, "legend": items[:MAX_LEGEND]}


_MODEL_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")


def _model_data(raw) -> dict | None:
    """The model page's data (jfollow10d): a vault object's slug and the view ("model" = the 3D
    object, "preview" = the ready card's picture). Other keys are dropped; the app fetches the files."""
    if not isinstance(raw, dict) or raw.get("view") not in ("model", "preview"):
        return None
    slug = raw.get("slug")
    if not isinstance(slug, str) or not _MODEL_SLUG.fullmatch(slug):
        return None
    return {"slug": slug, "view": raw["view"]}


WEB_PAGES = {"map": _map_data, "drawing": _drawing_data, "model": _model_data}


def drawing_card(title: str, svg: str, legend: list) -> dict | None:
    """The service's drawing card (jfollow10c): the one place a `drawing` web card is made. Callers
    pass svgsafe output built from the owner's own words; the model never names a web card."""
    return validate_card({"id": "dr" + uuid.uuid4().hex[:6], "type": "web", "title": title,
                          "page": "drawing", "data": {"svg": svg, "legend": legend}}, allow_web=True)


def _web(raw: dict, out: dict) -> dict | None:
    page = raw.get("page")
    if not isinstance(page, str) or page not in WEB_PAGES:
        return None
    data = WEB_PAGES[page](raw.get("data"))
    if data is None:
        return None
    out["page"], out["data"] = page, data
    return out


CHART_KINDS = ("pie", "donut", "line", "area", "bar", "stacked_bar", "scatter", "gauge")
MAX_CHART_LABELS = 12
MAX_CHART_LABEL = 24
MAX_SERIES = 4
MAX_SCATTER = 50
MAX_UNIT = 12


def _chart(raw: dict, out: dict) -> dict | None:
    """Labels + numeric series for a native chart; the model picks the kind and the numbers only.
    Any non-finite or non-number value, or a count outside the kind's rules, drops the card."""
    kind = raw.get("chart")
    series = raw.get("series")
    if kind not in CHART_KINDS or not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
        return None
    clean = []
    for s in series:
        if not isinstance(s, dict) or not isinstance(s.get("values"), list):
            return None
        if kind == "scatter":
            pairs = s["values"]
            if not all(isinstance(p, list) and len(p) == 2 for p in pairs):
                return None
            values = [[_number(p[0]), _number(p[1])] for p in pairs]
            if not 1 <= len(values) <= MAX_SCATTER or any(v is None for p in values for v in p):
                return None
        else:
            values = [_number(v) for v in s["values"]]
            if any(v is None for v in values):
                return None
        clean.append({"name": _text(s.get("name"), MAX_CHART_LABEL), "values": values})
    labels = raw.get("labels") if isinstance(raw.get("labels"), list) else []
    labels = [_text(x, MAX_CHART_LABEL) for x in labels]
    out["chart"] = kind
    if kind in ("pie", "donut"):
        vals = clean[0]["values"]
        if (len(clean) != 1 or not 2 <= len(labels) <= 8 or len(vals) != len(labels)
                or any(v < 0 for v in vals) or sum(vals) <= 0):
            return None
    elif kind in ("line", "area", "bar", "stacked_bar"):
        if not 2 <= len(labels) <= MAX_CHART_LABELS or any(len(s["values"]) != len(labels) for s in clean):
            return None
    elif kind == "gauge":
        lo, hi = _number(raw.get("min", 0)), _number(raw.get("max", 100))
        if len(clean) != 1 or len(clean[0]["values"]) != 1 or lo is None or hi is None or lo >= hi:
            return None
        clean[0]["values"] = [min(max(clean[0]["values"][0], lo), hi)]
        out["min"], out["max"] = lo, hi
    if kind not in ("scatter", "gauge"):
        out["labels"] = labels
    out["series"] = clean
    unit, caption = _text(raw.get("unit"), MAX_UNIT), _text(raw.get("caption"), MAX_CAPTION)
    if unit:
        out["unit"] = unit
    if caption:
        out["caption"] = caption
    return out


_CARD_BODY = {"info": _info, "stat": _stat, "bar": _bar, "flow": _flow, "link": _link,
              "image": _image, "video": _video, "web": _web, "chart": _chart}


def validate_card(raw, allow_web: bool = False) -> dict | None:
    """One card or None. Unknown types and unsafe URLs are dropped; text is clamped. A `web` card
    is kept only when service code passes allow_web=True: a model can never name a web card."""
    if not isinstance(raw, dict) or raw.get("type") not in CARD_TYPES:
        return None
    if raw["type"] == "web" and not allow_web:
        return None
    title = _text(raw.get("title"), MAX_TITLE)
    if not title:
        return None
    out = {"id": _id(raw.get("id")) or uuid.uuid4().hex[:8], "type": raw["type"], "title": title}
    return _CARD_BODY[raw["type"]](raw, out)


def validate_hud(msg, allow_web: bool = False) -> dict:
    """{shapes, cards} -> same shape with every invalid entry dropped (never raises)."""
    if not isinstance(msg, dict):
        return {"shapes": [], "cards": []}
    shapes = msg.get("shapes") if isinstance(msg.get("shapes"), list) else []
    cards = msg.get("cards") if isinstance(msg.get("cards"), list) else []
    return {
        "shapes": [s for s in map(validate_shape, shapes) if s][:MAX_SHAPES],
        "cards": [c for c in (validate_card(c, allow_web) for c in cards) if c][:MAX_CARDS],
    }


def _str(limit: int) -> dict:
    return {"type": "string", "maxLength": limit}


def schema_json() -> dict:
    """JSON Schema (draft 2020-12) of a validated HUD message; exported to tests/fixtures/hud/schema.json."""
    https = {"type": "string", "pattern": "^https://", "maxLength": MAX_URL}
    base = {"id": _str(MAX_ID), "title": {**_str(MAX_TITLE), "minLength": 1}}
    image_item = {
        "type": "object",
        "properties": {"url": https, "alt": _str(MAX_ALT), "caption": _str(MAX_CAPTION), "source_page": https},
        "required": ["url"],
        "additionalProperties": False,
    }

    def card(kind: str, props: dict, required: list[str]) -> dict:
        return {
            "type": "object",
            "properties": {"type": {"const": kind}, **base, **props},
            "required": ["id", "type", "title", *required],
            "additionalProperties": False,
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "jarvis-companion/hud.schema.json",
        "title": "Jarvis HUD message",
        "type": "object",
        "properties": {
            "shapes": {"type": "array", "maxItems": MAX_SHAPES, "items": {"$ref": "#/$defs/shape"}},
            "cards": {"type": "array", "maxItems": MAX_CARDS, "items": {"$ref": "#/$defs/card"}},
        },
        "required": ["shapes", "cards"],
        "$defs": {
            "shape": {
                "type": "object",
                "properties": {
                    "kind": {"enum": list(SHAPE_KINDS)},
                    "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "label": _str(MAX_LABEL),
                    "id": _str(MAX_ID),
                },
                "required": ["kind", "box"],
                "additionalProperties": False,
            },
            "card": {
                "oneOf": [
                    card("info", {"body": {"type": "array", "items": _str(MAX_LINE),
                                           "minItems": 1, "maxItems": MAX_BODY_LINES}}, ["body"]),
                    card("stat", {"value": _str(MAX_VALUE), "label": _str(MAX_LABEL)}, ["value", "label"]),
                    card("bar", {
                        "items": {"type": "array", "minItems": 1, "maxItems": MAX_ITEMS, "items": {
                            "type": "object",
                            "properties": {"label": _str(MAX_LABEL), "value": {"type": "number"}},
                            "required": ["label", "value"], "additionalProperties": False}},
                        "caption": _str(MAX_CAPTION),
                    }, ["items"]),
                    card("flow", {
                        "nodes": {"type": "array", "minItems": 1, "maxItems": MAX_NODES, "items": {
                            "type": "object",
                            "properties": {"id": _str(MAX_ID), "text": _str(MAX_NODE_TEXT)},
                            "required": ["id", "text"], "additionalProperties": False}},
                        "edges": {"type": "array", "maxItems": MAX_EDGES, "items": {
                            "type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3}},
                    }, ["nodes", "edges"]),
                    card("link", {"url": https, "source": _str(MAX_SOURCE)}, ["url"]),
                    {**card("image", {
                        "url": https, "alt": _str(MAX_ALT), "caption": _str(MAX_CAPTION), "source_page": https,
                        "items": {"type": "array", "minItems": 2, "maxItems": MAX_MEDIA_ITEMS,
                                  "items": image_item},
                        "file_id": {"type": "string", "pattern": _FILE_ID.pattern},
                    }, []),
                     "anyOf": [{"required": ["url"]}, {"required": ["file_id"]}]},
                    {**card("video", {
                        "youtube_id": {"type": "string", "pattern": _YOUTUBE_ID.pattern},
                        "url": https, "thumbnail_url": https, "source_page": https,
                    }, []),
                     "anyOf": [{"required": ["youtube_id"]}, {"required": ["url"]}]},
                    card("web", {"page": {"enum": list(WEB_PAGES)}, "data": {"oneOf": [{
                        "type": "object",
                        "properties": {
                            "routes": {"type": "array", "maxItems": MAX_ROUTES, "items": {
                                "type": "object",
                                "properties": {
                                    "points": {"type": "array", "minItems": 2, "maxItems": MAX_ROUTE_POINTS,
                                               "items": {"type": "array", "items": {"type": "number"},
                                                         "minItems": 2, "maxItems": 2}},
                                    "km": {"type": "number", "minimum": 0},
                                    "min": {"type": "number", "minimum": 0}},
                                "required": ["points", "km", "min"], "additionalProperties": False}},
                            "pins": {"type": "array", "maxItems": MAX_PINS, "items": {
                                "type": "object",
                                "properties": {"lat": {"type": "number", "minimum": -90, "maximum": 90},
                                               "lon": {"type": "number", "minimum": -180, "maximum": 180},
                                               "label": _str(MAX_LABEL)},
                                "required": ["lat", "lon", "label"], "additionalProperties": False}},
                            "attribution": {"const": MAP_ATTRIBUTION},
                            "rows": {"type": "array", "minItems": 1, "maxItems": MAX_ROWS, "items": {
                                "type": "object",
                                "properties": {
                                    "name": {**_str(MAX_LABEL), "minLength": 1}, "road": _str(MAX_LABEL),
                                    "min": {"type": "integer", "minimum": 0, "maximum": 600},
                                    "mode": {"enum": list(PLACE_MODES)},
                                    "km": {"type": "number", "minimum": 0, "maximum": 50},
                                    "hours": {"type": "string", "pattern": f"^(?:{_HOURS.pattern})$"},
                                    "times": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
                                        "type": "object",
                                        "properties": {"at": {"type": "string", "pattern": f"^{_CLOCK.pattern}$"},
                                                       "film": _str(MAX_LABEL)},
                                        "required": ["at", "film"], "additionalProperties": False}},
                                    "note": {"enum": list(PLACE_NOTES)}},
                                "required": ["name", "km"], "additionalProperties": False}},
                            "credits": {"type": "array", "minItems": 1, "maxItems": len(PLACE_CREDITS),
                                        "items": {"enum": list(PLACE_CREDITS)}},
                            "asof": {"type": "string", "pattern": f"^{_CLOCK.pattern}$"}},
                        "required": ["routes", "pins", "attribution"], "additionalProperties": False},
                        {"type": "object",
                         "properties": {"svg": {"type": "string", "maxLength": svgsafe.MAX_BYTES},
                                        "legend": {"type": "array", "maxItems": MAX_LEGEND,
                                                   "items": _str(MAX_LEGEND_ITEM)}},
                         "required": ["svg", "legend"], "additionalProperties": False},
                        {"type": "object",
                         "properties": {"slug": {"type": "string", "pattern": "^" + _MODEL_SLUG.pattern + "$"},
                                        "view": {"enum": ["model", "preview"]}},
                         "required": ["slug", "view"], "additionalProperties": False}]}},
                        ["page", "data"]),
                    card("chart", {
                        "chart": {"enum": list(CHART_KINDS)},
                        "labels": {"type": "array", "maxItems": MAX_CHART_LABELS, "items": _str(MAX_CHART_LABEL)},
                        "series": {"type": "array", "minItems": 1, "maxItems": MAX_SERIES, "items": {
                            "type": "object",
                            "properties": {"name": _str(MAX_CHART_LABEL), "values": {
                                "type": "array", "maxItems": MAX_SCATTER,
                                "items": {"oneOf": [{"type": "number"}, {
                                    "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}]}}},
                            "required": ["name", "values"], "additionalProperties": False}},
                        "unit": _str(MAX_UNIT), "caption": _str(MAX_CAPTION),
                        "min": {"type": "number"}, "max": {"type": "number"},
                    }, ["chart", "series"]),
                ]
            },
        },
    }
