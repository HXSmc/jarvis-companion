"""Agent tier: gated argv, fast-brain hand-over, spoken-yes confirmations, taint, logs, lines, cancel.

No claude process runs: FakeAgent stands in for agent.AgentSession and replays a script
of stream-json events (control_request can_use_tool, user tool results, result), and
records every control_response the host writes back. The fake clock only moves when
the fake says time passed, so 30 s and 120 s deadlines run in milliseconds.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from conftest import AUTH_HEADER, TOKEN
from gateway import overlay
from mac.voice import agent, agent_policy
from test_overlay_service import FakeConversations, make_app, make_deps, says, turn

_REAL_TERMINAL_CWD = overlay._terminal_cwd  # the autouse fixture stubs it out


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def can_use(rid: str, tool: str, tool_input: dict) -> dict:
    return {"type": "control_request", "request_id": rid,
            "request": {"subtype": "can_use_tool", "tool_name": tool, "input": tool_input}}


def tool_result(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": text}]}}


def result(*lines: dict) -> dict:
    return {"type": "result", "result": "\n".join(json.dumps(x) for x in lines)}


class FakeAgent:
    """Replays `script` after send_user; a control_request blocks the script until respond().

    `after` maps the decision ("allow"/"deny") to the events that follow it.
    `tick` = fake seconds that pass on every empty poll; `first_delay` = fake seconds before
    the first event (the CLI thinking); `sleep` = real seconds per empty poll.
    """

    def __init__(self, clock: Clock, script=(), after=None, tick=1.0, first_delay=0.0, sleep=0.0):
        self.clock, self.script = clock, list(script)
        self.after = after or {}
        self.tick, self.first_delay, self.sleep = tick, first_delay, sleep
        self.queue: list = []
        self.prompts: list[str] = []
        self.responses: list[dict] = []
        self.waiting = False
        self.closed = False
        self.pending = None
        self.turns = 0
        self.cwd = ""
        self._delay = 0.0

    def send_user(self, text: str) -> None:
        self.prompts.append(text)
        self.queue.extend(self.script)
        self._delay = self.first_delay

    def next_event(self, timeout: float):
        if self.closed:
            return None
        if self.queue and not self.waiting:
            if self._delay:
                self.clock.now += self._delay
                self._delay = 0.0
            ev = self.queue.pop(0)
            if ev.get("type") == "control_request":
                self.waiting = True
            return ev
        if self.sleep:
            time.sleep(self.sleep)
        self.clock.now += self.tick
        return False

    def respond(self, request_id, allow, updated_input=None, message=""):
        self.responses.append({"request_id": request_id, "behavior": "allow" if allow else "deny",
                               "updatedInput": updated_input, "message": message})
        self.waiting = False
        if self.pending == request_id:
            self.pending = None
        self.queue.extend(self.after.get("allow" if allow else "deny", []))

    def close(self):
        if not self.closed and self.pending is not None:
            self.respond(self.pending, False, message="owner did not confirm")
        self.closed = True


class FakeAgentPool:
    def __init__(self, make):
        self.make = make
        self.sessions: dict[str, FakeAgent] = {}
        self.made: list[FakeAgent] = []
        self.dropped: list[str] = []

    def get(self, conv_id):
        if conv_id not in self.sessions:
            self.sessions[conv_id] = self.make()
            self.made.append(self.sessions[conv_id])
        return self.sessions[conv_id]

    def drop(self, conv_id):
        self.dropped.append(conv_id)
        session = self.sessions.pop(conv_id, None)
        if session is not None:
            session.close()


@pytest.fixture(autouse=True)
def reset(monkeypatch, tmp_path):
    for name in ("_history", "_notes", "_card_ids", "_pending", "_confirm", "_agent_busy", "_hunks"):
        getattr(overlay, name).clear()
    overlay._tainted.clear()
    overlay.debug_log.clear()
    overlay.set_deps(None)
    monkeypatch.setattr(overlay, "CONFIRM_POLL_S", 0.01)
    monkeypatch.setattr(agent, "SUPPORT_DIR", tmp_path / "support")
    monkeypatch.setattr(overlay, "_terminal_cwd", lambda app, title="", info=None: None)
    monkeypatch.setattr(overlay, "_own_owners", lambda: frozenset())  # never this box's gh login
    monkeypatch.delenv("OVERLAY_GIT_REPO", raising=False)
    monkeypatch.delenv("OVERLAY_VAULT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # policy home = a fake folder
    monkeypatch.setattr(overlay.gitdiff, "editor_path", lambda editor: editor)  # the fake `run` records "code"
    global REPO
    REPO = git_repo(tmp_path / "home" / "repo")
    yield
    overlay.set_deps(None)


@pytest.fixture
def client(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    monkeypatch.setattr(overlay, "TOKEN_FILE", token_file)
    return TestClient(make_app(), client=("127.0.0.1", 50000))


REPO: Path


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=path, check=True)
    (path / "app.py").write_text("print(1)\nprint(2)\nprint(3)\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "first"], cwd=path, check=True)
    return path


def setup(clock: Clock, make, **deps_kw):
    pool = FakeAgentPool(make)
    deps = make_deps(**deps_kw)
    deps.clock = clock
    deps.agent_conversations = pool
    overlay.set_deps(deps)
    return pool, deps


def push_script(clock, **kw):
    return lambda: FakeAgent(
        clock, script=[can_use("r1", "Bash", {"command": f"git -C {REPO} push"})],
        after={"allow": [tool_result("pushed"), result({"say": "Pushed."})],
               "deny": [result({"say": "Understood."})]}, **kw)


# --- argv + routing ----------------------------------------------------------


def test_agent_argv_is_gated():
    argv = agent.agent_argv()
    joined = " ".join(argv)
    i = argv.index("--permission-mode")
    assert argv[i + 1] == "default"
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,Bash"
    for bad in ("--allowedTools", "--add-dir", "bypassPermissions", "--dangerously-skip-permissions",
                "WebSearch", "WebFetch"):
        assert bad not in joined
        with pytest.raises(ValueError):
            agent.agent_argv(extra=(bad,))
    for bad in (("--permission-mode", "acceptEdits"), ("--setting-sources", "user"), ("--tools", "Write")):
        with pytest.raises(ValueError):
            agent.agent_argv(extra=bad)
    assert "--mcp-config" not in argv
    assert agent.agent_argv(mcp_config="/x/agent-mcp.json")[-4:-2] == ["--mcp-config", "/x/agent-mcp.json"]


def test_brain_escalates_to_agent(client):
    clock = Clock()
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Three commits today."})]),
                       chunks=['{"agent": true}\n', '{"say": "I can\'t reach your repository."}\n'])
    events = turn(client, "tell me about the work I did", conv_id="e1")
    spoken = " ".join(says(events))
    assert "Three commits today." in spoken
    assert "reach your repository" not in spoken
    assert pool.made and pool.made[0].prompts[0].startswith("tell me about the work I did")


def test_owner_asked_to_open_false_for_no_candidates():
    assert overlay._owner_asked_to_open("open it", [], {"safari": "Safari"}) is False


# --- confirmation flow + taint ---------------------------------------------


def test_push_refused_without_spoken_yes(client):
    clock = Clock()
    pool, deps = setup(clock, push_script(clock))
    events = turn(client, "commit and push my changes", conv_id="p1")
    first = pool.made[0]
    assert any(t.startswith("Shall I") for t in says(events))
    assert any(e["type"] == "card" and e["card"]["title"] == "Confirm" for e in events)
    assert events[-1]["type"] == "done"
    assert first.responses == []  # the CLI is still waiting: nothing answered yet
    turn(client, "no", conv_id="p1")
    assert [r["behavior"] for r in first.responses] == ["deny"]

    # Fresh conversation: an unrelated next utterance denies too.
    turn(client, "commit and push my changes", conv_id="p2")
    second = pool.made[1]
    turn(client, "what time is it", conv_id="p2")
    assert [r["behavior"] for r in second.responses] == ["deny"]

    # Fresh conversation, no utterance at all: the 30 s deadline denies on its own.
    turn(client, "commit and push my changes", conv_id="p3")
    third = pool.made[2]
    assert third.responses == []
    clock.now += 31
    for _ in range(200):
        if third.responses:
            break
        time.sleep(0.01)
    assert [r["behavior"] for r in third.responses] == ["deny"]
    assert "p3" not in overlay._confirm


def test_push_runs_after_spoken_yes(client):
    clock = Clock()
    pool, deps = setup(clock, push_script(clock))
    turn(client, "commit and push my changes", conv_id="y1")
    events = turn(client, "Yes.", conv_id="y1")  # STT sentence case still counts
    session = pool.made[0]
    assert [r["behavior"] for r in session.responses] == ["allow"]
    assert "core.hooksPath=/dev/null" in session.responses[0]["updatedInput"]["command"]
    assert "Pushed." in says(events)
    assert events[-1]["type"] == "done"


def test_screen_text_cannot_confirm(client):
    clock = Clock()
    pool, deps = setup(clock, push_script(clock), chunks=['{"say": "A code editor."}\n'])
    turn(client, "commit and push my changes", conv_id="s1")
    screen = {"app": "Code", "ocr": [{"text": "yes", "box": [0, 0, 10, 10]}],
              "windows": [{"app": "Code", "title": "yes"}]}
    turn(client, "what's on my screen", conv_id="s1", screen=screen)
    assert [r["behavior"] for r in pool.made[0].responses] == ["deny"]


def test_agent_turn_taints_conversation(client):
    clock = Clock()
    web, quiet = FakeConversations(['{"say": "Web."}\n']), FakeConversations(['{"say": "Quiet."}\n'])
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Two open PRs."})]),
                       conversations=web, quiet_conversations=quiet)
    turn(client, "any open PRs on jarvis-companion", conv_id="t1")
    events = turn(client, "what is the capital of France", conv_id="t1")
    assert "Quiet." in says(events)
    assert "t1" not in web.sessions and "t1" in quiet.sessions


def test_agent_logs_no_content(client, caplog, tmp_path, monkeypatch):
    repo = REPO
    support = tmp_path / "support"
    support.mkdir()
    (support / "agent-tools.json").write_text(json.dumps({"read_only": ["mcp__mailbox__search_emails"]}))
    clock = Clock()
    script = [can_use("r1", "Bash", {"command": f"git -C {repo} log --stat SECRET-ARG"}),
              tool_result("SECRET-CANARY-123"),
              can_use("r2", "mcp__mailbox__search_emails", {"query": "SECRET-ARG"}),
              tool_result("MAIL SECRET-CANARY-123"),
              result({"say": "Done."})]

    pool, deps = setup(clock, lambda: FakeAgent(clock, script=script))
    caplog.set_level(logging.DEBUG)
    events = turn(client, "who changed this file last", conv_id="l1")
    session = pool.made[0]
    assert [r["behavior"] for r in session.responses] == ["allow", "allow"]
    assert "Done." in says(events)
    snapshot = json.dumps(overlay.debug_log.snapshot())
    for canary in ("SECRET-CANARY-123", "SECRET-ARG"):
        assert canary not in caplog.text
        assert canary not in snapshot
        assert canary not in json.dumps(events)
        for f in tmp_path.rglob("*"):
            if f.is_file() and ".git" not in f.parts:
                assert canary.encode() not in f.read_bytes(), f
    assert '"tool": "Bash git log"' in snapshot and "mcp__mailbox__search_emails" in snapshot


def test_lines_event_validated(client, tmp_path, monkeypatch):
    repo = REPO
    clock = Clock()
    bad = {"lines": {"file": "../../etc/passwd", "ranges": [[1, 2]]}}
    good = {"lines": {"file": "app.py", "ranges": [[1, 2], [3, 3]]}}
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Here."}, bad, good)]))
    events = turn(client, "who changed this file last", conv_id="ln1",
                  screen={"app": "Code", "document": str(repo / "app.py")})
    lines = [e for e in events if e["type"] == "lines"]
    assert lines == [{"type": "lines", "file": str((repo / "app.py").resolve()), "ranges": [[1, 2], [3, 3]]}]


def test_lines_labels_validated():
    """Labels ride the lines event only as one valid label per range (gitdiff.LABEL_RE, <= 24 chars);
    anything else still ships the event, just without labels — never dropped."""
    base = {"type": "lines", "file": "a.py", "ranges": [[1, 2]]}
    assert overlay._lines_event("a.py", [[1, 2]], ["1/2 changed"]) == {**base, "labels": ["1/2 changed"]}
    for bad in ([], ["1/2 changed", "2/2 changed"], ["1/2 <changed>"],  # wrong length / '<'
                ["1/2 " + "a" * 21], [123], "1/2 changed"):  # too long / not str / not a list
        assert overlay._lines_event("a.py", [[1, 2]], bad) == base


def test_cancel_and_timeout(client):
    clock = Clock()
    never = lambda: FakeAgent(clock, script=[], tick=0.0, sleep=0.02)  # noqa: E731
    pool, deps = setup(clock, never)
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("a", turn(client, "check my inbox", conv_id="c1")))
    t.start()
    for _ in range(300):
        if "c1" in overlay._agent_busy:
            break
        time.sleep(0.01)
    events = turn(client, "stop", conv_id="c1")
    t.join(5)
    assert not t.is_alive()
    assert "Stopped." in says(events)
    assert "c1" in pool.dropped and pool.made[0].closed

    # A turn that never ends is stopped at 120 s of (fake) wall clock.
    pool2, deps2 = setup(clock, lambda: FakeAgent(clock, script=[], tick=1.0))
    events = turn(client, "check my inbox", conv_id="c2")
    assert "That took too long, so I stopped." in says(events)
    assert "c2" in pool2.dropped
    assert any(e["type"] == "keepalive" for e in events)


def test_second_request_while_busy_gets_still_working(client):
    clock = Clock()
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[], tick=0.0, sleep=0.02))
    t = threading.Thread(target=lambda: turn(client, "check my inbox", conv_id="b1"))
    t.start()
    for _ in range(300):
        if "b1" in overlay._agent_busy:
            break
        time.sleep(0.01)
    events = turn(client, "and my calendar", conv_id="b1")
    assert "Still on the last one, I'm afraid." in says(events)
    turn(client, "stop", conv_id="b1")
    t.join(5)


def test_eviction_denies_pending_confirm():
    """A real AgentSession closed (LRU eviction) with a pending tool call writes deny first."""
    written = io.StringIO()

    class Proc:
        def __init__(self, *a, **k):
            self.stdin, self.stdout, self.argv = written, iter(()), a[0]

        def poll(self):
            return 0

    session = agent.AgentSession(popen=Proc)
    session.pending = "req-7"
    session.close()
    msg = json.loads(written.getvalue().splitlines()[0])
    assert msg["response"]["request_id"] == "req-7"
    assert msg["response"]["response"]["behavior"] == "deny"


def test_agent_session_round_trip():
    """AgentSession queues stdout events and answers control requests on stdin."""
    lines = [json.dumps(can_use("r1", "Read", {"file_path": "/x"})) + "\n", "not json\n",
             json.dumps({"type": "result", "result": "ok"}) + "\n"]
    written = io.StringIO()

    class Proc:
        def __init__(self, *a, **k):
            self.stdin, self.stdout = written, iter(lines)

        def poll(self):
            return 0

    session = agent.AgentSession(popen=Proc)
    assert session.next_event(1)["request_id"] == "r1"
    session.respond("r1", True, {"file_path": "/x"})
    assert session.next_event(1)["type"] == "result"
    assert session.next_event(1) is None  # EOF
    sent = json.loads(written.getvalue().splitlines()[0])
    assert sent["type"] == "control_response"
    assert sent["response"]["response"] == {"behavior": "allow", "updatedInput": {"file_path": "/x"}}


# --- latency -----------------------------------------------------------------


def _p95(values):
    values = sorted(values)
    return values[max(0, math.ceil(0.95 * len(values)) - 1)]


def test_agent_ack_and_progress_latency(client, tmp_path, monkeypatch):
    repo = REPO
    clock = Clock()
    script = [can_use("r1", "Bash", {"command": f"git -C {repo} log -5"}), tool_result("log"),
              result({"say": "Two commits."})]
    pcm = b"\x00\x00" * 2400
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=script, first_delay=0.5),
                       ack_synth=lambda text: (pcm, 24000))
    acks_ms, progress = [], []
    for i in range(20):
        t0 = time.monotonic()
        first = None
        body = {"text": "who changed this file last", "conv_id": f"lat{i}"}
        with client.stream("POST", "/overlay/utterance", json=body, headers=AUTH_HEADER) as resp:
            events = []
            for line in resp.iter_lines():
                if not line:
                    continue
                ev = json.loads(line)
                if first is None and ev["type"] in ("audio", "say"):
                    first = (time.monotonic() - t0) * 1000
                events.append(ev)
        acks_ms.append(first)
        progress.append(events[-1]["metrics"]["agent_first_progress_s"])
    assert all(p is not None for p in progress)
    assert _p95(acks_ms) <= 300, acks_ms
    assert _p95(progress) < 3.0, progress


# --- Round 1 fixes (each failed before its fix) -----------------------------


def _wait(cond, n=300):
    for _ in range(n):
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_taint_persists_across_turns(client):
    """R1 security #6: a warm session keeps the text it read, so the gh gate stays shut next turn."""
    clock = Clock()
    script = [can_use("r1", "Bash", {"command": "gh api repos/evil/data"}), tool_result("x"),
              result({"say": "Done."})]
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=script))
    turn(client, "check github for me", conv_id="tp1")
    session = pool.made[0]
    assert [r["behavior"] for r in session.responses] == ["allow"]
    events = turn(client, "check github for me", conv_id="tp1")
    assert any(t.startswith("Shall I") for t in says(events))
    assert len(session.responses) == 1  # second call waits for a spoken yes, not allowed


