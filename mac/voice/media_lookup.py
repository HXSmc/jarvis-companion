"""Pictures for the overlay from one fixed source: the Wikimedia Commons search API.

The fast-tier model never writes an image URL. It emits {"image_search": {"query", "count"}};
the service asks Commons (fixed host, no key, no redirects, 4 s, 512 KB cap, no cookies) and
builds the image card from the response. Everything Commons returns is untrusted text: it is
stripped of markup and control characters, capped, shown on the card and never fed back to a model.

Licences: public domain and CC0 only (ALLOWED_LICENCES), asked of the search and re-checked per hit.
Content filter (no key, no dependency): bitmap files only, the ~800 px thumbnail never the
original, and a word blocklist on the query, the file title, its object name and its categories.
shortcut: Commons has no safe-search flag, so an explicit file with innocent words and no telling
title or category still gets through; upgrade to a real image classifier if that ever shows up.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import unicodedata
from urllib.parse import urlsplit

import httpx

from common import hud

logger = logging.getLogger(__name__)
# httpx logs every request line (with the search words) at INFO, and the service log runs at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API_ENDPOINT = "https://commons.wikimedia.org/w/api.php"
# Wikimedia User-Agent policy: client name/version plus a contact (the public repo), no personal data.
USER_AGENT = f"jarvis-companion-overlay/1.0 (https://github.com/HXSmc/jarvis-companion) httpx/{httpx.__version__}"
MEDIA_HOSTS = frozenset({"thumb.wikimedia.org", "upload.wikimedia.org"})  # exact hostnames, never a suffix
PAGE_HOST = "commons.wikimedia.org"
MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
MEDIA_TYPES = frozenset({"image", "video"})
TIMEOUT_S = 4.0
MAX_BYTES = 512 * 1024
THROTTLE_S = 60  # after a 429 (Wikimedia API usage guidelines: back off)
SEARCH_LIMIT = 20  # still one request (~50 KB); the filters drop some hits
THUMB_WIDTH = 800
MAX_QUERY, MAX_TITLE, MAX_ARTIST, MAX_LICENCE = 80, 60, 40, 30
MAX_COUNT = hud.MAX_MEDIA_ITEMS
# Only licences that need no visible credit while the Mac does not draw `caption` (task
# f52-media-caption). Key = CommonsMetadata `License` code (extmetadata); value = the Commons
# structured-data statement the search filters on. Widen both together, e.g.
# "cc-by-4.0": "P275=Q20007257", once the caption is on screen.
ALLOWED_LICENCES = {"pd": "P6216=Q19652", "cc0": "P275=Q6938433"}

# Explicit and gore words; a match in the query skips the fetch, a match in a hit drops the hit.
_BLOCK = re.compile(
    r"\b(nude|nudes|nudity|naked|topless|porn\w*|sex|sexy|sexual\w*|erotic\w*|genital\w*|penis\w*|"
    r"vagina\w*|vulva|breasts?|nipples?|buttocks|nsfw|hentai|fetish\w*|bdsm|bondage|masturbat\w*|"
    r"intercourse|gore|gory|corpses?|cadavers?|dead bod(y|ies)|decapitat\w*|beheaded|beheading|"
    r"autopsy|mutilat\w*|lynching|suicide)\b",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]*>")
_MARKUP = re.compile(r"[<>*_`#~|\[\]\\{}]")
_QUERY_JUNK = re.compile(r"[^\w\s'-]")  # drops search operators (insource:, quotes, regex slashes)
_EXT = re.compile(r"\.(jpe?g|png|webp|gif|tiff?|svg)$", re.IGNORECASE)

_lock = threading.Lock()
_throttled_until = 0.0
_transport: httpx.BaseTransport | None = None  # tests swap in httpx.MockTransport


def clean_text(value, limit: int) -> str:
    """Untrusted web text -> plain single line: no tags, entities, markup or control chars, capped."""
    if not isinstance(value, str):
        return ""
    text = html.unescape(_TAG.sub(" ", value))
    text = _TAG.sub(" ", text)  # tags that arrived entity-encoded
    text = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in text)
    return " ".join(_MARKUP.sub(" ", text).split())[:limit].strip()


def clean_query(value) -> str:
    """Model text -> plain search words, <= 80 chars; '' when nothing is left."""
    text = clean_text(value, 500)
    return " ".join(_QUERY_JUNK.sub(" ", text).split())[:MAX_QUERY].strip()


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1
    return max(1, min(MAX_COUNT, int(value)))


def _exact_host(value, hosts) -> str | None:
    """https URL whose hostname is exactly one of `hosts`, with no userinfo or port; malformed -> None."""
    url = hud.safe_https_url(value)
    if not url:
        return None
    parts = urlsplit(url)
    try:
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.hostname not in hosts or parts.username is not None or port is not None:
        return None
    return url


def _media_url(value) -> str | None:
    return _exact_host(value, MEDIA_HOSTS)


def _page_url(value) -> str | None:
    return _exact_host(value, (PAGE_HOST,))


def _meta(ext: dict, key: str) -> str:
    entry = ext.get(key)
    value = entry.get("value") if isinstance(entry, dict) else None
    return value if isinstance(value, str) else ""


def _hit(page) -> dict | None:
    """One Commons search page -> {url, title, source_page, licence, artist}, or None to drop it."""
    if not isinstance(page, dict) or not isinstance(page.get("imageinfo"), list) or not page["imageinfo"]:
        return None
    info = page["imageinfo"][0]
    if not isinstance(info, dict) or info.get("mime") not in MIMES:
        return None
    url, source_page = _media_url(info.get("thumburl")), _page_url(info.get("descriptionurl"))
    ext = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}
    licence = clean_text(_meta(ext, "LicenseShortName"), MAX_LICENCE)
    raw_title = page.get("title") if isinstance(page.get("title"), str) else ""
    if not (url and source_page and licence):
        return None
    if (_meta(ext, "License").strip().lower() not in ALLOWED_LICENCES
            or _meta(ext, "AttributionRequired").strip().lower() != "false"):
        return None
    if _BLOCK.search(" ".join((raw_title, _meta(ext, "ObjectName"), _meta(ext, "Categories")))):
        return None
    title = clean_text(_EXT.sub("", raw_title.removeprefix("File:")).replace("_", " "), MAX_TITLE)
    return {"url": url, "title": title, "source_page": source_page, "licence": licence,
            "artist": clean_text(_meta(ext, "Artist"), MAX_ARTIST)}


def _fetch(params: dict) -> dict | None:
    """One GET to the fixed endpoint. None on any failure (logged as a reason, never the URL or query)."""
    global _throttled_until
    deadline = time.monotonic() + TIMEOUT_S
    try:
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False, transport=_transport,
                          headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}) as client:
            with client.stream("GET", API_ENDPOINT, params=params) as resp:
                if resp.status_code != 200:
                    if resp.status_code == 429:
                        with _lock:
                            _throttled_until = time.monotonic() + THROTTLE_S
                    logger.info("image lookup: HTTP %s", resp.status_code)
                    return None
                body = bytearray()
                for chunk in resp.iter_raw():  # raw bytes: the cap is on what crosses the wire
                    body += chunk
                    if len(body) > MAX_BYTES:
                        logger.info("image lookup: response over %s bytes", MAX_BYTES)
                        return None
                    if time.monotonic() > deadline:
                        logger.info("image lookup: timeout")
                        return None
        data = json.loads(bytes(body))
    except httpx.TimeoutException:
        logger.info("image lookup: timeout")
        return None
    except (httpx.HTTPError, ValueError) as e:
        logger.info("image lookup: %s", type(e).__name__)
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("error"), dict):
        code = str(data["error"].get("code", ""))[:40]
        if code == "ratelimited":
            with _lock:
                _throttled_until = time.monotonic() + THROTTLE_S
        logger.info("image lookup: API error %s", clean_text(code, 40))
        return None
    return data


def search(query, count=1) -> list[dict]:
    """Up to `count` (1-6) safe Commons hits for model text `query`; [] on any failure. Never raises."""
    try:
        words = clean_query(query)
        if not words:
            return []
        if _BLOCK.search(words):
            logger.info("image lookup: query refused by the content filter")
            return []
        with _lock:
            if time.monotonic() < _throttled_until:
                logger.info("image lookup: throttled after a 429")
                return []
        data = _fetch({
            "action": "query", "format": "json", "formatversion": "2",
            "generator": "search", "gsrsearch": f"{words} filetype:bitmap haswbstatement:{'|'.join(ALLOWED_LICENCES.values())}", "gsrnamespace": "6",
            "gsrlimit": str(SEARCH_LIMIT), "prop": "imageinfo", "iiprop": "url|mime|size|extmetadata",
            "iiurlwidth": str(THUMB_WIDTH), "iiextmetadatalanguage": "en",
            "iiextmetadatafilter": "ObjectName|Artist|LicenseShortName|License|AttributionRequired|Categories",
        })
        result = data.get("query") if data else None
        pages = result.get("pages") if isinstance(result, dict) else None
        if not isinstance(pages, list):
            return []
        pages = sorted((p for p in pages if isinstance(p, dict)),
                       key=lambda p: p.get("index") if isinstance(p.get("index"), int) else 1 << 30)
        hits = [h for h in map(_hit, pages) if h]
        return hits[:_count(count)]
    except Exception as e:  # a lookup bug must never break the spoken answer
        logger.info("image lookup: %s", type(e).__name__)
        return []


def caption(hit: dict) -> str:
    """Attribution: "<artist>, <licence>, Wikimedia Commons" (artist capped first; <= 92 chars)."""
    return ", ".join(p for p in (hit.get("artist"), hit.get("licence"), "Wikimedia Commons") if p)


def card(query, hits: list[dict]) -> dict | None:
    """Raw image card (one picture, or a gallery for 2-6) built only from lookup hits."""
    if not hits:
        return None
    title = " ".join(w[:1].upper() + w[1:] for w in clean_query(query).split())[:MAX_TITLE] or "Pictures"
    items = [{"url": h["url"], "alt": h["title"] or title, "caption": caption(h),
              "source_page": h["source_page"]} for h in hits[:MAX_COUNT]]
    if len(items) == 1:
        return {"type": "image", "title": title, **items[0]}
    return {"type": "image", "title": title, "url": items[0]["url"], "items": items}


def find(query, count=1) -> dict | None:
    """image_search -> raw image card or None (nothing safe found / lookup down)."""
    n = _count(count)
    return card(query, search(query, n))


def fast_tier_card(raw) -> dict | None:
    """A model-written card on the fast tier, after hud.validate_card, with the media rules:
    a model image URL is refused (only image_search makes web pictures; a saved file_id stays),
    a video is YouTube only (a direct file becomes a link), and a YouTube link becomes a video."""
    kind = raw.get("type") if isinstance(raw, dict) and raw.get("type") in hud.CARD_TYPES else "unknown"
    out = hud.validate_card(raw)
    if out is None:
        logger.info("card dropped: %s failed validation", kind)
        return None
    if out["type"] == "image" and "url" in out:
        logger.info("media card dropped: model-written image link")
        return None
    if out["type"] == "video" and "youtube_id" not in out:
        out = hud.validate_card({"id": out["id"], "type": "link", "title": out["title"], "url": out["url"]})
    elif out["type"] == "link" and hud.youtube_id_from_url(out["url"]):
        out = hud.validate_card({"id": out["id"], "type": "video", "title": out["title"],
                                 "youtube_id": hud.youtube_id_from_url(out["url"])})
    if out is not None and out["type"] != kind:
        logger.info("card %s became %s", kind, out["type"])
    return out
