"""jfollow10g: "router sessions" = every session on the Jarvis Companion app (the same
/central/sessions read, the board's columns); "done sessions" = the app's DONE column
(phase == "done"); a list answer shows device, date and what each session did, from fixed code
only; plain phrasings (incl. eval miss sess-1) reach the sessions intent. Fixture rows are invented."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from gateway import overlay  # noqa: E402
from mac.voice import auto_policy, intent_pick, intents  # noqa: E402
from test_overlay_agent import Clock, FakeAgentPool, client, reset  # noqa: F401,E402
from test_overlay_jfollow2 import lines  # noqa: E402
from test_overlay_jfollow4 import _cards, _deps, _steps, fresh_session_state  # noqa: F401,E402
from test_overlay_jfollow10a import bash, run_agent, _rows  # noqa: E402
from test_overlay_service import FakeConversations, says, turn  # noqa: E402

TOKEN = "not-a-real-token"
URL = "http://127.0.0.1:8788"
LINE_RE = re.compile(r"^(Mini PC|Mac|Windows PC) · (today \d\d:\d\d|yesterday \d\d:\d\d|"
                     r"[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}|date unknown) · .+$")
LIST_ROW_RE = re.compile(r"^result=ok rows=\d+ match=\d+ shown=\d+ filter=(all|done) "
                         r"device=(-|mac|minipc|windows) when=(-|today|yesterday|week)$")
NOW = datetime(2026, 11, 16, 10, 0, tzinfo=ZoneInfo("Europe/London"))  # the fixed "now"


@pytest.fixture(autouse=True)
def london(monkeypatch):
    """A fixed `now` (16 Nov 2026 10:00) in Europe/London; TZ restored after."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    monkeypatch.setattr(overlay, "_local_now", lambda: NOW)
    for store in (overlay._auto_tainted, overlay._tainted, overlay._sessions_list_flt, overlay._sessions_follow):
        store.clear()
    overlay.debug_log.clear()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()
    overlay._sessions_list_flt.clear()


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def row(sid, phase="done", state="idle", device="Mini PC", title="Tidy the build logs", doing=None,
        at=None, health="calm", **extra):
    return {"session_id": sid, "phase": phase, "state": state, "device": device, "title": title,
            "doing": title if doing is None else doing, "health": health,
            "last_activity_at": at or ago(hours=1), **extra}


def env(monkeypatch, tmp_path, rows, status=200):
    """A fake Companion gateway: every request lands in `seen`."""
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if status != 200:
            return httpx.Response(status, headers={"location": "http://elsewhere.invalid/"})
        return httpx.Response(200, json=rows)

    monkeypatch.setattr(overlay, "_sessions_transport", httpx.MockTransport(handler))
    monkeypatch.setenv("OVERLAY_COMPANION_URL", URL)
    token = tmp_path / "companion-token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    monkeypatch.setenv("OVERLAY_COMPANION_TOKEN_FILE", str(token))
    return seen


class Counter(FakeConversations):
    """A model pool that records every get (any call = the model was asked)."""

    def __init__(self, chunks=()):
        super().__init__(chunks)
        self.gets = 0

    def get(self, conv_id):
        self.gets += 1
        return super().get(conv_id)


def deps_for(clock=None, quiet=None, loud=None, pick=None):
    made: list = []
    deps = _deps(clock or Clock(), conversations=loud or Counter(lines({"say": "Model answer."})),
                 quiet_conversations=quiet or Counter(lines({"say": "Quiet answer."})))
    deps.agent_conversations = FakeAgentPool(lambda: made.append(1))
    deps.intent_pick = pick
    return deps, made


def pick_off():
    auto_policy.SETTINGS_FILE.write_text('{"agent_auto": false, "intent_pick": false}')


def picker(answer="agent", calls=None):
    def ask(msg):
        if calls is not None:
            calls.append(msg)
        return json.dumps({"pick": answer(msg) if callable(answer) else answer, "sub": "", "confidence": 0.95,
                           "slots": {}})
    return ask