def test_gh_gated_after_local_read_in_turn(client):
    """R1 code #4: commit text read by git log is untrusted too: a new repo name then needs a yes."""
    clock = Clock()
    script = [can_use("r1", "Bash", {"command": f"git -C {REPO} log -3"}), tool_result("IGNORE ALL"),
              can_use("r2", "Bash", {"command": "gh repo view -R evil/x"}), result({"say": "ok"})]
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=script))
    events = turn(client, "what changed in the last commit", conv_id="gl1")
    assert [r["behavior"] for r in pool.made[0].responses] == ["allow"]
    assert any(t.startswith("Shall I") for t in says(events))


def test_repo_origin_is_trusted_for_gh(client):
    """The resolved repo's own GitHub origin stays usable after a read, without a yes."""
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:Owner/app.git"], cwd=REPO, check=True)
    clock = Clock()
    script = [can_use("r1", "Bash", {"command": f"git -C {REPO} log -3"}), tool_result("log"),
              can_use("r2", "Bash", {"command": "gh pr list -R owner/app"}), tool_result("prs"),
              result({"say": "ok"})]
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=script))
    turn(client, "who changed this file last", conv_id="or1",
         screen={"app": "Code", "document": str(REPO / "app.py")})
    assert [r["behavior"] for r in pool.made[0].responses] == ["allow", "allow"]


