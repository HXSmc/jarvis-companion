"""Hunk navigation over the git-diff turn: "next change" cursor, one-box highlight, blame-driven why.

The repo is real (tests/fixtures/overlay/hunk_fixture.py: one added, one changed, one removed
hunk in sample.py, plus other.py with more changed lines so the file on screen must win).
Only the editor CLI is stubbed: `run` records argv, so the scroll target is asserted, never run.
"""

from __future__ import annotations

import importlib.util
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
    assert "Change 2 of 3, lines 149 to 149." in says(events)
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


def test_next_change_without_a_list(client):
    overlay.set_deps(make_deps(chunks=['{"say": "Which one?"}\n']))
    events = turn(client, "next change", conv_id="n1")
    assert acks.PHRASES["no_hunks"] in says(events)
    assert not [e for e in events if e["type"] == "lines"]
    # "why did this change" with no cursor falls through to the answer tier.
    events = turn(client, "why did this change", conv_id="n1")
    assert "Which one?" in says(events)


def test_why_did_this_change_uses_blame_and_taints(client, repo):
    calls: list = []
    _seed(client, repo, calls)
    quiet = FakeConversations(['{"say": "It added a retry counter."}\n'])
    overlay.set_deps(make_deps(run=lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=""),
                               conversations=FakeConversations(),
                               quiet_conversations=quiet))
    events = turn(client, "why did this change", conv_id="h1")
    assert "It added a retry counter." in says(events)
    prompt = quiet.sessions["h1"].prompts[-1]  # the tool-less pool, not the web one
    assert "Add the retry counter" in prompt  # the blame subject rides as untrusted context
    assert '<context untrusted="true">' in prompt
    assert "Why did change 1 of 3 in sample.py, lines 140 to 142" in prompt
    assert "h1" in overlay._tainted

    turn(client, "next change", conv_id="h1")
    turn(client, "next change", conv_id="h1")  # hunk 3: removed lines, blame can't trace them
    prompts_before = len(quiet.sessions["h1"].prompts)
    events = turn(client, "why did this change", conv_id="h1")
    assert acks.PHRASES["why_removed"] in says(events)
    assert len(quiet.sessions["h1"].prompts) == prompts_before  # fixed line, no model call


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