def list_card(events):
    got = [c for c in _cards(events) if c["id"] == "sv-list"]
    return got[0] if got else None


def detail():
    return _steps("sessions")[-1]["detail"]


BOARD = ([row(f"d{i}", at=ago(minutes=30 + i)) for i in range(18)]  # done/idle
         + [row(f"e{i}", state="done", at=ago(hours=2 + i)) for i in range(5)]  # done/done
         + [row("r1", phase="in_review", state="done", at=ago(minutes=5))]  # in review, state done
         + [row(f"p{i}", phase="in_progress", state="running", at=ago(minutes=i)) for i in range(3)]
         + [row(f"w{i}", phase="in_review", state="needs_input", device="Mac", at=ago(minutes=9 + i))
            for i in range(2)]
         + [row("a1", phase="in_progress", state="running", health="attention", device="Windows PC",
                at=ago(minutes=7))])


# --- criterion 5-7: one source, the board's counts --------------------------------------------

def test_counts_match_the_board(client, monkeypatch, tmp_path):
    rows = list(reversed(sorted(BOARD, key=lambda r: r["last_activity_at"], reverse=True)))  # oldest first
    assert len(rows) == 30
    env(monkeypatch, tmp_path, rows)
    # (i) an empty quiet reply forces the fixed fallback line, counted over all 30
    deps, _ = deps_for(quiet=Counter([]))
    events = turn(client, "how many sessions are running", conv_id="c5a")
    assert says(events)[-1] == "30 sessions, sir: 2 waiting on you, 4 running."
    assert _steps("sessions")[0]["detail"] == "result=ok rows=30"
    assert detail().startswith("summary=host reason=")
    # (ii) the quiet prompt's untrusted block opens with the fixed totals line, then 20 rows
    overlay.debug_log.clear()
    deps, _ = deps_for()
    turn(client, "how many sessions are running", conv_id="c5b")
    prompt = deps.quiet_conversations.sessions["c5b"].prompts[-1]
    note = prompt[prompt.index("Totals from the Companion app"):]
    first = note.splitlines()[0]
    done = sum(1 for r in rows if r["phase"] == "done")
    assert done == 23  # done/idle + done/done; in_review/done is not done
    assert first == ("Totals from the Companion app: 30 sessions; 3 in progress, 3 in review, 1 attention, "
                     "23 done; 27 on the Mini PC, 2 on the Mac, 1 on the Windows PC.")
    assert sum(1 for ln in note.splitlines() if ln.startswith("- device ")) == 20
    assert _steps("sessions")[-1]["detail"] == "result=ok rows=30"
    # the list path's done count is the same phase rule
    overlay.debug_log.clear()
    turn(client, "list my done sessions", conv_id="c5c")
    assert " match=23 " in detail()


def test_scan_keeps_newest(client, monkeypatch, tmp_path):
    old = [row(f"o{i}", phase="in_progress", state="running", at=ago(days=30, minutes=i)) for i in range(2097)]
    newest = [row(f"n{i}", title=f"Newest job {i}", at=ago(minutes=3 - i)) for i in range(3)]
    env(monkeypatch, tmp_path, old[::-1] + newest)  # oldest first, the newest three at the end
    deps_for()
    events = turn(client, "list my done sessions", conv_id="c6")
    body = list_card(events)["body"]
    assert [ln.rsplit(" · ", 1)[1] for ln in body[:3]] == ["Newest job 2", "Newest job 1", "Newest job 0"]
    assert detail().startswith("result=ok rows=2000 match=3 shown=3 ")


def test_endpoint_is_the_app_route(client, monkeypatch, tmp_path):
    seen = env(monkeypatch, tmp_path, BOARD)
    deps_for()
    turn(client, "list my done sessions", conv_id="c7")
    assert seen == [("GET", "/central/sessions", f"Bearer {TOKEN}")]
    turn(client, "how many sessions are running", conv_id="c7b")
    assert len(seen) == 2 and all(s[:2] == ("GET", "/central/sessions") for s in seen)
    # a redirect is never followed
    seen = env(monkeypatch, tmp_path, BOARD, status=302)
    overlay.debug_log.clear()
    events = turn(client, "list my done sessions", conv_id="c7c")
    assert len(seen) == 1 and detail() == "result=error status=302"
    assert says(events)[-1] == overlay.acks.PHRASES["no_sessions_access"]