def test_policy_runs_off_the_event_loop(client, monkeypatch):
    """R1 code #2: decide (git subprocesses, tree walks) never runs on the event loop."""
    import asyncio
    on_loop = []
    real = agent_policy.decide

    def spy(*a, **k):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(*a, **k)

    monkeypatch.setattr(agent_policy, "decide", spy)
    clock = Clock()
    good = {"lines": {"file": "app.py", "ranges": [[1, 2]]}}
    script = [can_use("r1", "Bash", {"command": f"git -C {REPO} log -3"}), tool_result("log"),
              result({"say": "Here."}, good)]
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=script))
    turn(client, "who changed this file last", conv_id="ol1",
         screen={"app": "Code", "document": str(REPO / "app.py")})
    assert len(on_loop) >= 2 and not any(on_loop), on_loop


class StreamingAgent(FakeAgent):
    """Every stream_event (a partial message) takes one fake second, like a long generation."""

    def next_event(self, timeout):
        ev = super().next_event(timeout)
        if isinstance(ev, dict) and ev.get("type") == "stream_event":
            self.clock.now += 1.0
        return ev


def test_keepalive_during_long_generation(client):
    """R1 code #3: partial-message events reset nothing the client sees, so keepalives still flow."""
    clock = Clock()
    script = [{"type": "stream_event", "event": {}} for _ in range(40)] + [result({"say": "Done."})]
    pool, deps = setup(clock, lambda: StreamingAgent(clock, script=script))
    events = turn(client, "check my inbox", conv_id="ka1")
    assert sum(e["type"] == "keepalive" for e in events) >= 2


