"""mac/voice/media_lookup: the Commons picture lookup, on recorded API JSON (no network)."""

import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from common import hud
from mac.voice import media_lookup

FIXTURES = Path(__file__).parent / "fixtures" / "overlay"
SUFFIX = " filetype:bitmap haswbstatement:P6216=Q19652|P275=Q6938433"


def fixture(name: str) -> bytes:
    return (FIXTURES / f"commons_{name}.json").read_bytes()


def ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, stream=httpx.ByteStream(body))  # a real stream, like the network


@pytest.fixture
def commons(monkeypatch):
    """Route media_lookup's httpx client to a mock; collect every request it sends."""
    state = {"requests": [], "reply": lambda req: ok(fixture("fox"))}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["reply"](request)

    monkeypatch.setattr(media_lookup, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(media_lookup, "_throttled_until", 0.0)
    return state


def test_three_good_hits(commons):
    hits = media_lookup.search("red fox", 3)
    assert len(hits) == 3
    for h in hits:
        assert urlsplit(h["url"]).hostname in media_lookup.MEDIA_HOSTS
        assert urlsplit(h["source_page"]).hostname == "commons.wikimedia.org"
        assert h["licence"] and h["artist"]
    assert hits[0]["title"] == "Common foxes in the snow"  # File: prefix and extension gone
    assert hits[0]["artist"] == "Wilhelm Kuhnert" and hits[0]["licence"] == "Public domain"
    assert [h["licence"] for h in hits] == ["Public domain", "CC0", "Public domain"]
    req = commons["requests"][0]
    assert req.url.host == "commons.wikimedia.org" and req.url.path == "/w/api.php"
    assert req.headers["user-agent"].startswith("jarvis-companion-overlay/1.0 (https://github.com/HXSmc/jarvis-companion)")
    assert "cookie" not in req.headers
    assert req.url.params["gsrsearch"] == "red fox" + SUFFIX and req.url.params["gsrnamespace"] == "6"


def test_count_limits_hits(commons):
    assert len(media_lookup.search("red fox", 1)) == 1
    assert len(media_lookup.search("red fox", 99)) == 3  # clamp to 6, only 3 exist


def test_bad_hits_are_dropped(commons):
    """Other host, http, SVG, webm, suffix/userinfo/port tricks, bad page host, no licence, CC BY,
    CC BY-SA, attribution required, blocklist."""
    commons["reply"] = lambda req: ok(fixture("bad"))
    hits = media_lookup.search("red fox", 6)
    assert [h["title"] for h in hits] == ["Panorama fox", "Tall strip fox", "Vulpes vulpes ssp fulvus"]


def _with_licence(code, short, attribution):
    data = json.loads(fixture("fox"))
    for page in data["query"]["pages"]:
        ext = page["imageinfo"][0]["extmetadata"]
        for key, value in (("License", code), ("LicenseShortName", short), ("AttributionRequired", attribution)):
            if value is None:
                ext.pop(key, None)
            else:
                ext[key] = {"value": value}
    return json.dumps(data).encode()


@pytest.mark.parametrize("code,short,attribution,kept", [
    ("pd", "Public domain", "false", 3),
    ("cc0", "CC0", "false", 3),
    ("cc-by-4.0", "CC BY 4.0", "true", 0),
    ("cc-by-sa-4.0", "CC BY-SA 4.0", "true", 0),
    (None, None, None, 0),  # missing licence
    ("pd", "Public domain", None, 0),  # unknown attribution flag
    ("cc0", "CC0", "true", 0),  # contradictory: needs credit -> dropped
])
def test_only_public_domain_and_cc0(commons, code, short, attribution, kept):
    commons["reply"] = lambda req: ok(_with_licence(code, short, attribution))
    assert len(media_lookup.search("red fox", 6)) == kept


def test_search_asks_commons_for_allowed_licences_only(commons):
    media_lookup.search("red fox", 1)
    sent = commons["requests"][0].url.params
    assert sent["gsrsearch"].endswith(" haswbstatement:P6216=Q19652|P275=Q6938433")
    assert sent["gsrlimit"] == "20" and "License" in sent["iiextmetadatafilter"]


def test_lookalike_hosts_are_refused():
    for url in ("https://upload.wikimedia.org.evil.com/x.jpg", "https://upload.wikimedia.org@evil.com/x.jpg",
                "https://thumb.wikimedia.org.evil.com/x.jpg", "https://thumb.wikimedia.org@evil.com/x.jpg",
                "https://user@upload.wikimedia.org/x.jpg", "https://upload.wikimedia.org:8443/x.jpg",
                "http://upload.wikimedia.org/x.jpg", "https://evilupload.wikimedia.org/x.jpg",
                "https://upload.wikimedia.org:bad/x.jpg", "https://upload.wikimedia.org:99999/x.jpg"):
        assert media_lookup._media_url(url) is None, url
    assert media_lookup._media_url("https://upload.wikimedia.org/a/b.jpg")


def test_artist_html_is_plain_and_capped():
    out = media_lookup.clean_text('<a href="https://x.example">Bob</a><script>x</script>', 40)
    assert out == "Bob x"
    assert media_lookup.clean_text("&lt;b&gt;**x**&lt;/b&gt; " + "y" * 80, 40).startswith("x")
    text = media_lookup.clean_text("<b>**x**</b>\x00\u202e" + "z" * 99, 40)
    assert not set(text) & set("<>*\x00‮") and len(text) <= 40


def test_redirect_gives_nothing_and_is_not_followed(commons):
    commons["reply"] = lambda req: httpx.Response(302, headers={"location": "https://evil.example/x"})
    assert media_lookup.search("red fox", 3) == []
    assert len(commons["requests"]) == 1


def test_oversize_body_gives_nothing(commons):
    commons["reply"] = lambda req: ok(b" " * (media_lookup.MAX_BYTES + 1) + fixture("fox"))
    assert media_lookup.search("red fox", 3) == []


def test_timeout_gives_nothing(commons):
    def boom(req):
        raise httpx.ReadTimeout("slow", request=req)
    commons["reply"] = boom
    assert media_lookup.search("red fox", 3) == []


@pytest.mark.parametrize("reply", [
    lambda req: httpx.Response(500),
    lambda req: httpx.Response(503),
    lambda req: ok(b"{not json"),
    lambda req: ok(b"[]"),
    lambda req: ok(fixture("empty")),
    lambda req: ok(b'{"error": {"code": "badvalue", "info": "<b>x</b>"}}'),
])
def test_failures_give_nothing(commons, reply):
    commons["reply"] = reply
    assert media_lookup.search("red fox", 3) == []


def test_429_backs_off_for_a_minute(commons, caplog):
    commons["reply"] = lambda req: httpx.Response(429)
    with caplog.at_level(logging.INFO, logger="mac.voice.media_lookup"):
        assert media_lookup.search("red fox", 3) == []
        commons["reply"] = lambda req: ok(fixture("fox"))
        assert media_lookup.search("red fox", 3) == []
    assert len(commons["requests"]) == 1  # the second lookup never went out
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "429" in logs and "fox" not in logs and "http" not in logs  # status, never query or URL


def test_query_is_cleaned_capped_and_sent_once(commons):
    nasty = 'insource:/x/ "red\nfox"\x00\x1b ' + "intitle:fox " * 50
    media_lookup.search(nasty, 2)
    assert len(commons["requests"]) == 1
    sent = commons["requests"][0].url.params["gsrsearch"]
    words = sent.removesuffix(SUFFIX)
    assert sent.endswith(SUFFIX) and len(words) <= 80
    assert not set(words) & set(':"/\n\x00\x1b')
    assert b"%0A" not in commons["requests"][0].url.raw_path  # no newline even encoded


def test_blocked_query_never_fetches(commons):
    assert media_lookup.search("nude fox", 1) == []
    assert media_lookup.search("  ", 1) == [] and media_lookup.search(None, 1) == []
    assert commons["requests"] == []


def test_card_single_and_gallery(commons):
    one = hud.validate_card(media_lookup.find("red fox", 1))
    assert one["type"] == "image" and "items" not in one and one["title"] == "Red Fox"
    assert one["alt"] == "Common foxes in the snow"  # description, not attribution
    assert one["caption"] == "Wilhelm Kuhnert, Public domain, Wikimedia Commons"
    gal = hud.validate_card(media_lookup.find("red fox", 3))
    assert len(gal["items"]) == 3 and gal["url"] == gal["items"][0]["url"]
    assert all(it["caption"].endswith("Wikimedia Commons") for it in gal["items"])


def test_caption_truncates_artist_never_licence():
    long = {"artist": media_lookup.clean_text("A" * 300, media_lookup.MAX_ARTIST), "licence": "CC BY-SA 4.0"}
    cap = media_lookup.caption(long)
    assert cap.endswith(", CC BY-SA 4.0, Wikimedia Commons") and len(cap) <= hud.MAX_CAPTION
    assert media_lookup.caption({"artist": "", "licence": "CC0"}) == "CC0, Wikimedia Commons"


def test_fast_tier_card_rules(caplog):
    with caplog.at_level(logging.INFO):
        assert media_lookup.fast_tier_card({"type": "image", "title": "Dog", "url": "https://evil.example/x.jpg"}) is None
        saved = media_lookup.fast_tier_card({"type": "image", "title": "Frame", "file_id": "frame_1.jpg"})
        assert saved["file_id"] == "frame_1.jpg"
        vid = media_lookup.fast_tier_card({"type": "link", "title": "Jet engine",
                                           "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
        assert vid["type"] == "video" and vid["youtube_id"] == "dQw4w9WgXcQ"
        bad = media_lookup.fast_tier_card({"type": "link", "title": "Jet", "url": "https://www.youtube.com/watch?v=bad"})
        assert bad["type"] == "link"
        for url in ("https://127.0.0.1/a.mp4", "https://localhost/x"):
            out = media_lookup.fast_tier_card({"type": "video", "title": "V", "url": url})
            assert out["type"] == "link" and "youtube_id" not in out  # never a playable file URL
        assert media_lookup.fast_tier_card({"type": "video", "title": "V", "url": "http://example.com/a.mp4"}) is None
    assert not any("http" in r.getMessage() for r in caplog.records)  # type and reason only


def test_search_words_never_reach_any_log(commons, caplog):
    with caplog.at_level(logging.DEBUG):
        media_lookup.search("zebracornmarker fox", 3)
        commons["reply"] = lambda req: httpx.Response(500)
        media_lookup.search("zebracornmarker fox", 3)
    assert commons["requests"] and not any("zebracornmarker" in r.getMessage() for r in caplog.records)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_malformed_port_drops_only_that_hit(commons):
    data = json.loads(fixture("fox"))
    data["query"]["pages"][0]["imageinfo"][0]["thumburl"] = "https://upload.wikimedia.org:bad/x.jpg"
    commons["reply"] = lambda req: ok(json.dumps(data).encode())
    hits = media_lookup.search("red fox", 6)
    assert len(hits) == len(data["query"]["pages"]) - 1 and all(":bad" not in h["url"] for h in hits)


def test_card_title_capitalises_words_not_after_apostrophes():
    hit = {"url": "https://thumb.wikimedia.org/a.jpg", "title": "t", "licence": "CC0", "artist": "",
           "source_page": "https://commons.wikimedia.org/wiki/File:a.jpg"}
    assert media_lookup.card("red fox's den", [hit])["title"] == "Red Fox's Den"
    assert media_lookup.card("eiffel tower in USA", [hit])["title"] == "Eiffel Tower In USA"