# --- criterion 8-14: the done list ------------------------------------------------------------

def test_done_list_card(client, monkeypatch, tmp_path):
    long_doing = "Rewrote the parser tests " + "and checked every edge case again " * 3
    rows = [row("m1", title="Tidy the build logs", at=ago(minutes=10)),
            row("m2", title="Sort the photo folder", doing=long_doing[:120], at=ago(days=1)),
            row("k1", device="Mac", title="Rename the screenshots", at=ago(days=3)),
            row("x1", device="Windows PC", title="Check the printer queue", at=ago(hours=2)),
            row("run", phase="in_progress", state="running", title="Still running here")]
    env(monkeypatch, tmp_path, rows)
    pick_off()
    deps, made = deps_for()
    events = turn(client, "which router sessions are done", conv_id="c8")
    assert _steps("intent")[-1]["kind"] == "sessions"
    assert says(events)[-1] == ("4 done sessions on the app, sir: 2 on the Mini PC, 1 on the Mac, "
                                "1 on the Windows PC. They're on the card.")
    card = list_card(events)
    assert card["title"] == "Done sessions · 4" and card["type"] == "info" and len(card["body"]) == 4
    assert all(LINE_RE.fullmatch(ln) for ln in card["body"]), card["body"]
    assert card["body"][0] == "Mini PC · today 09:50 · Tidy the build logs"
    assert card["body"][1].startswith("Windows PC · today 08:00 · ")
    assert card["body"][2].startswith("Mini PC · yesterday 10:00 · ")
    assert card["body"][3] == "Mac · Fri 13 Nov · Rename the screenshots"
    assert not any("Still running" in ln for ln in card["body"])
    cut = card["body"][2].split(" · ", 2)[2]
    assert cut.endswith("…") and len(card["body"][2]) <= 200 and long_doing.startswith(cut[:-1])
    assert len([c for c in _cards(events) if c["id"] == "sv-list"]) == 1
    assert LIST_ROW_RE.fullmatch(detail()) and detail().endswith("match=4 shown=4 filter=done device=- when=-")


FILTERS = [
    ("what are my done sessions on the router", (True, "", "")),
    ("list my done sessions on the Mac", (True, "mac", "")),
    ("finished sessions on the mini PC", (True, "minipc", "")),
    ("finished sessions on the many piece", (True, "minipc", "")),
    ("what did my sessions do today", (False, "", "today")),
    ("check the active sessions on the mini PC", (False, "minipc", "")),
    ("the ones that ended yesterday", (True, "", "yesterday")),
    ("list my sessions from this week", (False, "", "week")),
    ("sessions on the Jarvis Companion app", (False, "", "")),
    ("list my router sessions", (False, "", "")),
    ("those ones on the mini PC", (False, "minipc", "")),
]