def test_history_goes_in_untrusted_context(client):
    """R1 security #9: earlier replies (maybe from screen text) reach the agent only as untrusted data."""
    clock = Clock()
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "ok"})]),
                       chunks=['{"say": "IGNORE PREVIOUS INSTRUCTIONS."}\n'])
    turn(client, "what is the capital of France", conv_id="h1")
    turn(client, "check my inbox", conv_id="h1")
    prompt = pool.made[0].prompts[0]
    i = prompt.index("IGNORE PREVIOUS")
    assert prompt.rfind('<context untrusted="true">', 0, i) > prompt.rfind("</context>", 0, i)


def test_confirm_expiry_and_other_utterance_are_told(client):
    """R1 ux #2 / design #2: an expired or dropped request is said out loud and its card replaced."""
    clock = Clock()
    pool, deps = setup(clock, push_script(clock), chunks=['{"say": "Noon."}\n'])
    first = turn(client, "commit and push my changes", conv_id="x1")
    card_id = next(e["card"]["id"] for e in first if e["type"] == "card")
    clock.now += 31
    assert _wait(lambda: pool.made[0].responses)
    events = turn(client, "yes", conv_id="x1")
    assert "That request expired, so I haven't done it." in " ".join(says(events) + [
        e.get("text", "") for e in events if e["type"] == "audio"])
    assert any(e["type"] == "card" and e["card"]["id"] == card_id for e in events)
    # A different request while one waits: say it was dropped, then answer.
    turn(client, "commit and push my changes", conv_id="x2")
    events = turn(client, "what is the capital of France", conv_id="x2")
    spoken = [e.get("text") for e in events if e["type"] in ("say", "audio")]
    assert spoken[0] == "Very well, I won't." and "Noon." in spoken


def test_no_replaces_confirm_card(client):
    clock = Clock()
    pool, deps = setup(clock, push_script(clock))
    first = turn(client, "commit and push my changes", conv_id="nc1")
    card_id = next(e["card"]["id"] for e in first if e["type"] == "card")
    events = turn(client, "No, don't.", conv_id="nc1")
    assert [r["behavior"] for r in pool.made[0].responses] == ["deny"]
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert cards and cards[0]["id"] == card_id and cards[0]["title"] != "Confirm"


def test_progress_only_for_allowed_calls_and_not_right_after_ack(client):
    """R1 ux #3 / #4: no progress line before "Shall I", none within 4 s of the ack; metric still kept."""
    clock = Clock()
    pool, deps = setup(clock, push_script(clock))
    events = turn(client, "commit and push my changes", conv_id="pg1")
    progress = {overlay.acks.PHRASES[k] for k in overlay.acks.PHRASES if k.startswith("progress_")}
    assert not progress & set(says(events))
    script = [can_use("r1", "Bash", {"command": f"git -C {REPO} log -5"}), tool_result("log"),
              result({"say": "Two commits."})]
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=script, first_delay=0.5))
    events = turn(client, "who changed this file last", conv_id="pg2")
    assert not progress & set(says(events))
    assert events[-1]["metrics"]["agent_first_progress_s"] is not None


def test_timeout_shows_a_card(client):
    """Design #3: the timeout is visible, not only spoken."""
    clock = Clock()
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[], tick=1.0))
    events = turn(client, "check my inbox", conv_id="to1")
    assert any(e["type"] == "card" and e["card"]["title"] == "Stopped" for e in events)


def test_late_yes_before_watchdog_hears_expired(client, monkeypatch):
    """R2 ux: a yes after the deadline that beats the watchdog's poll is told it expired, and denied."""
    monkeypatch.setattr(overlay, "CONFIRM_POLL_S", 5.0)  # the watchdog sleeps through the late yes
    clock = Clock()
    pool, deps = setup(clock, push_script(clock))
    turn(client, "commit and push my changes", conv_id="ly1")
    clock.now += 31
    events = turn(client, "yes", conv_id="ly1")
    spoken = " ".join(says(events) + [e.get("text", "") for e in events if e["type"] == "audio"])
    assert "That request expired, so I haven't done it." in spoken and "Very well, I won't." not in spoken
    assert [r["behavior"] for r in pool.made[0].responses] == ["deny"]
    assert any(e["type"] == "card" and e["card"]["title"] == "Not done: it timed out" for e in events)


def test_expired_entries_are_pruned(client):
    """R2 code: an expired confirmation whose conversation never returns does not stay in memory."""
    clock = Clock()
    pool, deps = setup(clock, push_script(clock))
    overlay._expired["gone"] = (clock() - overlay.EXPIRED_TELL_S - 1, "old")
    turn(client, "commit and push my changes", conv_id="pr1")
    clock.now += 31
    assert _wait(lambda: "pr1" in overlay._expired)
    assert "gone" not in overlay._expired
    overlay._expired.pop("pr1", None)


# --- Round 3 (Mac run 3 findings) --------------------------------------------


def test_confirm_turn_event_sequence_for_the_app(client):
    """R3 #1: what the app needs to draw the Confirm card: the question, then the card, then `done`.

    The Mac run saw the card count on `done` but no card on screen. The cause was the macOS
    notification banner over the top of the card column; this pins the wire order the app relies on.
    """
    clock = Clock()
    pcm = b"\x00\x00" * 2400
    pool, deps = setup(clock, push_script(clock), ack_synth=lambda text: (pcm, 24000))
    events = turn(client, "commit and push my changes", conv_id="seq1")
    kinds = [e["type"] for e in events]
    assert kinds[0] == "audio" and events[0]["text"] == overlay.acks.PHRASES["agent_git"]
    assert "clear" not in kinds  # nothing may wipe the HUD in a confirm turn
    asked = next(i for i, e in enumerate(events) if e["type"] in ("say", "audio")
                 and e["text"].startswith("Shall I"))
    card = next(i for i, e in enumerate(events) if e["type"] == "card")
    assert asked < card == len(events) - 2 and kinds[-1] == "done"
    assert events[card]["card"] == {"id": "agentconfirm", "type": "info", "title": "Confirm",
                                    "body": [events[card]["card"]["body"][0], "Say yes to go ahead, or no."]}
    assert events[card]["card"]["body"][0].startswith("run git push in ")
    # The yes turn replaces that same card first, before anything is spoken.
    events = turn(client, "yes", conv_id="seq1")
    assert events[0] == {"type": "card", "card": {"id": "agentconfirm", "type": "info",
                                                  "title": "Going ahead", "body": [events[0]["card"]["body"][0]]}}
    assert events[-1]["type"] == "done" and "Pushed." in says(events)


