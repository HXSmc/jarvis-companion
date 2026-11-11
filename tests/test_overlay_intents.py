"""Overlay intents/acks/lean: validation, injection refusal, ack fallback, LRU (no real processes)."""

from __future__ import annotations

import pathlib
import time
from types import SimpleNamespace

import pytest

from mac.voice import acks
from mac.voice.intents import Intent, list_apps, open_app, open_site, parse
from mac.voice.lean import Conversations, lean_argv


class FakeRun:
    """subprocess.run stand-in that records argv instead of starting anything."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return SimpleNamespace(returncode=0)


APPS = {"safari": "Safari", "visual studio code": "Visual Studio Code"}


def test_open_app_validated():
    run = FakeRun()
    intent = parse("open safari")
    assert (intent.kind, intent.arg) == ("open_app", "safari")
    assert open_app(intent.arg, run=run, apps=APPS) == "Safari"
    assert run.calls == [["open", "-a", "Safari"]]
    # Case-insensitive exact, then unique prefix match.
    assert open_app("VISUAL STUDIO CODE", run=run, apps=APPS) == "Visual Studio Code"
    assert open_app("visual", run=run, apps=APPS) == "Visual Studio Code"
    # Unknown app: refused, nothing run.
    assert parse("open Photoshop").kind == "open_app"
    assert open_app("Photoshop", run=run, apps=APPS) is None
    assert len(run.calls) == 3


def test_open_site_https_only():
    run = FakeRun()
    intent = parse("open github.com")
    assert (intent.kind, intent.arg) == ("open_site", "github.com")
    assert open_site(intent.arg, run=run) == "https://github.com"
    assert parse("open github.com/HXSmc").kind == "open_site"
    assert open_site("github.com/HXSmc", run=run) == "https://github.com/HXSmc"
    assert run.calls == [["open", "https://github.com"], ["open", "https://github.com/HXSmc"]]
    for bad in ("http://x.com", "javascript:alert(1)", "file:///etc"):
        assert open_site(bad, run=run) is None
    assert len(run.calls) == 2


def test_open_rejects_injection():
    run = FakeRun()
    for bad in ("Safari; rm -rf ~", "Safari && say hi", "$(id)", "a.com; ls", "-a Terminal"):
        assert open_app(bad, run=run, apps=APPS) is None
        assert open_site(bad, run=run) is None
    assert run.calls == []


def test_parse_kinds():
    assert parse("hey jarvis clear") == Intent("clear")
    assert parse("Jarvis, open github.com") == Intent("open_site", "github.com")
    assert parse("open safari") == Intent("open_app", "safari")
    assert parse("show me the button to export a PDF").kind == "ground"
    assert parse("highlight the changes after the last merge").kind == "git_diff"
    assert parse("what is the capital of France").kind == "answer"


def test_git_diff_word_boundary_only():
    # Substring hits like "difference"/"different"/"diffusion" stay with the model.
    for q in ("what's the difference between TCP and UDP", "explain the difference again",
              "is this different", "diffusion models"):
        assert parse(q).kind == "answer", q
    for q in ("show the diff", "what changed since the last merge",
              "highlight the changes after the last merge"):
        assert parse(q).kind == "git_diff", q


def test_ground_needs_lead_and_ui_noun():
    # Lead alone or a lead + non-UI subject is a question, not a screen lookup.
    for q in ("show me how a transformer works", "show me a flowchart of oauth",
              "where is Paris"):
        assert parse(q).kind == "answer", q
    for q in ("show me the button to export a PDF", "where is the settings icon",
              "highlight the search field"):
        assert parse(q).kind == "ground", q


def test_open_app_suffix_routes_to_installed_app():
    apps = {"notes": "Notes", "safari": "Safari"}
    assert parse("open notes.app", apps=apps) == Intent("open_app", "notes")
    assert parse("open Safari.app", apps=apps) == Intent("open_app", "Safari")
    assert parse("open github.com", apps=apps) == Intent("open_site", "github.com")
    # Not an installed app: the .app name falls through to the domain check.
    assert parse("open notes.app", apps={}).kind == "open_site"


def test_list_apps(monkeypatch, tmp_path):
    (tmp_path / "Foo.app").mkdir()
    (tmp_path / "Bar Baz.app").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    # Missing dirs are skipped silently.
    assert list_apps((str(tmp_path), str(tmp_path / "nope"))) == {"foo": "Foo", "bar baz": "Bar Baz"}
    # "~" expands via Path.home().
    home = tmp_path / "home"
    (home / "Applications" / "Baz.app").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    assert list_apps(("~/Applications",)) == {"baz": "Baz"}


def test_pick_key():
    assert acks.pick_key("open_app") == "open"
    assert acks.pick_key("open_site") == "open"
    assert acks.pick_key("ground") == "look"
    assert acks.pick_key("git_diff") == "diff"
    assert acks.pick_key("clear") == "clear"
    assert acks.pick_key("answer") == "answer"
    assert acks.pick_key("zzz") == "generic"


def test_ack_cache_fallback():
    cache = acks.AckCache()
    cache.prerender()  # no synth: quiet no-op
    assert cache.get("open") == ("Of course.", None, 0)
    # Unknown key falls back to the generic phrase.
    assert cache.get("nope") == ("On it.", None, 0)

    calls: list[str] = []

    def synth(text):
        calls.append(text)
        return b"pcm:" + text.encode(), 24000

    cache = acks.AckCache(synth)
    cache.prerender()
    assert cache.get("open") == ("Of course.", b"pcm:Of course.", 24000)
    assert sorted(calls) == sorted(acks.PHRASES.values())

    def broken(text):
        raise RuntimeError("tts down")

    cache = acks.AckCache(broken)
    cache.prerender()  # must not raise
    assert cache.get("look") == ("Having a look.", None, 0)


def test_lean_tools_allowlist():
    argv = lean_argv("sonnet", "system prompt", "WebSearch,WebFetch", "claude")
    i = argv.index("--tools")
    assert argv[i + 1] == "WebSearch,WebFetch"
    assert argv[-2:] == ["--effort", "low"]
    empty = lean_argv("sonnet", "system prompt", "", "claude")
    assert empty[empty.index("--tools") + 1] == ""
    with pytest.raises(ValueError):
        lean_argv("sonnet", "system prompt", "Bash", "claude")
    with pytest.raises(ValueError):
        lean_argv("sonnet", "system prompt", "WebSearch,Edit", "claude")


def test_conversations_lru():
    made: list[object] = []
    closed: list[object] = []

    class FakeSession:
        def __init__(self):
            made.append(self)

        def close(self):
            closed.append(self)

    convs = Conversations(FakeSession)
    got = {i: convs.get(f"c{i}") for i in range(4)}
    assert len(made) == 5  # one session per conv_id + one warm spare
    # LRU cap 2: the two oldest were evicted and closed.
    _wait_until(lambda: closed == [got[0], got[1]])
    assert convs.get("c3") is got[3]  # still cached: no new session
    assert len(made) == 5
    convs.get("c2")  # touch: c2 becomes newest
    convs.get("c4")
    _wait_until(lambda: len(closed) == 3)
    assert closed[-1] is got[3]  # the now-oldest is evicted instead
    assert convs.get("c2") is got[2]


def _wait_until(cond, timeout=5.0):
    """Poll so the assertion holds whether close() is synchronous or on a thread."""
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cond()


def test_conversations_default_cap_keeps_two_and_closes_older_in_order():
    made: list[object] = []
    closed: list[object] = []

    class FakeSession:
        def __init__(self):
            made.append(self)

        def close(self):
            closed.append(self)

    convs = Conversations(FakeSession)
    got = [convs.get(f"c{i}") for i in range(5)]
    assert len(made) == 6  # 5 conversation sessions + 1 warm spare
    _wait_until(lambda: closed == got[:3])
    assert convs.get("c3") is got[3] and convs.get("c4") is got[4]
    assert len(made) == 6  # the two newest are still cached
    spare = made[5]
    assert spare not in closed  # eviction never closes the spare
    again = convs.get("c0")  # evicted id: a NEW session, taken from the spare
    assert again not in got and again is spare


def test_lean_argv_preapproves_only_allowlisted_tools():
    argv = lean_argv("sonnet", "p", "WebSearch,WebFetch")
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert "--allowedTools" not in lean_argv("haiku", "p", "")


def test_conversations_prewarm_hands_the_spare_to_the_next_new_conversation():
    made: list[object] = []

    class FakeSession:
        def __init__(self):
            made.append(self)

        def close(self):
            pass

    convs = Conversations(FakeSession)
    convs.prewarm()
    spare = made[0]
    assert convs.get("a") is spare  # the warm one, no cold start
    assert len(made) == 2  # a new spare is already starting for the next conversation