@pytest.mark.parametrize("text,want", FILTERS)
def test_filters_from_words(client, monkeypatch, tmp_path, text, want):
    assert intents.sessions_filter(text) == intents.SessionsFilter(*want)
    rows = [row("t1", at=ago(hours=1)), row("t2", device="Mac", at=ago(days=1, hours=1)),
            row("t3", phase="in_progress", state="running", at=ago(days=1)),
            row("t4", device="Mac", at=ago(days=5)), row("t5", at=ago(days=9)),
            row("t6", device="Mac", phase="in_review", state="needs_input", at=ago(minutes=2))]
    env(monkeypatch, tmp_path, rows)
    pick_off()
    deps_for()
    if text.startswith(("the ones", "those ones")):  # an anaphor: only as a follow-up
        turn(client, "how many sessions are running", conv_id="f9")
        overlay.debug_log.clear()
        events = turn(client, text, conv_id="f9")
    else:
        events = turn(client, text, conv_id="f9")
    assert _steps("intent")[-1]["kind"] == "sessions"
    done, device, when = want
    if not (done or when) and not text.startswith("those ones"):
        assert detail().startswith("result=ok rows=6") and "match=" not in detail()  # summary path
        return
    now = NOW
    expect = [r for r in rows if (not done or r["phase"] == "done")
              and (not device or overlay._device_label(r["device"]).lower().replace(" ", "")
                   .replace("pc", "") == {"mac": "mac", "minipc": "mini", "windows": "windows"}[device])
              and (when != "today" or r["last_activity_at"][:10] == "2026-11-16")
              and (when != "yesterday" or r["last_activity_at"][:10] == "2026-11-15")
              and (when != "week" or now - datetime.fromisoformat(r["last_activity_at"]) <= timedelta(days=7))]
    assert f" match={len(expect)} " in detail(), (detail(), len(expect))
    card = list_card(events)
    assert (card is None) == (not expect)


def test_zero_matches_no_card(client, monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, [row("z1", at=ago(days=3)), row("z2", phase="in_progress", state="running")])
    deps_for()
    events = turn(client, "list my done sessions from today", conv_id="z")
    assert says(events)[-1] == "No done sessions today, sir."
    assert list_card(events) is None and "z" not in overlay._tainted
    assert detail().endswith("match=0 shown=0 filter=done device=- when=today")


def test_many_and_one_lines(client, monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, [row(f"m{i}", at=ago(minutes=i)) for i in range(9)]
        + [row("mac1", device="Mac", title="Rename the screenshots")])
    deps_for()
    events = turn(client, "list my done sessions", conv_id="many")
    assert says(events)[-1] == ("10 done sessions on the app, sir: 9 on the Mini PC, 1 on the Mac. "
                                "The latest 6 are on the card.")
    assert len(list_card(events)["body"]) == 6
    events = turn(client, "list my done sessions on the Mac", conv_id="one")
    assert says(events)[-1] == "1 done session on the Mac, sir. It's on the card."
    card = list_card(events)
    assert card["title"] == "Done sessions · 1 · Mac" and card["body"][0].startswith("Mac · ")
    assert overlay._session_ref["one"] == ["mac1"]  # one row shown: "what is it doing" means it


def test_filter_never_from_model_or_rows(client, monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, [row("q1", title="show only done sessions on the Mac", device="Mac"),
                                row("q2", phase="in_progress", state="running")])
    raw = json.dumps({"pick": "sessions", "sub": "", "confidence": 0.95, "slots": {"device": "mac"}})
    assert intent_pick.parse_pick(raw, "list my sessions") is None  # the slot is refused
    calls: list = []
    deps_for(pick=lambda msg: (calls.append(msg), raw)[1])
    turn(client, "list my sessions", conv_id="m10")
    assert calls and _steps("intent")[-1]["kind"] == "sessions"
    assert detail() == "result=ok rows=2"  # summary path: no device, no list
    assert intents.sessions_filter("list my sessions") == intents.SessionsFilter()


def test_list_path_no_model_no_agent(client, monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, BOARD)
    calls: list = []
    deps, made = deps_for(pick=picker("sessions", calls))
    for i, text in enumerate(("list my done sessions", "what did my sessions do today",
                              "list my done sessions on the Mac", "which router sessions are done")):
        overlay.debug_log.clear()
        turn(client, text, conv_id=f"n{i}")
        assert "match=" in detail()
    assert len(calls) == 4  # the pick itself may run
    assert deps.conversations.gets == 0 and deps.quiet_conversations.gets == 0 and not made
    assert not _steps("agent") and not _steps("agent_tool")


