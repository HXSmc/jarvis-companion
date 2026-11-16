"""Captions of one YouTube video for the "what does the video say" question (jfollow10b).

youtube-transcript-api (MIT, pure Python, pinned on the installer's pip line) is imported only when
a transcript is asked for, so the service still starts without it. It talks only to YouTube's own
hosts (www.youtube.com); the id is checked against the 11-character pattern first. The transcript
is untrusted data: the caller wraps it, never parses it for directives.
"""

from __future__ import annotations

import concurrent.futures
import logging

from common import hud

logger = logging.getLogger(__name__)

LANGUAGES = ["en", "en-GB", "en-US"]
MAX_CHARS = 60_000
WAIT_S = 15.0
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="captions")


def _fetch(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi  # lazy: an optional dependency

    fetched = YouTubeTranscriptApi().fetch(video_id, languages=LANGUAGES)
    return " ".join(" ".join(str(s.text).split()) for s in fetched)


def transcript(video_id: str) -> str | None:
    """The joined English captions (<= 60,000 chars), or None (no captions, not installed, error, 15 s)."""
    if not (isinstance(video_id, str) and hud._YOUTUBE_ID.fullmatch(video_id)):
        return None
    try:
        text = _POOL.submit(_fetch, video_id).result(timeout=WAIT_S)
    except Exception as e:  # missing library, no captions, blocked, timeout: the reason class only
        logger.info("captions: %s", type(e).__name__)
        return None
    return text[:MAX_CHARS] or None
