"""Hunk navigation over the git-diff turn: "next change" cursor, one-box highlight, blame-driven why.

The repo is real (tests/fixtures/overlay/hunk_fixture.py: one added, one changed, one removed
hunk in sample.py, plus other.py with more changed lines so the file on screen must win).
Only the editor CLI is stubbed: `run` records argv, so the scroll target is asserted, never run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from conftest import TOKEN
from gateway import overlay
from mac.voice import acks, gitdiff
from test_overlay_service import FakeConversations, make_app, make_deps, says, turn


def _wait(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def reset(monkeypatch, tmp_path):
    for name in ("_history", "_notes", "_card_ids", "_pending", "_hunks"):
        getattr(overlay, name).clear()
    overlay._tainted.clear()
    overlay.debug_log.clear()
    overlay.set_deps(None)
    monkeypatch.delenv("OVERLAY_GIT_REPO", raising=False)
    monkeypatch.delenv("OVERLAY_VAULT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # policy home: the fixture repo's git is Allow
    monkeypatch.setattr(gitdiff, "editor_path", lambda editor: editor)  # the fake `run` records "code"
    yield
    overlay.set_deps(None)


@pytest.fixture
def client(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    monkeypatch.setattr(overlay, "TOKEN_FILE", token_file)
    return TestClient(make_app(), client=("127.0.0.1", 50000))


@pytest.fixture
def repo(tmp_path) -> dict:
    spec = importlib.util.spec_from_file_location(
        "hunk_fixture_nav", Path(__file__).parent / "fixtures/overlay/hunk_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(tmp_path / "home" / "r", extra=True)


def _seed(client, repo: dict, calls: list) -> list[dict]:
    """A git_diff turn with sample.py on screen in Code: stores the cursor and scrolls to hunk 1."""
    overlay.set_deps(make_deps(run=lambda cmd, **kw: calls.append(list(cmd))
                               or SimpleNamespace(returncode=0, stdout="")))
    root = Path(repo["repo"])
    return turn(client, "show the diff", conv_id="h1",
                screen={"app": "Code", "document": str(root / "sample.py")})


def test_next_change_scrolls_and_highlights_one_hunk(client, repo):
    calls: list = []
    events = _seed(client, repo, calls)
    root = Path(repo["repo"])
    sample = root / "sample.py"
    # sample.py is on screen, so it beats other.py (12 changed lines vs 3).
    first = next(e for e in events if e["type"] == "lines")
    assert first == {"type": "lines", "file": str(root / "sample.py"),
                     "ranges": [[140, 142], [149, 149], [155, 155]],
                     "labels": ["1/3 +3", "2/3 changed", "3/3 removed here"]}
    assert overlay._hunks["h1"].rel == "sample.py"
    assert _wait(lambda: calls == [["code", "--goto", f"{sample.resolve()}:140"]])

    events = turn(client, "next change", conv_id="h1")
    assert [e for e in events if e["type"] == "lines"] == [
        {"type": "lines", "file": str(root / "sample.py"), "ranges": [[149, 149]],
         "labels": ["2/3 changed"]}]
    assert "Change 2 of 3, line 149." in says(events)  # R1 ux 1 / design 2: one line is "line a"
    assert _wait(lambda: calls[-1] == ["code", "--goto", f"{sample.resolve()}:149"])
    steps = overlay.debug_log.snapshot()["turns"][0]["steps"]  # newest turn first
    assert any(s.get("step") == "hunk" and s.get("detail") == "next 2/3 lines=149-149"
               for s in steps)

    events = turn(client, "next change", conv_id="h1")  # the removed hunk is still navigable
    assert [e for e in events if e["type"] == "lines"][0]["labels"] == ["3/3 removed here"]

    events = turn(client, "next change", conv_id="h1")  # past the end: no move, no lines
    assert acks.PHRASES["last_hunk"] in says(events)
    assert not [e for e in events if e["type"] == "lines"]
    assert overlay._hunks["h1"].index == 2

    events = turn(client, "previous change", conv_id="h1")
    assert [e for e in events if e["type"] == "lines"][0]["ranges"] == [[149, 149]]
    # R1 code 9: previous walks back to hunk 1, then stops with the first-change line.
    events = turn(client, "previous change", conv_id="h1")
    assert [e for e in events if e["type"] == "lines"][0]["labels"] == ["1/3 +3"]
    assert "Change 1 of 3, lines 140 to 142." in says(events)
    events = turn(client, "previous change", conv_id="h1")
    assert acks.PHRASES["first_hunk"] in says(events)
    assert not [e for e in events if e["type"] == "lines"]
    assert overlay._hunks["h1"].index == 0


def test_next_change_without_a_list(client):
    overlay.set_deps(make_deps(chunks=['{"say": "Which one?"}\n']))
    events = turn(client, "next change", conv_id="n1")
    assert acks.PHRASES["no_hunks"] in says(events)
    assert not [e for e in events if e["type"] == "lines"]
    # "why did this change" with no cursor falls through to the answer tier.
    events = turn(client, "why did this change", conv_id="n1")
    assert "Which one?" in says(events)


def test_why_did_this_change_uses_blame_and_taints(client, repo):
    """jfollow item 3: the answer is a fixed template from the blamed commit (no model call); the
    commit text still rides as an untrusted note for a later free question, and the conv is tainted."""
    calls: list = []
    _seed(client, repo, calls)
    quiet = FakeConversations(['{"say": "It added a retry counter."}\n'])
    loud = FakeConversations(['{"say": "It added a retry counter."}\n'])
    overlay.set_deps(make_deps(run=lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=""),
                               conversations=loud, quiet_conversations=quiet))
    events = turn(client, "why did this change", conv_id="h1")
    assert any(s.startswith('Change 1 of 3 came from "Add the retry counter", committed ') for s in says(events))
    assert quiet.sessions == {} and loud.sessions == {}  # no model call
    assert "Add the retry counter" in overlay._notes["h1"][-1]
    assert "h1" in overlay._tainted

    # On hunk 2 the answer and the note hold hunk 2's subject and diff, not hunk 1's or 3's.
    turn(client, "next change", conv_id="h1")
    events = turn(client, "why did this change", conv_id="h1")
    (line,) = [s for s in says(events) if s.startswith("Change 2 of 3")]
    assert '"Rename the timeout flag"' in line and "retry" not in line and "cache" not in line
    note = overlay._notes["h1"][-1]
    assert "Rename the timeout flag" in note and "+L904 = 146" in note
    assert "Drop the unused cache lines" not in note and "Add the retry counter" not in note
    turn(client, "next change", conv_id="h1")  # hunk 3: removed lines, blame can't trace them
    events = turn(client, "why did this change", conv_id="h1")
    assert acks.PHRASES["why_removed"] in says(events)
    assert quiet.sessions == {} and loud.sessions == {}


def test_hunk_state_is_bounded_and_cleared(client, repo, monkeypatch):
    calls: list = []
    _seed(client, repo, calls)
    assert overlay._hunks["h1"].rel == "sample.py"
    turn(client, "clear", conv_id="h1")
    assert "h1" not in overlay._hunks  # the band cancel also drops the cursor
    assert not [e for e in turn(client, "next change", conv_id="h1") if e["type"] == "lines"]

    # A file with more than 20 hunks keeps only the first 20 in the cursor and the event.
    many = [{"file": "a.py", "start": 10 * i, "count": 1, "old": 1} for i in range(1, 26)]
    monkeypatch.setattr(gitdiff, "repo_for", lambda document: Path(repo["repo"]))
    monkeypatch.setattr(gitdiff, "last_merge", lambda r: repo["merge"])
    monkeypatch.setattr(gitdiff, "hunks", lambda r, b: many)
    monkeypatch.setattr(gitdiff, "diff_text", lambda r, b: "", raising=False)
    events = turn(client, "show the diff", conv_id="m1")
    assert len(overlay._hunks["m1"].hunks) == 20
    assert overlay._hunks["m1"].hunks[-1]["start"] == 200
    assert len(next(e for e in events if e["type"] == "lines")["ranges"]) == 20

    # Cursors die with their conversation: the 32-slot memory rotation pops the oldest.
    overlay._hunks["old1"] = overlay._hunks.pop("m1")
    overlay._history.clear()
    overlay._history["old1"] = deque([("hi", "Hello.")])
    for i in range(31):
        overlay._history[f"c{i}"] = deque([("q", "a")])
    turn(client, "next change", conv_id="fresh")
    assert "old1" not in overlay._hunks and "old1" not in overlay._history


def test_why_refused_when_the_policy_says_no(client, repo, monkeypatch):
    """R1 code 9: a "why" whose policy probe is not Allow speaks hunk_refused and asks no model; the probe
    runs off the event loop (R1 code 5)."""
    calls: list = []
    _seed(client, repo, calls)
    quiet = FakeConversations(['{"say": "x"}\n'])
    overlay.set_deps(make_deps(conversations=FakeConversations(), quiet_conversations=quiet))
    threads: list = []
    real = overlay.agent_policy.decide

    def deny(tool, inp, ctx):
        import asyncio
        try:  # a worker thread has no running loop; the event loop's thread does
            asyncio.get_running_loop()
            threads.append(True)
        except RuntimeError:
            threads.append(False)
        return overlay.agent_policy.Deny("that repository's own git config names programs to run")

    monkeypatch.setattr(overlay.agent_policy, "decide", deny)
    blamed: list = []
    monkeypatch.setattr(gitdiff, "blame_commits", lambda *a, **k: blamed.append(a) or ([], False))
    events = turn(client, "why did this change", conv_id="h1")
    assert acks.PHRASES["hunk_refused"] in says(events)
    assert not quiet.sessions and not blamed
    assert threads and not any(threads)  # never on the event loop
    monkeypatch.setattr(overlay.agent_policy, "decide", real)


def test_cursor_is_stored_before_the_boxes_and_a_new_highlight_replaces_it(client, repo, monkeypatch):
    """R1 code 6: a barge-in right after the boxes still leaves the cursor. R1 code 7: a new highlight
    that finds nothing drops the old cursor."""
    import asyncio
    root = Path(repo["repo"])
    deps = make_deps(run=lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=""))
    body = overlay.TurnBody(text="show the diff", conv_id="b1",
                            screen=overlay.ScreenBody(app="Code", document=str(root / "sample.py")))

    async def until_lines():
        gen = overlay.run_turn(body, deps)
        async for ev in gen:
            if ev["type"] == "lines":
                stored = "b1" in overlay._hunks
                await gen.aclose()  # the app barged in: the stream stops here
                return stored
        return None

    assert asyncio.run(until_lines()) is True
    calls: list = []
    _seed(client, repo, calls)
    assert "h1" in overlay._hunks
    monkeypatch.setattr(gitdiff, "hunks", lambda r, b: [])
    turn(client, "show the diff", conv_id="h1")
    assert "h1" not in overlay._hunks
    assert acks.PHRASES["no_hunks"] in says(turn(client, "next change", conv_id="h1"))


def test_hunk_span_card_and_line_helpers():
    """R1 code 11: a deletion at the top of a file (start 0) is line 1. R1 design 4: past five hunks the
    card shows four and "+N more", and still the hint, within six lines."""
    top = {"file": "a.py", "start": 0, "count": 0, "old": 2}
    assert overlay._hunk_span(top) == (1, 1)
    assert overlay._hunk_line(1, 2, top) == "Change 1 of 2, line 1."
    assert overlay._hunk_line(2, 2, {"start": 5, "count": 3, "old": 1}) == "Change 2 of 2, lines 5 to 7."
    many = [{"file": "a.py", "start": 10 * i, "count": 1, "old": 1} for i in range(1, 10)]
    body = overlay._hunk_card("a.py", many)["body"]
    assert body == ["1/9 line 10 changed", "2/9 line 20 changed", "3/9 line 30 changed",
                    "4/9 line 40 changed", "+5 more", 'Say "next change" to jump.']
    assert overlay._hunk_card("a.py", many[:1])["body"] == ["1/1 line 10 changed"]


def test_one_file_is_singular_and_backticks_are_not_spoken():
    """R3 Mac UX: "1 file" (not "1 files") on the card and in the spoken summary; the "why" answer
    quotes code in backticks, which the voice must not read out (the caption keeps the text)."""
    import asyncio
    from types import SimpleNamespace

    from mac.voice import gitdiff

    assert gitdiff.files_phrase(1) == "1 file" and gitdiff.files_phrase(2) == "2 files"
    stat = gitdiff.diff_cards([{"file": "a.py", "start": 3, "count": 1, "old": 1}], "abc1234")[0]
    assert stat["value"] == "1 file"
    heard = []
    deps = SimpleNamespace(synth=lambda text: (heard.append(text), (b"\0\0", 16000))[1])
    line = "Line 149 changed because the old `L146 = 146` became ``L904 = 146``."
    audio = asyncio.run(overlay._audio(line, deps))
    assert heard == ["Line 149 changed because the old L146 = 146 became L904 = 146."]
    assert audio["text"] == line


def test_fixture_wrap_and_fold_modes(tmp_path):
    """The live-Mac fixture modes: each builds a repo whose .vscode setting stands for the
    editor case and whose last commit is the diff the case needs (a >300-char line / an
    import line gaining a second name)."""
    fixture = Path(__file__).parent / "fixtures/overlay/hunk_fixture.py"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
    for directory, mode, setting, want in (
            (tmp_path / "x", "--wrap", {"editor.wordWrap": "on"},
             lambda diff: any(l.startswith("+") and len(l) > 300 for l in diff.splitlines())),
            (tmp_path / "y", "--fold", {"editor.foldingImportsByDefault": True},
             lambda diff: "a06, b06" in diff)):
        proc = subprocess.run([sys.executable, str(fixture), str(directory), mode],
                              capture_output=True, text=True, env=env, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert json.loads((directory / ".vscode/settings.json").read_text()) == setting
        diff = subprocess.run(["git", "-C", str(directory), "diff", "-U0", "HEAD~1"],
                              capture_output=True, text=True, env=env, check=True).stdout
        assert want(diff), f"{mode}: {diff[:400]}"