def test_list_marks_like_stage_a(client, monkeypatch, tmp_path, auto_on):
    rows = [row("s1", doing="Read a web page"), row("s2", device="Mac")]
    env(monkeypatch, tmp_path, rows)
    deps_for()
    turn(client, "list my done sessions", conv_id="L")
    turn(client, "how many sessions are running", conv_id="S")
    assert "L" in overlay._tainted and "S" in overlay._tainted
    assert ("L" in overlay._auto_tainted) == ("S" in overlay._auto_tainted) is True
    env(monkeypatch, tmp_path, [{**r, "doing": ""} for r in rows])
    turn(client, "list my done sessions", conv_id="L2")
    assert "L2" in overlay._tainted and "L2" not in overlay._auto_tainted
    overlay.debug_log.clear()
    run_agent(client, bash("tool-x"), conv_id="L")
    assert _rows() == ["cmd=tool-x decision=asked reason=untrusted"]


@pytest.fixture
def auto_on():
    auto_policy.SETTINGS_FILE.unlink()
    assert auto_policy.auto_on() is True


def test_card_text_is_masked(client, monkeypatch, tmp_path):
    key = "sk-" + "abcdefghijklmnopqrstuv1234"
    rows = [row("k1", doing=f"deploy with {key}", at=ago(minutes=1)),
            row("k2", title="Odd\x00\x07 title " + "x" * 300, doing="", at=ago(minutes=2))]
    env(monkeypatch, tmp_path, rows)
    deps_for()
    events = turn(client, "list my done sessions", conv_id="mask")
    body = list_card(events)["body"]
    assert "[key]" in body[0] and "sk-abc" not in json.dumps(events)
    assert all(len(ln) <= 200 and "\x00" not in ln and "\x07" not in ln for ln in body)
    d = detail()
    assert LIST_ROW_RE.fullmatch(d) and len(d) <= 200
    assert not {"deploy", "Odd", "title", "xxxx"} & set(d.split())


def test_token_never_leaks(client, monkeypatch, tmp_path, caplog):
    caplog.set_level("DEBUG")
    deps_for()
    out = []
    env(monkeypatch, tmp_path, BOARD)
    out += turn(client, "list my done sessions", conv_id="tok")
    env(monkeypatch, tmp_path, BOARD, status=302)
    out += turn(client, "list my done sessions", conv_id="tok2")

    def slow(request):
        raise httpx.ConnectTimeout("timed out")
    monkeypatch.setattr(overlay, "_sessions_transport", httpx.MockTransport(slow))
    out += turn(client, "list my done sessions", conv_id="tok3")
    assert detail() == "result=error status=0"
    for blob in (json.dumps(out), json.dumps(overlay.debug_log.snapshot()), caplog.text,
                 json.dumps(overlay._history, default=str)):
        assert TOKEN not in blob


# --- criterion 15: the name callers keep 20 rows ----------------------------------------------

def test_other_callers_unchanged(monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, [{k: v for k, v in r.items() if k != "phase"} for r in BOARD])
    old_rows, _ = overlay._fetch_sessions(URL, TOKEN)
    # rows without a phase: today's state rule (done/stopped/failed last), newest first, 20
    want = sorted((r for r in BOARD), key=lambda r: (r["state"] not in ("done", "stopped", "failed"),
                                                     r["last_activity_at"]), reverse=True)[:20]
    assert [r["id"] for r in old_rows] == [r["session_id"] for r in want]
    # with a phase: not-DONE first, so a done/idle row never displaces a running one
    env(monkeypatch, tmp_path, BOARD)
    new_rows, _ = overlay._fetch_sessions(URL, TOKEN)
    assert len(new_rows) == 20
    open_ids = {r["session_id"] for r in BOARD if r["phase"] != "done"}
    assert {r["id"] for r in new_rows[:len(open_ids)]} == open_ids
    every, _ = overlay._fetch_sessions(URL, TOKEN, None)
    assert len(every) == 30
    base = subprocess.run(["git", "merge-base", "HEAD", "origin/ci-ipa"], cwd=ROOT, capture_output=True, text=True)
    if base.returncode == 0:
        frozen = [f"tests/test_overlay_jfollow{n}.py" for n in (4, 5, 6, 7)]
        diff = subprocess.run(["git", "diff", base.stdout.strip(), "--", *frozen], cwd=ROOT,
                              capture_output=True, text=True)
        assert diff.stdout == ""


