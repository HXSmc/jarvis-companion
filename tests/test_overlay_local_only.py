"""Mac-only seam of router-local-model (C11/C12): the voice overlay reads /central/sessions and
hands rows to its cloud model; a local-only chat's title, doing line and card ask never do."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from gateway import overlay  # noqa: E402
from mac.voice import intents  # noqa: E402
from test_overlay_jfollow10g import TOKEN, URL, env  # noqa: E402

ROWS = [
    {"session_id": "s-local", "state": "needs_input", "device": "Mini PC", "phase": "in_progress",
     "title": "SECRET_LOCAL_TITLE", "doing": "SECRET_LOCAL_DOING", "local_only": True,
     "upstream": "gaming-pc", "last_activity_at": "2026-11-20T10:00:00Z",
     "card": {"need": {"id": "abcdef012345", "kind": "choice", "question": "SECRET_Q",
                       "options": ["SECRET_OPT_A", "SECRET_OPT_B"]}}},
    {"session_id": "s-cloud", "state": "running", "device": "Mini PC", "phase": "in_progress",
     "title": "Fix login", "doing": "Patching auth", "local_only": False,
     "last_activity_at": "2026-11-20T09:00:00Z"},
]


def test_overlay_local_rows_carry_no_chat_text(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, ROWS)
    rows, status = overlay._fetch_sessions(URL, TOKEN)
    assert status == 200 and [r["id"] for r in rows] == ["s-local", "s-cloud"]
    local, cloud = rows
    assert local["title"] == "Local chat" and local["doing"] == "" and local["need"] is None
    assert cloud["title"] == "Fix login" and cloud["doing"] == "Patching auth"
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    seen = " ".join([overlay._sessions_note(rows), overlay.session_prompt("fix it", local),
                     str(overlay._session_list_card(rows, intents.SessionsFilter(False, "", ""), now))])
    assert "SECRET" not in seen
    prompt = overlay.session_prompt("fix it", local)
    assert "s-local" not in prompt and "transcript" not in prompt
    assert "transcript" in overlay.session_prompt("fix it", cloud)  # cloud refs keep the fix-it line
