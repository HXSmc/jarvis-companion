"""mac/voice/media_lookup.find_video: the YouTube video lookup, on recorded API JSON (no network)."""

import json
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from common import hud
from mac.voice import media_lookup

FIXTURES = Path(__file__).parent / "fixtures" / "overlay"
FAKE = "AIzaFAKE-test-key-0000000000000000000"
# Bodies from the live API, shape per https://developers.google.com/youtube/v3/docs/errors
BAD_KEY_BODY = {
    "error": {
        "code": 400,
        "message": "API key not valid. Please pass a valid API key.",
        "errors": [{"message": "API key not valid. Please pass a valid API key.",
                    "domain": "global", "reason": "badRequest"}],
        "status": "INVALID_ARGUMENT",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": "API_KEY_INVALID", "domain": "googleapis.com",
                     "metadata": {"service": "youtube.googleapis.com"}}],
    }
}
QUOTA_BODY = {
    "error": {
        "code": 403,
        "message": "The request cannot be completed because you have exceeded your quota.",
        "errors": [{"message": "The request cannot be completed because you have exceeded your quota.",
                    "domain": "youtube.quota", "reason": "quotaExceeded"}],
    }
}


def fixture(name: str) -> bytes:
    return (FIXTURES / f"youtube_{name}.json").read_bytes()


def ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, stream=httpx.ByteStream(body))  # a real stream, like the network


def status_reply(code: int, body=None) -> httpx.Response:
    raw = json.dumps(body).encode() if body is not None else b""
    return httpx.Response(code, stream=httpx.ByteStream(raw))