def test_stop_by_name_still_confirms(client, monkeypatch, tmp_path):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json=BOARD + [row("b1", phase="in_progress", state="running",
                                                     title="build runner")])
    monkeypatch.setattr(overlay, "_sessions_transport", httpx.MockTransport(handler))
    env_token = tmp_path / "companion-token"
    env_token.write_text(TOKEN)
    env_token.chmod(0o600)
    monkeypatch.setenv("OVERLAY_COMPANION_URL", URL)
    monkeypatch.setenv("OVERLAY_COMPANION_TOKEN_FILE", str(env_token))
    deps_for()
    events = turn(client, "stop the build runner session", conv_id="stop")
    assert says(events)[-1] == "Stop the build runner session?" and not posts
    turn(client, "yes", conv_id="stop")
    assert len(posts) == 1


# --- criterion 16-19: routing -----------------------------------------------------------------

ROUTES = ["what are my companion sessions doing right now", "which router sessions are done",
          "what did my sessions do today", "list my done sessions on the Mac",
          "show me the sessions on the Jarvis Companion app", "list my sessions that ended yesterday",
          "finished sessions on the mini PC", "show me my done sessions on the Windows PC",
          "which sessions are done on the Windows PC"]
# Generated on BASE (e7aad9a) with intents.parse; these keep their kinds.
KEEP = {"start a session to tidy the logs": "session", "stop the session on the Mac": "session",
        "open sessions on the Mac": "open_app", "open session on the mini PC": "session", "open sessions": "open_app",
        "end all sessions": "answer", "how do I check sessions": "answer", "how long are sessions": "answer"}


@pytest.mark.parametrize("text", ROUTES)
def test_word_routes(client, monkeypatch, tmp_path, text):
    assert intents.parse(text, apps={}).kind == "sessions"
    env(monkeypatch, tmp_path, BOARD)
    pick_off()
    deps_for(pick=picker("answer"))
    turn(client, text, conv_id="w")
    assert _steps("intent")[-1]["kind"] == "sessions"
    assert _steps("intent_pick")[-1]["detail"].endswith("reason=off")


@pytest.mark.parametrize("text,kind", sorted(KEEP.items()))
def test_word_routes_keep(text, kind):
    assert intents.parse(text, apps={}).kind == kind


def _owner_conv(client, monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, BOARD)
    clock = Clock()
    deps, made = deps_for(clock, pick=picker("agent"))  # the model answers agent on every turn
    return deps, made, clock


def test_owner_rows_replay(client, monkeypatch, tmp_path):
    deps, made, clock = _owner_conv(client, monkeypatch, tmp_path)
    turn(client, "how many sessions are running", conv_id="o")
    assert _steps("intent")[-1]["kind"] == "sessions"  # a word sessions hit beats the model's agent
    assert re.match(r"pick=agent conf=\d+ path=model ", _steps("intent_pick")[-1]["detail"])
    clock.now += 10
    overlay.debug_log.clear()
    turn(client, "How many sessions did you say", conv_id="o")
    assert _steps("intent")[-1]["kind"] == "sessions" and _steps("sessions")[0]["detail"] == "result=ok rows=30"
    clock.now += 10
    overlay.debug_log.clear()
    turn(client, "Okay, can you tell me the ones that run this week", conv_id="o")
    assert _steps("intent")[-1]["kind"] == "sessions"
    assert detail().endswith("filter=all device=- when=week")
    assert not made and not _steps("agent")
    # (d) no anaphor and no "sessions": the model's agent pick stands (recorded in the report)
    clock.now += 10
    overlay.debug_log.clear()
    turn(client, "Okay, if you filter out anything that says unowned", conv_id="o")
    assert _steps("intent")[-1]["kind"] == "agent"


def test_owner_follow_expires(client, monkeypatch, tmp_path):
    deps, made, clock = _owner_conv(client, monkeypatch, tmp_path)
    turn(client, "how many sessions are running", conv_id="o2")
    clock.now += 31
    overlay.debug_log.clear()
    turn(client, "Okay, can you tell me the ones that run this week", conv_id="o2")
    assert _steps("intent")[-1]["kind"] == "agent"