class SlowFirstTool(FakeAgent):
    """Real wait semantics: an empty poll moves the fake clock by the timeout asked for."""

    def __init__(self, clock, script, release_after):
        super().__init__(clock, script=script)
        self.release_after = release_after
        self.timeouts: list[float] = []

    def send_user(self, text):
        super().send_user(text)
        self.release = self.clock.now + self.release_after

    def next_event(self, timeout):
        self.timeouts.append(timeout)
        if self.clock.now < self.release:
            self.clock.now += min(timeout, self.release - self.clock.now)
            return False
        return super().next_event(timeout)


def test_first_progress_line_on_a_timer_when_the_model_is_slow(client):
    """R3 #3: the model's first tool call came 9.6 s in; the sub's progress line plays at 1.5 s, once."""
    clock = Clock()
    script = [can_use("r1", "Bash", {"command": f"gh pr list -R o/r"}), tool_result("[]"),
              result({"say": "No open pull requests."})]
    made = []
    pool, deps = setup(clock, lambda: made.append(SlowFirstTool(clock, script, 9.6)) or made[-1])
    events = turn(client, "any open PRs on jarvis-companion", conv_id="tm1")
    line = overlay.acks.PHRASES["progress_gh"]
    assert says(events).count(line) == 1
    m = events[-1]["metrics"]
    assert 1.5 <= m["agent_first_progress_s"] < 2.0, m
    assert m["agent_first_tool_s"] >= 9.6, m
    assert made[0].timeouts[0] <= overlay.FIRST_PROGRESS_S  # the loop wakes for the timer, not a 1 s tick
    rows = [s for s in overlay.debug_log.snapshot()["turns"][0]["steps"] if s["step"] == "done"]
    assert rows[0]["agent_first_tool_s"] >= 9.6 and rows[0]["agent_first_progress_s"] < 2.0


def test_no_timer_line_when_a_tool_call_comes_first(client):
    """R3 #3: a fast first tool call (criterion 23's 0.5 s) cancels the timer line."""
    clock = Clock()
    script = [can_use("r1", "Bash", {"command": f"git -C {REPO} log -5"}), tool_result("log"),
              result({"say": "Two commits."})]
    pool, deps = setup(clock, lambda: SlowFirstTool(clock, script, 0.5))
    events = turn(client, "who changed this file last", conv_id="tm2")
    progress = {overlay.acks.PHRASES[k] for k in overlay.acks.PHRASES if k.startswith("progress_")}
    assert not progress & set(says(events))
    assert events[-1]["metrics"]["agent_first_progress_s"] == events[-1]["metrics"]["agent_first_tool_s"]


def test_history_of_file_on_screen_gets_diff_card_and_lines(client):
    """R3 #2: "the code on my screen" -> the host adds a diff card and a lines event for that file."""
    repo = REPO
    (repo / "app.py").write_text("print(1)\nprint(20)\nprint(3)\nprint(4)\n")
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qam", "second"], cwd=repo, check=True)
    clock = Clock()
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[
        result({"say": "Two commits: 'second' and 'first'. The newest changed line 2 and added line 4."})]))
    events = turn(client, "research the git history of the code on my screen and tell me what changed",
                  conv_id="hs1", screen={"app": "Code", "document": str(repo / "app.py")})
    cards = [e["card"] for e in events if e["type"] == "card"]
    # jtools2 R1 design 3: the same card as "since the last merge" (title, rows, the "next change" hint).
    assert cards and cards[-1]["title"] == "Changes in app.py"
    assert cards[-1]["body"] == ["Now: 1/2 line 2 changed", "2/2 line 4 added", 'Say "next change" to jump.']
    lines = [e for e in events if e["type"] == "lines"]
    assert lines == [{"type": "lines", "file": str((repo / "app.py").resolve()), "ranges": [[2, 2], [4, 4]],
                      "labels": ["1/2 changed", "2/2 +1"]}]
    assert overlay._hunks["hs1"].rel == "app.py" and overlay._hunks["hs1"].index == 0
    done = [s for s in overlay.debug_log.snapshot()["turns"][0]["steps"] if s["step"] == "done"]
    assert done[0]["lines"] == 1
    # Not about the screen: no host diff.
    events = turn(client, "what changed in the last commit", conv_id="hs2",
                  screen={"app": "Code", "document": str(repo / "app.py")})
    assert not [e for e in events if e["type"] == "lines"]


def test_prompt_commit_form_passes_policy_and_heredoc_is_a_form_deny(client):
    """R3 #4: the Mac's first `git commit` was denied (shell syntax) and the old prompt said never retry.

    The prompt's one-call commit-and-push form must be a single Confirm the owner hears in full; the
    heredoc form Claude Code's Bash tool teaches is denied with a `form` reason the model may fix once.
    """
    ctx = agent_policy.Ctx(home=str(Path.home()), vault=None, cwd="")
    chain = (f"git -C {REPO} add a.txt b.txt && git -C {REPO} commit -m 'Update a.txt and add b.txt'"
             f" && git -C {REPO} push")
    d = agent_policy.decide("Bash", {"command": chain}, ctx)
    assert isinstance(d, agent_policy.Confirm) and len(d.summary) <= 120
    assert d.summary == "run git add a.txt b.txt, commit -m 'Update a.txt and add b.txt', push in repo"
    assert "git -C REPO add <files> && git -C REPO commit -m 'One short line' && git -C REPO push".replace(
        "REPO", "<repo>") in agent.AGENT_PROMPT
    heredoc = f"git -C {REPO} commit -m \"$(cat <<'EOF'\nUpdate a.txt\n\nCo-Authored-By: x\nEOF\n)\""
    d = agent_policy.decide("Bash", {"command": heredoc}, ctx)
    assert isinstance(d, agent_policy.Deny) and agent_policy.reason_code(d.reason) == "form"
    assert "try once more" in agent.AGENT_PROMPT and "Never ask him for permission" in agent.AGENT_PROMPT


def test_deny_row_shows_reason_code_and_kind_not_input(client):
    """R3 #4/#5: the debug page says why a call was denied (a fixed word) and what kind it was."""
    clock = Clock()
    heredoc = f"git -C {REPO} commit -m \"$(cat <<'EOF'\nSECRET-MSG\nEOF\n)\""
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[can_use("r1", "Bash", {"command": heredoc})],
                                                after={"deny": [result({"say": "I couldn't commit."})]}))
    turn(client, "commit my changes", conv_id="dr1")
    snap = json.dumps(overlay.debug_log.snapshot())
    rows = [s for s in overlay.debug_log.snapshot()["turns"][0]["steps"] if s["step"] == "agent_tool"]
    assert rows == [{**rows[0], "tool": "Bash git commit", "decision": "deny", "kind": "git", "reason": "form"}]
    assert "SECRET-MSG" not in snap and "EOF" not in snap


def test_secrets_refusal_is_the_fixed_line():
    """R3 #5: the model refuses a passwords ask before any tool; the line it must say is fixed."""
    assert agent.SECRETS_LINE == "Your passwords and keys are off limits to me, sir."
    assert len(agent.SECRETS_LINE.split()) <= 12
    assert f'{{"say": "{agent.SECRETS_LINE}"}}' in agent.AGENT_PROMPT


