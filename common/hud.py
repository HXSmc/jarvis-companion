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
      image: url (https) or file_id; alt?: str<=120, source_page?: https, gallery
             items?: [{url, alt?, source_page?}] <= 6 (kept only with 2+; url = first item)
      video: youtube_id, or url that is a YouTube watch/shorts/embed/youtu.be link or ends
             .mp4/.mov/.m4v/.m3u8, thumbnail_url? (file urls only), source_page?;
             no usable media -> link card on source_page/url, else dropped
    hud   = {shapes: [shape], cards: [card]}
"""

from __future__ import annotations

import math
import re
import uuid
from urllib.parse import parse_qs, urlsplit

SHAPE_KINDS = ("rect", "circle", "triangle")
CARD_TYPES = ("info", "stat", "bar", "flow", "link", "image", "video")

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
    elif host in ("www.youtube.com", "www.youtube-nocookie.com"):
        m = re.fullmatch(r"/(?:shorts|embed)/([^/]+)", path)
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
    if alt:
        out["alt"] = alt
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
    elif raw_url and urlsplit(raw_url).path.lower().endswith((".mp4", ".mov", ".m4v", ".m3u8")):
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


_CARD_BODY = {"info": _info, "stat": _stat, "bar": _bar, "flow": _flow, "link": _link,
              "image": _image, "video": _video}


def validate_card(raw) -> dict | None:
    """One card or None. Unknown types and unsafe URLs are dropped; text is clamped."""
    if not isinstance(raw, dict) or raw.get("type") not in CARD_TYPES:
        return None
    title = _text(raw.get("title"), MAX_TITLE)
    if not title:
        return None
    out = {"id": _id(raw.get("id")) or uuid.uuid4().hex[:8], "type": raw["type"], "title": title}
    return _CARD_BODY[raw["type"]](raw, out)


def validate_hud(msg) -> dict:
    """{shapes, cards} -> same shape with every invalid entry dropped (never raises)."""
    if not isinstance(msg, dict):
        return {"shapes": [], "cards": []}
    shapes = msg.get("shapes") if isinstance(msg.get("shapes"), list) else []
    cards = msg.get("cards") if isinstance(msg.get("cards"), list) else []
    return {
        "shapes": [s for s in map(validate_shape, shapes) if s][:MAX_SHAPES],
        "cards": [c for c in map(validate_card, cards) if c][:MAX_CARDS],
    }


def _str(limit: int) -> dict:
    return {"type": "string", "maxLength": limit}


def schema_json() -> dict:
    """JSON Schema (draft 2020-12) of a validated HUD message; exported to tests/fixtures/hud/schema.json."""
    https = {"type": "string", "pattern": "^https://", "maxLength": MAX_URL}
    base = {"id": _str(MAX_ID), "title": {**_str(MAX_TITLE), "minLength": 1}}
    image_item = {
        "type": "object",
        "properties": {"url": https, "alt": _str(MAX_ALT), "source_page": https},
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
                        "url": https, "alt": _str(MAX_ALT), "source_page": https,
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
                ]
            },
        },
    }