def test_followup_carries_done(client, monkeypatch, tmp_path):
    """O2: "which ones were on the Mac" right after a done list keeps done."""
    deps, made, clock = _owner_conv(client, monkeypatch, tmp_path)
    env(monkeypatch, tmp_path, BOARD + [row("macdone", device="Mac", title="Rename the screenshots")])
    turn(client, "which router sessions are done", conv_id="o3")
    clock.now += 5
    overlay.debug_log.clear()
    events = turn(client, "which ones were on the Mac", conv_id="o3")
    assert _steps("intent")[-1]["kind"] == "sessions"
    assert detail().endswith("filter=done device=mac when=-")
    assert all(ln.startswith("Mac · ") for ln in list_card(events)["body"])


@pytest.mark.parametrize("text,kind", [
    ("go and check the repository for session code", "answer"), ("what's the weather today", "answer"),
    ("take a screenshot on the Mac", "answer"), ("is it done yet", "answer"), ("finished, thanks", "answer")])
def test_s2_limits_negatives(client, monkeypatch, tmp_path, text, kind):
    env(monkeypatch, tmp_path, BOARD)
    clock = Clock()
    deps_for(clock, pick=picker("answer"))
    turn(client, "how many sessions are running", conv_id="neg")
    clock.now += 3
    overlay.debug_log.clear()
    turn(client, text, conv_id="neg")
    assert _steps("intent")[-1]["kind"] == kind


def test_s2_limits_agent(client, monkeypatch, tmp_path):
    env(monkeypatch, tmp_path, BOARD)
    clock = Clock()
    deps_for(clock, pick=picker(lambda msg: "sessions" if "are running" in msg else "agent"))
    # a word-rule agent hit keeps agent
    word_agent = "search my notes for the sessions from this week"
    assert intents.parse(word_agent, apps={}).kind == "agent"
    turn(client, "how many sessions are running", conv_id="ag")
    clock.now += 3
    overlay.debug_log.clear()
    turn(client, word_agent, conv_id="ag")
    assert _steps("intent")[-1]["kind"] == "agent"
    # a model agent pick with a start/stop verb stays agent
    turn(client, "how many sessions are running", conv_id="ag2")
    clock.now += 3
    overlay.debug_log.clear()
    turn(client, "stop the ones that ran today", conv_id="ag2")
    assert not intents.sessions_followup("stop the ones that ran today")
    assert _steps("intent")[-1]["kind"] != "sessions"


def test_pick_prompt_sessions_line():
    line = next(ln for ln in intent_pick.PICK_PROMPT.splitlines() if ln.startswith("- sessions:"))
    for word in ("router sessions", "done", "Companion app"):
        assert word in line
    assert intent_pick.KIND_TO_PICK["sessions"] == "sessions"
    assert intent_pick.PICKS["sessions"] == (frozenset(), frozenset())


def test_s2_gated_and_barge_never_switch(client, monkeypatch, tmp_path):
    """Room talk failing the follow-up gate, and a barge-in, are dropped/kept as before: no list."""
    env(monkeypatch, tmp_path, BOARD)
    clock = Clock()
    deps_for(clock, pick=picker("agent"))
    turn(client, "how many sessions are running", conv_id="g")
    clock.now += 3
    overlay.debug_log.clear()
    turn(client, "which ones were on the Mac", conv_id="g", followup=True, audio_ms=3000, levels=[0.002] * 12)
    assert not [s for s in _steps("intent") if s.get("kind") == "sessions"] and not _steps("sessions")
    turn(client, "how many sessions are running", conv_id="b")
    clock.now += 3
    overlay.debug_log.clear()
    turn(client, "which ones were on the Mac", conv_id="b", barge_in=True)
    assert _steps("intent_pick")[-1]["detail"].endswith("reason=barge")
    assert not [s for s in _steps("sessions") if "match=" in s["detail"]]