def test_host_diff_goes_through_the_policy(client):
    """R3 #2: the host's own diff of the file on screen obeys the agent policy (secret file, filters)."""
    repo = REPO
    for text in ("A=1\n", "A=2\n"):
        (repo / ".env").write_text(text)
        subprocess.run(["git", "add", "-f", ".env"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "env"], cwd=repo, check=True)
    clock = Clock()
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Two commits."})]))
    events = turn(client, "what changed in this file", conv_id="hp1",
                  screen={"app": "Code", "document": str(repo / ".env")})
    assert not [e for e in events if e["type"] == "lines"]
    assert not [e for e in events if e["type"] == "card" and e["card"]["title"].startswith("Changed in")]
    (repo / "app.py").write_text("print(9)\n")
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qam", "app"], cwd=repo, check=True)
    subprocess.run(["git", "config", "filter.x.clean", "cat"], cwd=repo, check=True)
    events = turn(client, "what changed in this file", conv_id="hp2",
                  screen={"app": "Code", "document": str(repo / "app.py")})
    assert not [e for e in events if e["type"] == "lines"]


# --- Round 4 (Mac run 4 findings) --------------------------------------------


def _changed_app_py(repo: Path) -> None:
    (repo / "app.py").write_text("print(1)\nprint(20)\nprint(3)\nprint(4)\n")
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qam", "second"], cwd=repo, check=True)


def test_model_lines_never_suppress_the_host_card_and_the_first_change_is_scrolled_into_view(client):
    """R4 #1 (Mac run 4, criterion 26): the model sent its own `lines` (an off-screen line 559), which
    used to switch the host's diff card off: `done cards:0 lines:1`. Now the host's card and git-checked
    lines always come for the file on screen, the model's guess for that file is dropped, and VS Code
    is scrolled to the first change with `code --goto <file in repo>:<line>` (no fallback, no shell)."""
    _changed_app_py(REPO)
    target = (REPO / "app.py").resolve()
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return type("P", (), {"returncode": 0, "stdout": ""})()

    clock = Clock()
    reply = [{"say": "Two commits: 'second' and 'first'."}, {"lines": {"file": "app.py", "ranges": [[1, 1]]}}]
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[result(*reply)]), run=fake_run)
    events = turn(client, "research the git history of the code on my screen and tell me what changed",
                  conv_id="r4a", screen={"app": "Code", "document": str(REPO / "app.py")})
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert [c["title"] for c in cards] == ["Changes in app.py"]
    assert [e for e in events if e["type"] == "lines"] == [
        {"type": "lines", "file": str(target), "ranges": [[2, 2], [4, 4]],
         "labels": ["1/2 changed", "2/2 +1"]}]
    assert _wait(lambda: calls == [["code", "--goto", f"{target}:2"]])
    kinds = [e["type"] for e in events]
    assert kinds.index("card") < kinds.index("lines") < kinds.index("done")
    done = [s for s in overlay.debug_log.snapshot()["turns"][0]["steps"] if s["step"] == "done"]
    assert done[0]["cards"] == 1 and done[0]["lines"] == 1

    # Cursor gets its own CLI; any other front app (Xcode, a terminal) is never scrolled.
    calls.clear()
    turn(client, "what changed in this file", conv_id="r4b", screen={"app": "Cursor", "document": str(target)})
    assert _wait(lambda: calls == [["cursor", "--goto", f"{target}:2"]])
    calls.clear()
    events = turn(client, "what changed in this file", conv_id="r4c", screen={"app": "Xcode", "document": str(target)})
    assert calls == [] and [e for e in events if e["type"] == "lines"]

    # A request not about the screen keeps the model's validated lines (no host card, no scroll).
    events = turn(client, "what changed in the last commit", conv_id="r4d",
                  screen={"app": "Code", "document": str(target)})
    assert calls == [] and [e["ranges"] for e in events if e["type"] == "lines"] == [[[1, 1]]]


def test_editor_scroll_is_skipped_when_the_cli_is_missing(client):
    """No `code` on PATH: the card and lines still come, and nothing else is opened (no `open -t`)."""
    _changed_app_py(REPO)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        raise FileNotFoundError

    clock = Clock()
    pool, deps = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Two commits."})]), run=fake_run)
    events = turn(client, "what changed in this file", conv_id="r4e",
                  screen={"app": "Code", "document": str(REPO / "app.py")})
    assert _wait(lambda: [c[0] for c in calls] == ["code"])
    assert [e["type"] for e in events].count("card") == 1 and [e for e in events if e["type"] == "lines"]


class BusRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, kind, data=None):
        self.events.append((kind, data or {}))


def test_expired_confirm_is_pushed_to_the_app(client, monkeypatch):
    """R4 #2 (Mac run 4, criterion 32): after the 30 s expiry the Confirm card stayed 20 s and more.
    The HTTP turn has ended, so the watchdog pushes `hud` events on /overlay/events: the same card id
    retitled "Not done: it timed out", then the expired line. The app handles them like turn events."""
    bus = BusRecorder()
    monkeypatch.setattr(overlay, "bus", bus)
    clock = Clock()
    pool, deps = setup(clock, push_script(clock))
    first = turn(client, "commit and push my changes", conv_id="ex1")
    confirm = next(e["card"] for e in first if e["type"] == "card")
    assert confirm["title"] == "Confirm" and bus.events == []
    clock.now += 31
    assert _wait(lambda: len(bus.events) >= 2)
    assert [r["behavior"] for r in pool.made[0].responses] == ["deny"]
    (k1, card), (k2, line) = bus.events
    assert k1 == k2 == "hud"
    assert card == {"type": "card", "card": {**confirm, "title": "Not done: it timed out",
                                             "body": [confirm["body"][0]]}}
    assert line["type"] in ("say", "audio") and line["text"] == "That request expired, so I haven't done it."
    assert len(line["text"].split()) <= 12


def test_yes_or_no_in_time_pushes_nothing(client, monkeypatch):
    """Only the watchdog pushes: a settled confirmation answers inside its own turn."""
    bus = BusRecorder()
    monkeypatch.setattr(overlay, "bus", bus)
    clock = Clock()
    pool, deps = setup(clock, push_script(clock))
    turn(client, "commit and push my changes", conv_id="ex2")
    turn(client, "no", conv_id="ex2")
    clock.now += 31
    time.sleep(0.1)
    assert bus.events == []


def test_new_conversation_replaces_the_old_confirm_card(client):
    """R4 #2: a confirm dropped because a new conversation started also loses its Confirm card."""
    clock = Clock()
    pool, deps = setup(clock, push_script(clock), chunks=['{"say": "Noon."}\n'])
    first = turn(client, "commit and push my changes", conv_id="nc-a")
    card_id = next(e["card"]["id"] for e in first if e["type"] == "card")
    events = turn(client, "what time is it", conv_id="nc-b")
    assert [r["behavior"] for r in pool.made[0].responses] == ["deny"]
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert cards[0]["id"] == card_id and cards[0]["title"] == "Not done"


