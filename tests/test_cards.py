"""Session cards: jarvis-card events, files, answers, receipts, delegation, live plain lines.

Reuses test_central's stub router (autouse fixture). conftest points the cards
store, card files and MINIPC_ROUTER_DB at tmp_path; tests that need the router's
sqlite build a tiny one with the same columns the gateway reads.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import pytest

from gateway import cards, central
from gateway.stubs import router_stub
from test_central import IOS, TOKEN, _call, stub_router  # noqa: F401  (autouse fixture)

REAL_GENERATE = central._generate_plain  # captured before conftest stubs it

SHARED = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


def _iso(ts):
    return ts.isoformat().replace("+00:00", "Z")


@pytest.fixture
def router_db(tmp_path, monkeypatch):
    """A router.db with sessions(id, claude_session_uuid, created_at) + events."""
    path = tmp_path / "router.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT, claude_session_uuid TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE events (session_id TEXT, seq INTEGER, type TEXT, text TEXT, prompt TEXT, "
                 "options TEXT, ts TEXT, PRIMARY KEY (session_id, seq))")
    for sid in ("s-run", "s-ask", "s-done", "s-child"):
        conn.execute("INSERT INTO sessions VALUES (?,?,?)", (sid, f"uuid-{sid}", "2026-10-21T00:00:00Z"))
    conn.commit()
    monkeypatch.setenv("MINIPC_ROUTER_DB", str(path))

    def add(sid, seq, etype, text, ts):
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (sid, seq, etype, text, None, None, _iso(ts)))
        conn.commit()
    yield add
    conn.close()


def _event(body, headers=SHARED):
    return _call("POST", "/cards/event", headers=headers, json=body)


def _sessions():
    resp = _call("GET", "/central/sessions", headers=IOS)
    assert resp.status_code == 200, resp.text
    return {s["session_id"]: s for s in resp.json()}


# --- auth ----------------------------------------------------------------------

def test_session_routes_take_the_shared_token_only():
    assert _event({"session": "s-run", "action": "doing", "text": "x"}, headers={}).status_code == 401
    assert _event({"session": "s-run", "action": "doing", "text": "x"}, headers=IOS).status_code == 401
    assert _call("POST", "/cards/file?session=s-run&name=a.md", content=b"x").status_code == 401


def test_phone_routes_take_the_ios_token_only():
    body = {"msg_id": "m-1", "need_id": "a" * 12, "answer": "Approve"}
    assert _call("POST", "/central/cards/s-run/answer", headers=SHARED, json=body).status_code == 401
    assert _call("GET", f"/central/cards/s-run/files/{'a' * 32}", headers=SHARED).status_code == 401


# --- session resolution --------------------------------------------------------

def test_claude_uuid_resolves_to_router_id(router_db):
    resp = _event({"session": "uuid-s-run", "action": "doing", "text": "Building the card layout"})
    assert resp.json()["session_id"] == "s-run"
    assert _event({"session": "nope", "action": "doing", "text": "x"}).status_code == 404


# --- live doing line -----------------------------------------------------------

def test_declared_doing_beats_the_model_while_working():
    _event({"session": "s-run", "action": "doing", "text": "Building the card layout"})
    data = _sessions()
    assert data["s-run"]["doing"] == "Building the card layout"
    assert data["s-run"]["card"]["doing"] == "Building the card layout"


def test_declared_doing_expires(monkeypatch):
    _event({"session": "s-run", "action": "doing", "text": "Old line"})
    monkeypatch.setattr(cards, "_now", lambda: datetime.now(timezone.utc) + timedelta(minutes=20))
    assert _sessions()["s-run"]["card"]["doing"] is None


# --- asks + buttons ------------------------------------------------------------

def test_approve_ask_moves_idle_session_to_review_and_tapping_sends_one_message(monkeypatch):
    heard = []
    monkeypatch.setattr(cards, "NEED_LISTENERS", [lambda sid, need: heard.append((sid, need["kind"]))])
    resp = _event({"session": "s-done", "action": "ask", "kind": "approve", "text": "Plan for cards is ready"})
    need_id = resp.json()["need_id"]
    assert heard == [("s-done", "approve")]

    s = _sessions()["s-done"]
    assert s["phase"] == "in_review" and s["health"] == "waiting"
    assert s["recap"][-1] == "Needs you: Plan for cards is ready"
    assert s["card"]["need"]["id"] == need_id and s["card"]["need"]["answered"] is None

    router_stub.seen.clear()
    ok = _call("POST", "/central/cards/s-done/answer", headers=IOS,
               json={"msg_id": "m-ok", "need_id": need_id, "answer": "Approve"})
    assert ok.status_code == 200, ok.text
    sent = [r for r in router_stub.seen if r["path"] == "/sessions/s-done/message"]
    assert len(sent) == 1
    assert sent[0]["body"]["text"] == 'Owner tapped "Approve" on your card ask: Plan for cards is ready'

    s = _sessions()["s-done"]
    assert s["phase"] == "done"  # back where the router puts it
    assert s["card"]["need"]["answered"]["answer"] == "Approve"
    assert s["card"]["receipt"]["text"] == "Approve"
    again = _call("POST", "/central/cards/s-done/answer", headers=IOS,
                  json={"msg_id": "m-ok2", "need_id": need_id, "answer": "Approve"})
    assert again.status_code == 409


def test_revise_in_chat_settles_the_ask():
    need_id = _event({"session": "s-done", "action": "ask", "kind": "approve", "text": "Plan"}).json()["need_id"]
    _call("POST", "/central/sessions/s-done/message", headers=IOS, json={"msg_id": "m-r", "text": "Revise: drop step 3"})
    need = _sessions()["s-done"]["card"]["need"]
    assert need["id"] == need_id and need["answered"]["answer"] == "reply"


def test_choice_needs_options_and_only_its_buttons_answer():
    assert _event({"session": "s-done", "action": "ask", "kind": "choice", "text": "Voice?",
                   "options": ["Daniel"]}).status_code == 422
    need_id = _event({"session": "s-done", "action": "ask", "kind": "choice", "text": "Voice?",
                      "options": ["Daniel", "George"]}).json()["need_id"]
    bad = _call("POST", "/central/cards/s-done/answer", headers=IOS,
                json={"msg_id": "m-1", "need_id": need_id, "answer": "Approve"})
    assert bad.status_code == 422
    good = _call("POST", "/central/cards/s-done/answer", headers=IOS,
                 json={"msg_id": "m-2", "need_id": need_id, "answer": "George"})
    assert good.status_code == 200


def test_checklist_answer_lists_each_item():
    need_id = _event({"session": "s-done", "action": "ask", "kind": "checklist", "text": "Test on your phone",
                      "items": ["Open the board", "Tap Approve"]}).json()["need_id"]
    router_stub.seen.clear()
    _call("POST", "/central/cards/s-done/answer", headers=IOS,
          json={"msg_id": "m-c", "need_id": need_id, "answer": "Send results", "checked": [True, False],
                "note": "Approve did nothing"})
    text = [r for r in router_stub.seen if r["path"].endswith("/message")][0]["body"]["text"]
    assert text == ("Owner checklist results for: Test on your phone\n[x] Open the board\n[ ] Tap Approve\n"
                    "Owner note: Approve did nothing")


def test_unknown_kind_and_stale_need_id_refused():
    assert _event({"session": "s-done", "action": "ask", "kind": "maybe", "text": "x"}).status_code == 422
    resp = _call("POST", "/central/cards/s-done/answer", headers=IOS,
                 json={"msg_id": "m-1", "need_id": "b" * 12, "answer": "Approve"})
    assert resp.status_code == 404


def test_clear_removes_the_ask():
    _event({"session": "s-done", "action": "ask", "kind": "yesno", "text": "Restart?"})
    _event({"session": "s-done", "action": "clear"})
    assert _sessions()["s-done"]["card"]["need"] is None


# --- files + links -------------------------------------------------------------

def test_markdown_file_round_trip_private_on_disk(tmp_path):
    resp = _call("POST", "/cards/file?session=s-done&name=plan.md&title=The%20plan", headers=SHARED,
                 content=b"# Plan\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert resp.status_code == 200, resp.text
    fid = resp.json()["file_id"]
    row = _sessions()["s-done"]["card"]["files"][0]
    assert row["id"] == fid and row["view"] == "markdown" and row["title"] == "The plan"
    got = _call("GET", f"/central/cards/s-done/files/{fid}", headers=IOS)
    assert got.status_code == 200 and got.text.startswith("# Plan")
    assert got.headers["content-type"].startswith("text/markdown")
    path = tmp_path / "card_files" / "s-done" / f"{fid}.md"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # another session's id never serves it
    assert _call("GET", f"/central/cards/s-run/files/{fid}", headers=IOS).status_code == 404


def test_file_type_and_size_limits(monkeypatch):
    assert _call("POST", "/cards/file?session=s-done&name=x.exe", headers=SHARED,
                 content=b"MZ").status_code == 415
    monkeypatch.setattr(cards, "MAX_FILE_BYTES", 10)
    assert _call("POST", "/cards/file?session=s-done&name=x.md", headers=SHARED,
                 content=b"x" * 11).status_code == 413


def test_ask_with_file_must_be_on_this_card():
    fid = _call("POST", "/cards/file?session=s-done&name=plan.md", headers=SHARED, content=b"# P").json()["file_id"]
    assert _event({"session": "s-run", "action": "ask", "kind": "approve", "text": "x",
                   "file_id": fid}).status_code == 404
    assert _event({"session": "s-done", "action": "ask", "kind": "approve", "text": "x",
                   "file_id": fid}).status_code == 200


def test_oldest_file_dropped_past_the_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(cards, "MAX_FILES", 2)
    ids = [_call("POST", f"/cards/file?session=s-done&name={i}.md", headers=SHARED, content=b"x").json()["file_id"]
           for i in range(3)]
    assert [f["id"] for f in _sessions()["s-done"]["card"]["files"]] == ids[1:]
    assert not (tmp_path / "card_files" / "s-done" / f"{ids[0]}.md").exists()


def test_same_name_file_replaces_the_old_one(tmp_path):
    first = _call("POST", "/cards/file?session=s-done&name=plan.md", headers=SHARED, content=b"v1").json()["file_id"]
    second = _call("POST", "/cards/file?session=s-done&name=plan.md", headers=SHARED, content=b"v2").json()["file_id"]
    assert [f["id"] for f in _sessions()["s-done"]["card"]["files"]] == [second]
    assert not (tmp_path / "card_files" / "s-done" / f"{first}.md").exists()


def test_card_question_replaces_router_prompt_line():
    _event({"session": "s-ask", "action": "ask", "kind": "yesno", "text": "Deploy to prod now?"})
    recap = _sessions()["s-ask"]["recap"]
    assert recap == ["Needs you: Deploy to prod now?"]


def test_links_must_be_https():
    assert _event({"session": "s-done", "action": "link", "url": "http://x.test/a"}).status_code == 422
    _event({"session": "s-done", "action": "link", "url": "https://claude.ai/artifact/abc", "text": "Before/after"})
    assert _sessions()["s-done"]["card"]["links"][0]["title"] == "Before/after"


# --- delegation ----------------------------------------------------------------

def test_declared_and_router_children_show_both_ways(monkeypatch):
    rows = [dict(r) for r in router_stub.SESSIONS] + [
        {"session_id": "s-child", "title": "Notifications", "state": "running", "phase": "in_progress",
         "updated_at": "2026-10-21T10:00:00Z"},
        {"session_id": "s-kid2", "title": "Spike", "state": "running", "phase": "in_progress",
         "parent_session_id": "s-run", "updated_at": "2026-10-21T10:00:00Z"},
    ]
    monkeypatch.setattr(router_stub, "SESSIONS", rows)
    _event({"session": "s-run", "action": "delegate", "child": "s-child", "text": "handed notifications"})
    data = _sessions()
    kids = {c["session_id"]: c for c in data["s-run"]["card"]["children"]}
    assert kids["s-child"]["what"] == "handed notifications" and kids["s-child"]["on_board"]
    assert kids["s-kid2"]["declared"] is False
    assert data["s-child"]["card"]["parent"]["session_id"] == "s-run"
    assert data["s-kid2"]["card"]["parent"]["session_id"] == "s-run"
    assert _event({"session": "s-run", "action": "delegate", "child": "s-run"}).status_code == 404


# --- message receipt -----------------------------------------------------------

def test_receipt_received_then_answered(router_db, monkeypatch):
    _call("POST", "/central/sessions/s-done/message", headers=IOS, json={"msg_id": "m-1", "text": "Any news?"})
    receipt = _sessions()["s-done"]["card"]["receipt"]
    assert receipt["text"] == "Any news?" and receipt["status"] == "received"
    router_db("s-done", 1, "text", "Yes, the build is green.", datetime.now(timezone.utc) + timedelta(seconds=5))
    assert _sessions()["s-done"]["card"]["receipt"]["status"] == "answered"


def test_receipt_working_while_session_busy(monkeypatch):
    rows = [dict(r) for r in router_stub.SESSIONS]
    rows[0].update(health="working", last_activity_at=_iso(datetime.now(timezone.utc) + timedelta(seconds=3)))
    monkeypatch.setattr(router_stub, "SESSIONS", rows)
    _call("POST", "/central/sessions/s-run/message", headers=IOS, json={"msg_id": "m-1", "text": "Go"})
    assert _sessions()["s-run"]["card"]["receipt"]["status"] == "working"


def test_receipt_failed_when_router_down(monkeypatch):
    monkeypatch.setattr(router_stub, "fail_status", 500)
    resp = _call("POST", "/central/sessions/s-run/message", headers=IOS, json={"msg_id": "m-1", "text": "Go"})
    assert resp.status_code == 502
    monkeypatch.setattr(router_stub, "fail_status", None)
    assert _sessions()["s-run"]["card"]["receipt"]["status"] == "failed"


def test_new_chat_records_receipt_on_spawned_session():
    _call("POST", "/chat", headers=IOS, json={"thread_id": "app-1", "msg_id": "m-9", "text": "Build cards"})
    assert cards.load()["s-new"]["receipt"]["text"] == "Build cards"


# --- live plain lines from router events ---------------------------------------

def test_recent_events_skip_generic_status_and_read_whole_reply(router_db):
    t = datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc)
    router_db("s-run", 1, "status", "working", t)
    router_db("s-run", 2, "status", "Running cd ~/x && …", t)
    router_db("s-run", 3, "status", "mask CI green; committing live tests", t)
    router_db("s-run", 4, "text", "The header is fixed. Now I'll update REPORT.md.\n\n# Final report\n" + "x" * 500, t)
    router_db("s-run", 5, "status", "ledger: DONE", t)
    facts = cards.recent_events("s-run")
    assert facts["statuses"] == ["mask CI green; committing live tests"]
    assert facts["reply"].endswith("x" * 500) and facts["reply_seq"] == 4


def test_finished_session_gets_done_lines_not_now_lines(monkeypatch):
    sent = {}

    def fake(prompt, timeout_s):
        sent["prompt"] = prompt
        return ('{"doing": "Done: fixed the page header", "recap": ["I fixed the page header", '
                '"I am now updating the final report", "Now I will tell the owner", "I wrote the report"]}')

    monkeypatch.setattr(central, "_run_claude_plain", fake)
    out = REAL_GENERATE({"session_id": "s", "title": "Header", "phase": "done",
                         "_facts": {"statuses": ["old status"], "reply": "# Final report\nAll done", "reply_seq": 9}})
    assert out["recap"] == ["I fixed the page header", "I wrote the report"]
    assert "Stage: finished" in sent["prompt"] and "# Final report" in sent["prompt"]
    assert "old status" not in sent["prompt"]  # status lines only matter while working


def test_digest_changes_when_session_finishes_with_same_reply():
    facts = {"statuses": ["building"], "reply": "r", "reply_seq": 3}
    working = central._plain_digest({"phase": "in_progress", "_facts": facts})
    done = central._plain_digest({"phase": "done", "_facts": facts})
    assert working != done
    assert central._plain_digest({"phase": "done", "summary": "  "}) is None