@pytest.fixture
def video(monkeypatch, tmp_path):
    """Isolated video state: no env key, a tmp .env, empty caches and gates, a mock transport
    that records every request, and a fixed (movable) clock."""
    state = {"requests": [], "reply": lambda req: ok(fixture("rocket")),
             "now": datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["reply"](request)

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(media_lookup, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(media_lookup, "_video_cache", {})
    monkeypatch.setattr(media_lookup, "_quota_until", None)
    monkeypatch.setattr(media_lookup, "_key_retry_until", None)
    monkeypatch.setattr(media_lookup, "_throttled_until", 0.0)
    monkeypatch.setattr(media_lookup, "_now", lambda: state["now"])
    monkeypatch.setattr(media_lookup, "_transport", httpx.MockTransport(handler))
    return state


@pytest.fixture
def key(video, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", FAKE)
    return FAKE


def test_good_fixture_makes_one_valid_video_card(key, video):
    card, reason = media_lookup.find_video("  Rocket   launch!! ")
    assert reason == "ok"
    assert card == {"type": "video", "title": "Falcon 9 & Starship launch compilation",
                    "youtube_id": "Kq8Z1v2Rk3E"}
    assert "<" not in card["title"] and ">" not in card["title"] and len(card["title"]) <= 60
    assert hud.validate_card(card) is not None
    req = video["requests"][0]
    assert req.url.host == "www.googleapis.com" and req.url.path == "/youtube/v3/search"
    params = req.url.params
    assert params["part"] == "snippet" and params["type"] == "video"
    assert params["videoEmbeddable"] == "true" and params["safeSearch"] == "strict"
    assert params["maxResults"] == "10" and params["q"] == "Rocket launch"
    assert req.headers["x-goog-api-key"] == FAKE
    assert "key" not in params
    assert FAKE not in str(req.url)
    assert "cookie" not in req.headers


def test_bad_and_malformed_hits_are_skipped_and_allbad_is_none(key, video):
    card, reason = media_lookup.find_video("Rocket launch")  # hits 1-8 skipped, first good hit used
    assert reason == "ok" and card["youtube_id"] == "Kq8Z1v2Rk3E"
    video["reply"] = lambda req: ok(fixture("allbad"))
    assert media_lookup.find_video("another rocket") == (None, "none")


def test_script_and_control_chars_never_reach_the_title(key, video):
    nasty = "<script>x</script>Rocket &amp; launch\x00\x07 " + "a" * 100
    items = [{"kind": "youtube#searchResult",
              "id": {"kind": "youtube#video", "videoId": "Kq8Z1v2Rk3E"},
              "snippet": {"title": nasty, "description": "A launch", "channelTitle": "Space",
                          "liveBroadcastContent": "none"}}]
    video["reply"] = lambda req: ok(json.dumps({"kind": "youtube#searchListResponse",
                                                "items": items}).encode())
    card, reason = media_lookup.find_video("Rocket launch")
    assert reason == "ok"
    assert "<" not in card["title"] and ">" not in card["title"]
    assert not any(unicodedata.category(ch)[0] == "C" for ch in card["title"])
    assert len(card["title"]) <= 60


def test_blocked_and_empty_queries_never_fetch(key, video):
    assert media_lookup.find_video("naked rocket") == (None, "none")
    for query in ("", None, "!!!", {}):
        assert media_lookup.find_video(query) == (None, "none")
    assert video["requests"] == []


def test_key_comes_from_env_or_the_env_file(video, tmp_path):
    assert media_lookup.find_video("Rocket launch") == (None, "key_missing")
    assert video["requests"] == []
    (tmp_path / ".env").write_text(f'# c\nOTHER=1\nYOUTUBE_API_KEY="{FAKE}"\n')
    card, reason = media_lookup.find_video("Rocket launch")
    assert reason == "ok" and card["youtube_id"] == "Kq8Z1v2Rk3E"
    assert video["requests"][0].headers["x-goog-api-key"] == FAKE  # one pair of quotes stripped


def test_quota_error_blocks_until_the_next_midnight_pt(key, video):
    video["now"] = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)  # 12:00 PST
    video["reply"] = lambda req: status_reply(403, QUOTA_BODY)
    assert media_lookup.find_video("Rocket launch") == (None, "quota")
    video["reply"] = lambda req: ok(fixture("rocket"))
    assert media_lookup.find_video("other query") == (None, "quota")  # different query, gate holds
    assert len(video["requests"]) == 1
    video["now"] = datetime(2026, 11, 12, 7, 59, tzinfo=timezone.utc)  # 23:59 PST
    assert media_lookup.find_video("third query") == (None, "quota")
    assert len(video["requests"]) == 1
    video["now"] = datetime(2026, 11, 12, 8, 1, tzinfo=timezone.utc)  # 00:01 PST, reset
    card, reason = media_lookup.find_video("fourth query")
    assert reason == "ok" and len(video["requests"]) == 2


def test_rate_limit_429_counts_as_quota(key, video):
    video["reply"] = lambda req: status_reply(
        429, {"error": {"code": 429, "errors": [{"reason": "rateLimitExceeded"}]}})
    assert media_lookup.find_video("Rocket launch") == (None, "quota")
    assert media_lookup.find_video("more rockets") == (None, "quota")
    assert len(video["requests"]) == 1


def test_bad_key_backs_off_ten_minutes(key, video):
    video["reply"] = lambda req: status_reply(400, BAD_KEY_BODY)
    assert media_lookup.find_video("Rocket launch") == (None, "key_rejected")
    video["now"] = video["now"] + timedelta(minutes=5)
    assert media_lookup.find_video("another rocket") == (None, "key_rejected")
    assert len(video["requests"]) == 1
    video["now"] = video["now"] + timedelta(minutes=6)  # 11 min after the rejection
    video["reply"] = lambda req: ok(fixture("rocket"))
    card, reason = media_lookup.find_video("third rocket")
    assert reason == "ok" and len(video["requests"]) == 2


def test_redirect_oversize_500_and_transport_failures(key, video):
    video["reply"] = lambda req: httpx.Response(302, headers={"location": "https://evil.example/"})
    assert media_lookup.find_video("Rocket launch") == (None, "error")
    assert len(video["requests"]) == 1  # a 302 sends exactly one request
    video["reply"] = lambda req: ok(b" " * (media_lookup.MAX_BYTES + 1))
    assert media_lookup.find_video("big body") == (None, "error")
    video["reply"] = lambda req: httpx.Response(500)
    assert media_lookup.find_video("server error") == (None, "error")

    def connect_boom(req):
        raise httpx.ConnectError("boom", request=req)

    video["reply"] = connect_boom
    assert media_lookup.find_video("transport") == (None, "error")

    def read_boom(req):
        raise httpx.ReadTimeout("slow", request=req)

    video["reply"] = read_boom
    assert media_lookup.find_video("slow one") == (None, "timeout")


def test_cache_repeats_ttl_and_negative_results(key, video):
    card, reason = media_lookup.find_video("Rocket launch")
    assert reason == "ok"
    again, reason2 = media_lookup.find_video("  rocket   LAUNCH ")
    assert reason2 == "ok" and again == card and len(video["requests"]) == 1
    video["reply"] = lambda req: ok(fixture("allbad"))
    assert media_lookup.find_video("empty result") == (None, "none")
    assert media_lookup.find_video("empty result") == (None, "none")  # nothing found is cached too
    assert len(video["requests"]) == 2

    def read_boom(req):
        raise httpx.ReadTimeout("slow", request=req)

    video["reply"] = read_boom
    assert media_lookup.find_video("timeout query") == (None, "timeout")
    video["reply"] = lambda req: ok(fixture("rocket"))
    card2, reason3 = media_lookup.find_video("timeout query")  # a timeout is never cached
    assert reason3 == "ok" and card2["youtube_id"] == "Kq8Z1v2Rk3E"
    video["now"] = video["now"] + timedelta(hours=6, seconds=1)
    media_lookup.find_video("Rocket launch")  # past the TTL, a request goes out again
    assert len(video["requests"]) == 5


def test_the_key_never_reaches_any_log_or_returned_value(key, video, caplog):
    cases = [
        ("quota q", lambda req: status_reply(403, QUOTA_BODY)),
        ("bad key q", lambda req: status_reply(400, BAD_KEY_BODY)),
        ("redirect q", lambda req: httpx.Response(302, headers={"location": "https://evil.example/"})),
        ("server q", lambda req: httpx.Response(500)),
    ]
    caplog.set_level(logging.DEBUG)
    for query, reply in cases:
        video["reply"] = reply
        result = media_lookup.find_video(query)
        assert FAKE not in repr(result)
        media_lookup._quota_until = None  # gates off, so every case reaches the network path
        media_lookup._key_retry_until = None

    def url_boom(req):
        raise httpx.ConnectError(f"boom {req.url} {dict(req.headers)}", request=req)

    video["reply"] = url_boom
    result = media_lookup.find_video("boom q")
    assert FAKE not in repr(result)
    for record in caplog.records:
        assert FAKE not in record.getMessage()
        if record.exc_info:
            assert FAKE not in (record.exc_text or "")