def test_event_bus_publish_from_another_thread():
    """The watchdog runs on its own thread; the bus hands items to the subscriber's loop safely."""
    import asyncio

    async def main():
        bus = overlay.EventBus()
        async with bus.subscribe() as q:
            threading.Thread(target=bus.publish, args=("hud", {"type": "say", "text": "x"})).start()
            return await asyncio.wait_for(q.get(), 2)

    assert asyncio.run(main()) == ("hud", {"type": "say", "text": "x"})


def test_hung_editor_cli_never_holds_the_turn(client):
    """R5 design 1: `code --goto` hanging (stuck VS Code IPC) must not hold the highlight or `done`."""
    _changed_app_py(REPO)
    release = threading.Event()
    timeouts: list = []

    def hung_run(cmd, **kw):
        timeouts.append(kw.get("timeout"))
        release.wait(5)
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    clock = Clock()
    setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Two commits."})]), run=hung_run)
    try:
        t0 = time.monotonic()
        events = turn(client, "what changed in this file", conv_id="r5a",
                      screen={"app": "Code", "document": str(REPO / "app.py")})
        assert time.monotonic() - t0 < 3
        kinds = [e["type"] for e in events]
        assert "lines" in kinds and kinds[-1] == "done"
        assert _wait(lambda: timeouts == [2])
    finally:
        release.set()


def test_expiry_push_never_retitles_another_pending_confirm(client, monkeypatch):
    """R5 security 3: the Confirm card id is shared. If another conversation's Confirm is pending when
    this one expires, the push must not turn that live card into "Not done"; only the line goes out."""
    bus = BusRecorder()
    monkeypatch.setattr(overlay, "bus", bus)
    clock = Clock()
    setup(clock, push_script(clock))
    turn(client, "commit and push my changes", conv_id="race1")
    overlay._confirm["race2"] = object()  # a newer Confirm, already registered (its card is on screen)
    try:
        clock.now += 31
        assert _wait(lambda: len(bus.events) >= 1)
        time.sleep(0.1)
        assert [e["type"] for _, e in bus.events] in (["say"], ["audio"])
    finally:
        overlay._confirm.pop("race2", None)


# --- Round 6 (Mac run 5: no highlight drawn; approver's small issues) ----------


def test_highlight_outcome_and_editor_scroll_reach_the_turn_record(client):
    """R6: the app posts one reason code per `lines` event after the turn's `done`; it lands on that
    turn (not a new one), next to the service's own `editor_scroll` row (result + time)."""
    _changed_app_py(REPO)

    def fake_run(cmd, **kw):
        return type("P", (), {"returncode": 0, "stdout": ""})()

    clock = Clock()
    setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Two commits."})]), run=fake_run)
    turn(client, "what changed in this file", conv_id="r6a", screen={"app": "Code", "document": str(REPO / "app.py")})
    assert _wait(lambda: any(s["step"] == "editor_scroll"
                             for s in overlay.debug_log.snapshot()["turns"][0]["steps"]))
    overlay.debug_log.note("lines", "off_view win=title seen=1-45 try=3 want=936-949")
    turns = overlay.debug_log.snapshot()["turns"]
    assert len(turns) == 1
    steps = {s["step"]: s for s in turns[0]["steps"]}
    assert steps["editor_scroll"]["detail"].startswith("code in ")
    assert steps["lines"]["detail"] == "off_view win=title seen=1-45 try=3 want=936-949"
    # Other app notes still open their own turn (no speech heard after a wake, and so on).
    overlay.debug_log.note("no_speech", "")
    assert len(overlay.debug_log.snapshot()["turns"]) == 2


def test_editor_scroll_row_says_why_it_did_not_scroll(client):
    _changed_app_py(REPO)

    def fake_run(cmd, **kw):
        raise FileNotFoundError

    clock = Clock()
    setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Two commits."})]), run=fake_run)
    turn(client, "what changed in this file", conv_id="r6b", screen={"app": "Code", "document": str(REPO / "app.py")})
    rows = lambda: [s for s in overlay.debug_log.snapshot()["turns"][0]["steps"] if s["step"] == "editor_scroll"]  # noqa: E731
    assert _wait(lambda: len(rows()) == 1)
    assert rows()[0]["detail"].startswith("none in ")


def test_terminal_repo_is_the_front_windows_or_none(tmp_path):
    """Approver: three Terminal windows, lsof lists every shell. One repo -> it; several -> the one
    the front window's title names; no single match -> None (the agent asks which repo)."""
    a, b = git_repo(tmp_path / "jt-scratch32"), git_repo(tmp_path / "other")
    (a / "sub").mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()
    def pick(cwds, title):
        found = overlay._pick_terminal_cwd(cwds, title)
        return found.resolve() if found else None
    assert pick([plain, a / "sub", a], "") == a.resolve()  # one repo, whatever the title
    assert pick([a, b, plain], "jt-scratch32 — -zsh — 80×24") == a.resolve()
    assert pick([a, b], "other — -zsh — 80×24") == b.resolve()
    assert pick([a, b], "ali — -zsh — 80×24") is None
    assert pick([a, b], "jt-scratch32-old — -zsh") is None  # a longer name is not this folder
    assert pick([plain], "plain") is None


# --- jtools2: repo from the front Terminal window (run 6 shape), origin in the prompt ---------


def _run6(tmp_path, monkeypatch):
    """Run 6 on the Mac: 4 Terminal windows (driver bash in ~, a new fish one in ~/jt-scratch6 in
    front, one in the overlay clone, one more), shells found by pgrep + lsof."""
    home = tmp_path / "home"
    scratch, clone = git_repo(home / "jt-scratch6"), git_repo(home / "jarvis-companion-overlay")
    (home / "Downloads").mkdir()
    shells = {"101": home, "102": scratch, "103": clone, "104": home / "Downloads"}

    def fake_cmd(argv, timeout=1):
        argv = [os.path.basename(argv[0]), *argv[1:]] if argv[0] in (overlay._PGREP, overlay._LSOF) else argv
        if argv[:2] == ["pgrep", "-x"]:
            return "50\n" if argv[2] == "Terminal" else ""
        if argv[:2] == ["pgrep", "-P"]:
            parents = set(argv[2].split(","))
            return {frozenset({"50"}): "60\n61\n62\n63\n", frozenset({"60", "61", "62", "63"}):
                    "101\n102\n103\n104\n", frozenset(shells): "201\n"}.get(frozenset(parents), "")
        if argv[0] == "lsof":  # login (root) prints nothing; the driver's curl runs in ~
            return "".join(f"p{pid}\nfcwd\nn{d}\n" for pid, d in shells.items()) + f"p201\nfcwd\nn{home}\n"
        return ""

    monkeypatch.setattr(overlay, "_cmd", fake_cmd)
    monkeypatch.setattr(overlay, "_terminal_cwd", _REAL_TERMINAL_CWD)
    monkeypatch.setattr(overlay.sys, "platform", "darwin")
    windows = [{"app": "Terminal", "title": "jt-scratch6 — ~/jt-scratch6 — -fish — 80×24", "box": [0, 0, 600, 400]},
               {"app": "Terminal", "title": "ali — -bash — 80×24", "box": [20, 20, 620, 420]},
               {"app": "Terminal", "title": "jarvis-companion-overlay — -zsh — 120×40", "box": [40, 40, 900, 600]},
               {"app": "Terminal", "title": "Downloads — -zsh — 80×24", "box": [60, 60, 660, 460]}]
    return scratch, windows


def test_terminal_repo_run6_shape(tmp_path, monkeypatch):
    scratch, windows = _run6(tmp_path, monkeypatch)
    info: dict = {}
    found = overlay._agent_repo(overlay.ScreenBody(app="Terminal", windows=windows), info)
    assert found is not None and found.resolve() == scratch.resolve()
    detail = overlay._repo_pick_detail(info)
    assert re.fullmatch(r"terminal shells=\d+ repos=\d+ title_hits=1 app=terminal procs=\d+", detail) and "/" not in detail
    # The app name the screen reports may differ from the window owner's: the first terminal window counts.
    found = overlay._agent_repo(overlay.ScreenBody(app="Terminal.app", windows=windows), {})
    assert found is not None and found.resolve() == scratch.resolve()
    # R1 code 8: with another app in front (no window of it listed), a background Terminal never picks.
    behind = [{"app": "Safari", "title": "News", "box": [0, 0, 900, 700]}] + windows
    assert overlay._agent_repo(overlay.ScreenBody(app="Slack", windows=behind), {}) is None
    assert overlay._agent_repo(overlay.ScreenBody(app="Slack", windows=windows), {}) is not None  # front = Terminal
    # A ~/path in the title beats a shorter folder name that is also in it.
    deep = git_repo(tmp_path / "home" / "jt-scratch6-x" / "jt-scratch6")
    assert overlay._title_hit(deep, "jt-scratch6 — ~/jt-scratch6 — -fish") < overlay._title_hit(
        scratch, "jt-scratch6 — ~/jt-scratch6 — -fish")


def test_terminal_repo_with_the_service_launchd_path(tmp_path, monkeypatch):
    """R3 Mac 30: the service's launchd PATH (plist: ~/.local/bin, brew, /usr/bin, /bin) has no
    /usr/sbin, where macOS keeps lsof, so a bare "lsof" never ran and every turn logged shells=0.
    The tools are called by absolute path; the row now also shows terminal processes found."""
    scratch, windows = _run6(tmp_path, monkeypatch)
    fake = overlay._cmd
    plist = (Path(__file__).parent.parent / "launchd" / "com.jarvis.overlay.service.plist").read_text()
    path = re.search(r"<key>PATH</key>\s*<string>([^<]*)</string>", plist).group(1).split(":")
    mac_bin = {"pgrep": "/usr/bin", "lsof": "/usr/sbin"}

    def launchd_cmd(argv, timeout=1):
        name = os.path.basename(argv[0])
        found = argv[0] == f"{mac_bin.get(name)}/{name}" if "/" in argv[0] else mac_bin.get(name) in path
        return fake(argv, timeout) if found else ""  # _cmd turns FileNotFoundError into ""

    monkeypatch.setattr(overlay, "_cmd", launchd_cmd)
    info: dict = {}
    found = overlay._agent_repo(overlay.ScreenBody(app="Terminal", windows=windows), info)
    assert found is not None and found.resolve() == scratch.resolve()
    assert overlay._repo_pick_detail(info) == "terminal shells=5 repos=2 title_hits=1 app=terminal procs=9"
    # Not a terminal in front: the row says so instead of a bare shells=0.
    assert overlay._repo_pick_detail({"how": "none"}) == "none shells=0 repos=0 title_hits=0 app=other procs=0"


def test_repo_pick_row_and_origin_line_reach_the_turn(client, tmp_path, monkeypatch):
    """The debug row carries counts only; the prompt names the GitHub origin, never the remote URL."""
    scratch, windows = _run6(tmp_path, monkeypatch)
    subprocess.run(["git", "remote", "add", "origin", "https://u:ghp_FAKE@github.com/o/r.git"], cwd=scratch,
                   check=True)
    clock = Clock()
    pool, _ = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "One commit."})]))
    turn(client, "what changed in the last commit", conv_id="rp1", screen={"app": "Terminal", "windows": windows})
    prompt = pool.made[0].prompts[0]
    assert "origin: o/r" in prompt and "ghp_FAKE" not in prompt
    steps = {s["step"]: s for s in overlay.debug_log.snapshot()["turns"][0]["steps"]}
    assert re.fullmatch(r"terminal shells=\d+ repos=\d+ title_hits=1 app=terminal procs=\d+", steps["repo_pick"]["detail"])


def test_agent_argv_hides_mcp_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "SUPPORT_DIR", tmp_path / "support")  # as the reset fixture does
    tools = agent.SUPPORT_DIR / "agent-tools.json"
    tools.parent.mkdir(parents=True)
    tools.write_text(json.dumps({"read_only": ["mcp__m__search"], "confirm": ["mcp__m__send"],
                                 "hidden": ["mcp__m__delete", "mcp__m__remove_account"]}))
    argv = agent.agent_argv()
    i = argv.index("--disallowedTools")
    assert argv[i + 1:i + 3] == ["mcp__m__delete", "mcp__m__remove_account"]
    assert argv[i + 3].startswith("--")
    assert agent.confirm_mcp_tools() == frozenset({"mcp__m__send"})
    with pytest.raises(ValueError):
        agent.agent_argv(hidden=("bad name",))
    tools.write_text(json.dumps({"read_only": []}))
    assert "--disallowedTools" not in agent.agent_argv()
    assert agent.confirm_mcp_tools() is None


def test_confirm_list_reaches_the_policy(client):
    """2c: the gateway loads the confirm list into the session's policy, so an unlisted tool is denied."""
    (agent.SUPPORT_DIR).mkdir(parents=True, exist_ok=True)
    (agent.SUPPORT_DIR / "agent-tools.json").write_text(json.dumps(
        {"read_only": ["mcp__m__search"], "confirm": ["mcp__m__send"], "hidden": ["mcp__m__delete"]}))
    clock = Clock()
    pool, _ = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Done."})]))
    turn(client, "check my email", conv_id="cm1")
    ctx = pool.made[0].policy_ctx
    assert ctx.confirm_mcp == frozenset({"mcp__m__send"})
    assert isinstance(agent_policy.decide("mcp__m__delete", {}, ctx), agent_policy.Deny)
