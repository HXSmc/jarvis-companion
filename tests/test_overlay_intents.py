"""Overlay intents/acks/lean: validation, injection refusal, ack fallback, LRU (no real processes)."""

from __future__ import annotations

import pathlib
import time
from types import SimpleNamespace

import pytest

from mac.voice import acks, media_lookup
from mac.voice.intents import (SESSION_WORDS_MAX, STOP_PHRASES, CardRemoveAsk, Intent,
                               MemoryAsk, WorkspaceAsk, card_matches, card_pick,
                               card_remove_request, card_size_request, card_words,
                               claims_overlay_change, first_form, is_cancel,
                               is_confirm, is_hunk_back, is_no, is_stop, list_apps,
                               media_request, match_card, memory_request, names_one_card,
                               noise_words, open_app, open_site, parse, pick_shaped,
                               session_request, strip_lead,
                               tail_hunk, workspace_request)
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


def test_agent_routing():
    for text, sub in (
        ("research the git history of the code on my screen and tell me what changed", "git"),
        ("any open PRs on jarvis-companion", "gh"),
        ("what does my Obsidian say about pricing", "notes"),
        ("who changed this file last", "git"),
        ("check my inbox", "mail"),
        ("Hey Jarvis, what changed in the last commit?", "git"),
        ("commit and push my changes", "git"),
        ("what's on my calendar today", "calendar"),
        ("what does this function do", "code"),
    ):
        intent = parse(text)
        assert (intent.kind, intent.sub) == ("agent", sub), text
    # Generic questions and the deterministic paths stay where they were.
    assert parse("what is a pull request").kind == "answer"
    assert parse("explain git rebase").kind == "answer"
    assert parse("show the diff since the last merge").kind == "git_diff"
    assert parse("what changed since the last merge").kind == "git_diff"
    assert parse("what time is it") == Intent("clock", "time")
    assert parse("highlight the last debugging session").kind == "look"
    assert parse("what is the capital of France").kind == "answer"
    assert parse("show me the logs").kind != "agent"
    assert parse("tell me about the history of Rome").kind == "answer"
    assert parse("I have an issue with my wifi").kind == "answer"
    assert parse("open safari", apps={"safari": "Safari"}) == Intent("open_app", "safari")


def test_confirm_and_cancel_phrases():
    for text in ("yes", "Yes.", "YES", "Jarvis, yes please", "go ahead!", "Hey Jarvis do it"):
        assert is_confirm(text), text
    for text in ("no", "yes but first tell me the time", "what's on my screen", "yesterday"):
        assert not is_confirm(text), text
    for text in ("stop", "Cancel.", "never mind", "Jarvis, stop"):
        assert is_cancel(text), text
    for text in ("stop the music in five minutes", "no"):
        assert not is_cancel(text), text


def test_agent_ack_keys():
    for sub, text in (("git", "Checking the git history."), ("gh", "Looking at GitHub."),
                      ("notes", "Searching your notes."), ("mail", "Checking your mail."),
                      ("calendar", "Checking your calendar."), ("code", "Looking at that file.")):
        key = acks.pick_key("agent", sub)
        assert (key, acks.PHRASES[key]) == (f"agent_{sub}", text)
    assert acks.pick_key("agent", "zzz") == "agent_code"
    assert acks.progress_key("git") == "progress_git"
    assert acks.progress_key("file") == "progress_file"
    assert acks.progress_key("zzz") == "progress_other"
    assert acks.PHRASES["progress_notes"] != acks.PHRASES["agent_notes"]  # no stutter (R1 ux #4)
    for text in acks.PHRASES.values():
        assert len(text.split()) <= 12, text
        assert not any(w in text.lower() for w in ("certainly", "absolutely", "happy to", "great")), text


def test_ack_cache_fallback():
    cache = acks.AckCache()
    cache.prerender()  # no synth: quiet no-op
    assert cache.get("open") == ("Of course, sir.", None, 0)
    # Unknown key falls back to the generic phrase.
    assert cache.get("nope") == ("On it.", None, 0)

    calls: list[str] = []

    def synth(text):
        calls.append(text)
        return b"pcm:" + text.encode(), 24000

    cache = acks.AckCache(synth)
    cache.prerender()
    assert cache.get("open") == ("Of course, sir.", b"pcm:Of course, sir.", 24000)
    # PHRASES with a template ({n}) are spoken at turn time, never prerendered.
    assert sorted(calls) == sorted(t for t in acks.PHRASES.values() if "{" not in t)

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


def test_confirm_phrases_survive_real_speech_to_text():
    """R1 ux #1: STT commas, "sir" and politeness still count; extra requests still do not."""
    for text in ("Yes, please.", "Yes, go ahead.", "Yeah, do it", "yes sir", "go ahead Jarvis", "Yep.",
                 "Yes, do it, please."):
        assert is_confirm(text), text
    for text in ("yes, but first check the tests", "yes and also delete the branch", "no"):
        assert not is_confirm(text), text
    for text in ("Stop, please", "Cancel it.", "Never mind, Jarvis"):
        assert is_cancel(text), text
    for text in ("no", "No, don't.", "Nope.", "no thanks", "Don't do it", "no, cancel it"):
        assert is_no(text), text
    for text in ("no idea what time it is", "yes"):
        assert not is_no(text), text


def test_hunk_routing():
    for text, arg in (("next change", "next"), ("Jarvis, next change.", "next"),
                      ("go to the next change", "next"), ("next", "next"),
                      ("previous change", "prev"), ("why did this change?", "why"),
                      ("explain this change", "why")):
        intent = parse(text, apps={})
        assert (intent.kind, intent.arg, intent.sub) == ("hunk", arg, arg), text
    # The deterministic paths these could be confused with stay as they were.
    assert parse("what changed in the last commit") == Intent(
        "agent", "what changed in the last commit", "git")
    assert parse("highlight the changes since the last merge").kind == "git_diff"
    assert parse("what's next on my calendar").sub == "calendar"


def test_agent_routing_ignores_ordinary_talk():
    """R1 code #7: everyday words do not send a request to the 120 s agent tier."""
    for text in ("I'm committed to this plan", "tell me about the new iPhone release",
                 "write an email to John about lunch", "what does CI stand for in schools",
                 "how do I write a polite email"):
        assert parse(text).kind != "agent", text
    for text in ("is the CI green on jarvis-companion", "read my latest emails", "any new email from Sara",
                 "list the releases of jarvis-companion"):
        assert parse(text).kind == "agent", text


def test_plain_changes_go_to_git_diff():
    apps: dict[str, str] = {}
    for text in ("highlight the changes", "Hey Jarvis, highlight the changes.",
                 "show the changes", "show me the changes", "what changed",
                 "highlight the exact changes", "highlight my changes"):
        intent = parse(text, apps=apps)
        assert (intent.kind, intent.sub) == ("git_diff", "auto"), text
    intent = parse("highlight the changes since the last merge", apps=apps)
    assert (intent.kind, intent.sub) == ("git_diff", "merge"), intent
    for text in ("highlight the URL", "highlight it", "highlight the last debugging session"):
        assert parse(text, apps=apps).kind == "look", text
    assert parse("what changed in the last commit", apps=apps) == Intent(
        "agent", "what changed in the last commit", "git")


def test_hunk_back_and_tail_and_noise():
    assert is_hunk_back("go back")
    assert is_hunk_back("previous")
    assert is_hunk_back("Go back one.")
    assert not is_hunk_back("go back to the menu")
    assert tail_hunk("put a short note on the word. They work in a card. Next change") == "next"
    assert tail_hunk("Page of his next change") == "next"
    assert tail_hunk("go to the next change") == "next"
    for text in ("what is the next change in the law", "tell me about the weather",
                 "next", ""):
        assert tail_hunk(text) is None, text
    assert noise_words("Jew") == 1
    assert noise_words("the word") == 2
    assert noise_words("uh huh okay") == 3
    for text in ("why?", "what about tomorrow", "yes", "no", "stop",
                 "and the second one is what", ""):
        assert noise_words(text) is None, text


def test_media_request_extraction():
    m = media_request("show me a picture of a dog")
    assert (m.kind, m.subject, m.count, m.article) == ("image", "dog", 1, "a")
    m = media_request("Hey Jarvis, show me a picture of a dog.")
    assert (m.kind, m.subject, m.count, m.article) == ("image", "dog", 1, "a")
    m = media_request("Can you not pull images in general? "
                      "Pull me an image of a dog as a test. Let's see if you can")
    assert (m.kind, m.subject, m.count) == ("image", "dog", 1)
    m = media_request("show me pictures of red foxes please")
    assert (m.kind, m.subject, m.count) == ("image", "red foxes", min(4, media_lookup.MAX_COUNT))
    m = media_request("show me 3 photos of the eiffel tower")
    assert (m.kind, m.subject, m.count) == ("image", "eiffel tower", 3)
    m = media_request("find a video of a rocket launch")
    assert (m.kind, m.subject, m.count) == ("video", "rocket launch", 1)
    # Bare form (D-A1): "a picture of a dog" with no verb.
    for text in ("a picture of a dog", "picture of a dog", "hey jarvis, a picture of a dog"):
        m = media_request(text)
        assert (m.kind, m.subject, m.count) == ("image", "dog", 1), text
    m = media_request("pictures of foxes")
    assert (m.kind, m.subject, m.count) == ("image", "foxes", min(4, media_lookup.MAX_COUNT))
    m = media_request("an image of a cat")
    assert (m.kind, m.subject, m.count) == ("image", "cat", 1)
    m = media_request("show me a diagram of a heart")
    assert (m.kind, m.subject, m.count) == ("image", "heart diagram", 1)
    for text in ("show me a picture of this", "show me a picture of my screen",
                 "show me a picture"):
        m = media_request(text)
        assert (m.kind, m.subject, m.count) == ("image", "", 0), text
    assert media_request("what's the weather") is None
    assert media_request("x" * 301) is None
    # Untrusted transcript junk never reaches the query.
    m = media_request('show me a picture of <b>cat</b> "insource:x" \x07dog')
    for bad in ("<", ">", '"', "insource:", "\x07"):
        assert bad not in m.subject, m.subject
    m = media_request("show me a picture of one two three four five six seven eight nine ten")
    assert len(m.subject.split()) <= 6 and len(m.subject) <= media_lookup.MAX_QUERY
    # A deixis subject ("this page") never searches; the look tier owns the turn.
    assert parse("show me a picture of this page").kind == "look"
    assert media_request("show me a picture of this page").subject == ""


def test_stop_phrases():
    """STOP_PHRASES end the turn (a superset of cancel); talk about stopping does not."""
    for s in STOP_PHRASES + ("Stop.", "Hey Jarvis, stop.", "Jarvis stop",
                             "Stop listening, please", "That's all, thanks"):
        assert parse(s, apps={}).kind == "stop", s
    for s in ("stop the server", "stop the timer in the code", "can you stop the build",
              "stop listening to music on Spotify", "what does stop mean"):
        assert parse(s, apps={}).kind != "stop", s
    # Every _CANCEL_RE form is still a stop; ordinary requests are neither.
    for s in ("stop", "stop it", "Stop.", "cancel", "cancel it", "cancel that",
              "Cancel, please", "never mind", "nevermind", "Never mind, Jarvis",
              "forget it", "Forget it.", "that's enough", "abort", "Abort, sir"):
        assert is_cancel(s) and is_stop(s), s
    for s in ("no", "yes", "stop the music"):
        assert not is_cancel(s) and not is_stop(s), s


def test_card_size_request_and_match():
    cards = [("c1", "flow", card_words("Phone diagram")),
             ("c2", "image", card_words("Dog")),
             ("c3", "image", card_words("Cat"))]
    intent = parse("can you maximise the phone diagram", apps={})
    assert (intent.kind, intent.arg, intent.sub) == ("card_size", "max", "phone diagram")
    assert match_card(intent.sub, cards) == ("c1", "title")
    ask = card_size_request("enlarge the picture")
    assert (ask.action, ask.target) == ("max", "picture")
    assert match_card(ask.target, cards) == ("c3", "kind")  # newest picture, no name given
    ask = card_size_request("make that one bigger")
    assert (ask.action, ask.target) == ("max", "one")
    assert match_card(ask.target, cards) == ("c3", "newest")  # only fillers: the newest card
    for s in ("make it smaller", "back to normal"):
        ask = card_size_request(s)
        assert (ask.action, ask.target) == ("restore", ""), s
    ask = card_size_request("zoom in on the dog picture")
    assert (ask.action, ask.target) == ("max", "dog picture")
    assert match_card(ask.target, cards) == ("c2", "title")
    ask = card_size_request("enlarge the spreadsheet")
    assert ask.action == "max"
    assert match_card(ask.target, cards) is None
    # Mac live run 5: the model titled the card "Smartphone block diagram"; "the phone diagram" is it.
    # A spoken word of 4+ letters may end a compound title word; short words still match whole only.
    phones = [("s1", "flow", card_words("Smartphone block diagram")), ("h1", "image", card_words("Hotdog"))]
    assert match_card("phone diagram", phones) == ("s1", "title")
    assert match_card("dog picture", phones) is None
    # Questions about size, and "expand on that" as an elocution ask, stay with the model.
    for s in ("what's bigger, a whale or a bus", "can you expand on that"):
        assert parse(s, apps={}).kind != "card_size", s


def test_sessions_intent():
    for s, arg in (("what are my sessions doing", ""), ("Hey Jarvis, is anything stuck?", ""),
                   ("what went wrong with the build session", "the build session"),
                   ("session status please", "")):
        intent = parse(s, apps={})
        assert (intent.kind, intent.arg) == ("sessions", arg), (s, intent)
    assert parse("what's the status of the order", apps={}).kind != "sessions"


def test_session_request_parent_cases():
    ask = session_request("start a new session and fix why this session has been going quiet")
    assert (ask.action, ask.ref, ask.words) == ("start", "this",
                                                "fix why this session has been going quiet")
    ask = session_request("start a session on the mini PC to say hello in a file called voice-test")
    assert (ask.action, ask.device, ask.words) == ("start", "mini_pc",
                                                   "say hello in a file called voice-test")
    ask = session_request("open a new session about the login bug")
    assert ask.action == "start"
    ask = session_request("start a session on the Mac to x")
    assert (ask.action, ask.device) == ("start", "mac")
    ask = session_request("stop the jarvis overlay session")
    assert (ask.action, ask.ref, ask.name) == ("stop", "name", "jarvis overlay")
    ask = session_request("tell the jarvis overlay session yes")
    assert (ask.action, ask.ref, ask.name, ask.answer) == ("answer", "name", "jarvis overlay", "yes")
    ask = session_request("approve the overlay session")
    assert (ask.action, ask.ref, ask.name, ask.answer) == ("answer", "name", "overlay", "approve")
    ask = session_request("tell it: use the second option")
    assert (ask.action, ask.ref, ask.name, ask.answer, ask.words) == ("answer", "this", "", "text",
                                                                      "use the second option")
    for s in ("stop", "stop it", "stop the server", "what are my sessions doing",
              "start the timer"):
        assert parse(s, apps={}).kind != "session", s
    assert session_request("x" * 400) is None
    ask = session_request("Hey Jarvis, start a session and fix why this session has been going quiet")
    assert (ask.action, ask.ref, ask.words) == ("start", "this",
                                                "fix why this session has been going quiet")
    ask = session_request("start a session and one\ntwo\x07three")
    assert ask.words == "one two three"
    ask = session_request("start a session and " + "x" * 500)
    assert ask is None or len(ask.words) <= SESSION_WORDS_MAX
    # "what is it doing" follows a session: sessions intent with the ref sub.
    intent = parse("what is it doing", apps={})
    assert (intent.kind, intent.arg, intent.sub) == ("sessions", "", "ref")


def test_memory_request_parent_cases():
    ask = memory_request("remember that the side gate is locked")
    assert (ask.action, ask.text) == ("add", "the side gate is locked")
    ask = memory_request("remember this: the wifi password is on the router")
    assert (ask.action, ask.text) == ("add", "the wifi password is on the router")
    ask = memory_request("remember, the printer is out of toner")
    assert (ask.action, ask.text) == ("add", "the printer is out of toner")
    ask = memory_request("from now on I test before pushing")
    assert (ask.action, ask.text) == ("add", "From now on I test before pushing")
    ask = memory_request("note that the printer is out of toner")
    assert (ask.action, ask.text) == ("add", "the printer is out of toner")
    for s in ("what do you remember", "what have you remembered"):
        assert memory_request(s).action == "list", s
    assert memory_request("forget that").action == "forget_last"
    ask = memory_request("forget what I said about the battery")
    assert (ask.action, ask.text) == ("forget_about", "the battery")
    for s in ("forget everything", "forget everything you remember"):
        assert memory_request(s).action == "forget_all", s
    ask = memory_request("remember that when I say notes I mean the Obsidian vault")
    assert ask.text == "when I say notes I mean the Obsidian vault"
    for s in ("can you remember the capital of France", "remember the milk"):
        assert memory_request(s) is None, s
        assert parse(s, apps={}).kind != "memory", s


def test_workspace_request_parent_cases():
    ask = workspace_request("save these pages and overlays as our battery research")
    assert (ask.action, ask.name) == ("save", "battery research")
    ask = workspace_request("open the battery research")
    assert (ask.action, ask.name) == ("open", "battery research")
    ask = workspace_request("open battery", names=("battery research",))
    assert (ask.action, ask.name) == ("open", "battery")
    assert workspace_request("open battery") is None
    assert parse("open battery", apps={}).kind != "workspace"
    ask = workspace_request("open the pages")
    assert ask.action == "open_pages"
    assert parse("open the pages", apps={}).kind == "workspace"
    assert workspace_request("what have I saved").action == "list"
    ask = workspace_request("delete the battery research")
    assert (ask.action, ask.name) == ("delete", "battery research")
    assert workspace_request("open Safari") is None
    assert parse("open Safari", apps={"safari": "Safari"}).kind == "open_app"


def test_session_request():
    # Parent criterion 8 cases.
    ask = session_request("start a new session and fix why this session has been going quiet")
    assert (ask.action, ask.device, ask.ref, ask.name) == ("start", "mini_pc", "this", "")
    assert ask.words == "fix why this session has been going quiet"
    ask = session_request("start a session on the mini PC to say hello in a file called voice-test")
    assert (ask.action, ask.device, ask.ref) == ("start", "mini_pc", None)
    assert ask.words == "say hello in a file called voice-test"
    ask = session_request("open a new session about the login bug")
    assert (ask.action, ask.words) == ("start", "the login bug")
    ask = session_request("start a session on the Mac to x")
    assert (ask.action, ask.device, ask.words) == ("start", "mac", "x")
    ask = session_request("stop the jarvis overlay session")
    assert (ask.action, ask.ref, ask.name, ask.words) == ("stop", "name", "jarvis overlay", "")
    ask = session_request("tell the jarvis overlay session yes")
    assert (ask.action, ask.ref, ask.name, ask.answer) == ("answer", "name", "jarvis overlay", "yes")
    ask = session_request("approve the overlay session")
    assert (ask.action, ask.ref, ask.name, ask.answer) == ("answer", "name", "overlay", "approve")
    ask = session_request("tell it: use the second option")
    assert (ask.action, ask.ref, ask.name, ask.answer) == ("answer", "this", "", "text")
    assert ask.words == "use the second option"
    for s in ("stop", "stop it", "stop the server", "what are my sessions doing",
              "start the timer"):
        assert parse(s, apps={}).kind != "session", s
    assert session_request("start a session about " + "x" * 400) is None
    # Wake word stripped, original case of the words kept.
    ask = session_request("Hey Jarvis, start a session about Fix the Login Bug")
    assert (ask.action, ask.words) == ("start", "Fix the Login Bug")
    # Newlines and control chars collapse to one clean line of words.
    ask = session_request("start a session about fix the\nbattery\x07drain now")
    assert ask.words == "fix the battery drain now"
    # A 500-char words value cannot pass the 300-char input cap; the cap constant stands.
    assert session_request("start a session about " + "x" * 500) is None
    assert SESSION_WORDS_MAX == 300


def test_memory_request():
    ask = memory_request("remember that when I say notes I mean the Obsidian vault")
    assert (ask.action, ask.text) == ("add", "when I say notes I mean the Obsidian vault")
    assert memory_request("remember this: the gate code is 2517").text == "the gate code is 2517"
    assert memory_request("remember this, the gate code is 2517").text == "the gate code is 2517"
    assert memory_request("remember, the bakery closes at noon").text == "the bakery closes at noon"
    assert memory_request("from now on spell check my notes") == MemoryAsk(
        "add", "From now on spell check my notes")
    assert memory_request("note that the wifi is flaky upstairs").text == "the wifi is flaky upstairs"
    assert memory_request("what do you remember").action == "list"
    assert memory_request("what have you remembered").action == "list"
    assert memory_request("forget that").action == "forget_last"
    ask = memory_request("forget what I said about the gate code")
    assert (ask.action, ask.text) == ("forget_about", "the gate code")
    assert memory_request("forget everything").action == "forget_all"
    assert memory_request("forget everything you remember").action == "forget_all"
    for s in ("can you remember the capital of France", "remember the milk"):
        assert memory_request(s) is None, s
        assert parse(s, apps={}).kind != "memory", s


def test_open_app_wins_over_saved_workspace_word():
    apps = {"notes": "Notes", "safari": "Safari"}
    names = ("battery notes",)
    assert parse("open notes", apps=apps, workspace_names=names) == Intent("open_app", "notes")
    assert parse("open github.com", apps=apps, workspace_names=("github.com research",)).kind == "open_site"
    # no installed app of that name: the subset rule still asks which workspace
    assert parse("open battery", apps=apps, workspace_names=names) == Intent("workspace", "open")
    assert parse("open the battery notes", apps=apps, workspace_names=names).kind == "workspace"


def test_workspace_request():
    ask = workspace_request("save these pages and overlays as our battery research")
    assert (ask.action, ask.name) == ("save", "battery research")
    ask = workspace_request("open the battery research")
    assert (ask.action, ask.name) == ("open", "battery research")
    ask = workspace_request("open battery", ("battery research",))
    assert (ask.action, ask.name) == ("open", "battery")
    assert workspace_request("open battery") is None
    assert parse("open battery", apps={}).kind != "workspace"
    ask = workspace_request("open the pages")
    assert (ask.action, ask.name) == ("open_pages", "")
    assert parse("open the pages", apps={}).kind == "workspace"
    assert workspace_request("what have I saved") == WorkspaceAsk("list", "")
    ask = workspace_request("delete the battery research")
    assert (ask.action, ask.name) == ("delete", "battery research")
    assert workspace_request("open Safari") is None
    assert parse("open Safari", apps=APPS).kind != "workspace"


def test_round7_live_phrasings():
    """Mac run r2 transcripts that missed their fixed path (F6, 41 stop, 49 Secrets)."""
    from mac.voice.intents import parse
    for text in ("Hi, everything", "Hide the gods", "hi the cards", "hide, everything"):
        assert parse(text, apps={}).kind == "hide", text
    for text in ("Hi", "hi there", "hi Jarvis how are you"):
        assert parse(text, apps={}).kind != "hide", text
    ask = session_request("Stop the session called Voice Test, write hello")
    assert (ask.action, ask.ref, ask.name) == ("stop", "name", "voice test, write hello")
    assert session_request("stop session named build runner").name == "build runner"
    for text in ("Open the note Projects in the Secrets folder",
                 "Read my note called Projects, in the Secrets folder of my notes",
                 "show me my secrets note", "what's in my secrets"):
        assert parse(text, apps={"notes": "Notes"}).kind == "vault_secrets", text
    for text in ("what is the secret of success", "tell me a secret"):
        assert parse(text, apps={}).kind != "vault_secrets", text


LEAD_INS = ("okay", "ok", "alright", "all right", "right", "so", "well", "um", "uh",
            "hey", "now", "please", "jarvis", "can you", "could you", "would you",
            "will you", "can we", "could we", "i want you to", "i'd like you to",
            "i need you to", "go ahead and", "just")
LEAD_PAIRS = ("okay so", "hey jarvis could you please", "alright, can you", "um, just",
              "go ahead and")


def test_strip_lead_table():
    """Lead-ins come off whole-word before every fast path; nothing would remain -> unchanged."""
    for lead in LEAD_INS:
        assert strip_lead(lead) == lead, lead  # "okay" alone stays "okay"
    phrases = ("stop", "enlarge that", "make it smaller", "remove that one", "hide",
               "show them", "clear", "next change", "previous change",
               "what are my sessions doing", "start a session to fix the build",
               "remember that I park on level 3", "forget that",
               "what workspaces do I have")
    for lead in LEAD_INS + LEAD_PAIRS:
        for phrase in phrases:
            assert parse(lead + " " + phrase, apps=()) == parse(phrase, apps=()), \
                lead + " " + phrase
        ask = media_request(lead + " show me a picture of saturn")
        assert (ask.kind, ask.subject, ask.count) == ("image", "saturn", 1), lead
    # Trailing please/for me/now/sir after the fixed-phrase commands keeps the kind.
    for text, kind in (("hide please", "hide"), ("hide for me", "hide"), ("hide now", "hide"),
                       ("hide sir", "hide"), ("clear please", "clear"), ("clear for me", "clear"),
                       ("clear now", "clear"), ("clear sir", "clear"),
                       ("show them please", "show"), ("show them for me", "show"),
                       ("show them now", "show"),
                       ("enlarge that please", "card_size"), ("enlarge that for me", "card_size"),
                       ("enlarge that now", "card_size"), ("enlarge that sir", "card_size")):
        assert parse(text, apps=()).kind == kind, text


def test_lead_in_cases():
    """The round-8 phrasings that missed their fast path (F6 Saturn, lead-ins everywhere)."""
    ask = media_request("Okay, can you show me a picture of Saturn")
    assert (ask.kind, ask.subject, ask.count) == ("image", "saturn", 1)
    assert parse("can you enlarge that", apps=()).kind == "card_size"
    assert parse("okay stop", apps=()).kind == "stop"
    assert media_request("That's nice. Can you show me a picture of Saturn").subject == "saturn"
    assert parse("That's nice. Clear", apps=()).kind == "clear"
    # Clock and hunk stay whole-utterance: a clock question after small talk is a question.
    assert parse("Hi, I'm stuck. What time is it", apps=()).kind == "answer"
    assert first_form("That's nice. Remember that I park on level 3", memory_request) is not None


def test_filler_mid_sentence_unchanged():
    """Fillers mid-sentence are content: the model kinds still see the whole transcript."""
    for text in ("what is so special about Saturn", "is it right to say now or never",
                 "how well does it work", "right click the button"):
        assert parse(text, apps=()) == Intent("answer", text), text


def test_stripper_never_makes_a_yes():
    """Politeness stripping never manufactures a confirmation."""
    for text in ("okay yes", "right yes", "alright", "okay", "so yes", "yes okay"):
        assert not is_confirm(text), text
    assert is_confirm("yes please")
    assert is_no("okay no")
    assert is_stop("okay stop")
    assert is_cancel("um, cancel that")


def test_card_remove_collisions():
    """One-card removal never steals another kind's phrases (base kinds hard-coded)."""
    for text in ("clear", "clear everything", "dismiss them", "dismiss everything",
                 "close everything", "remove the cards", "get rid of the cards",
                 "dismiss it", "dismiss that", "close it", "close that", "close this"):
        assert parse(text, apps=()).kind == "clear", text
    for text in ("get rid of it", "hide", "put it away"):
        assert parse(text, apps=()).kind == "hide", text
    for text in ("show them", "bring them back"):
        assert parse(text, apps=()).kind == "show", text
    for text in ("remove that", "remove it", "remove that one", "close this card",
                 "dismiss that card", "get rid of that one", "take that away"):
        assert parse(text, apps=()) .kind == "card_remove", text
        assert parse(text, apps=()).arg == "pointer", text
    for text, target in (("remove the dog picture", "dog picture"),
                         ("close the phone diagram", "phone diagram"),
                         ("get rid of the changes card", "changes"),
                         ("take away the picture", "picture")):
        intent = parse(text, apps=())
        assert (intent.kind, intent.arg, intent.sub) == ("card_remove", "target", target), text
    # A trailing please/for me/now/sir never becomes part of the target.
    for text in ("remove that please", "remove it please", "remove that for me",
                 "remove that now", "remove that card please", "dismiss that card please",
                 "close this card sir"):
        assert parse(text, apps=()).kind == "card_remove", text
        assert parse(text, apps=()).arg == "pointer", text
    intent = parse("remove the dog picture for me", apps=())
    assert (intent.kind, intent.arg, intent.sub) == ("card_remove", "target", "dog picture")
    assert parse("forget that", apps=()).kind == "memory"
    assert parse("delete the battery research", apps=(),
                 workspace_names=("battery research",)).kind == "workspace"
    assert parse("stop the voice-test session", apps=()).kind == "session"
    for text in ("make it smaller", "restore it"):
        assert parse(text, apps=()).kind == "card_size", text
    # Targets owned by today's kinds keep them (base kinds measured, hard-coded).
    assert parse("remove the highlight", apps=()) == Intent("look", "remove the highlight")
    for text in ("remove the highlights", "remove the boxes", "close the notes app"):
        assert parse(text, apps=()) == Intent("answer", text), text
    # A names-only target ("close Safari") is NOT card_remove in parse (base kind kept);
    # card_remove_request still returns the ask so the service can upgrade it later.
    assert parse("close Safari", apps=()) == Intent("answer", "close Safari")
    assert card_remove_request("close Safari") == CardRemoveAsk("safari", False, False)


def test_card_remove_match_order():
    """card_matches: match_card's logic over lists, ids newest first, duplicate ids once."""
    cards = [("a1", "flow", ("smartphone", "block", "diagram")),
             ("b2", "image", ("dog",)), ("c3", "bar", ("changes",))]
    assert card_matches("the dog picture", cards) == ("title", ["b2"])
    assert card_matches("the phone diagram", cards) == ("title", ["a1"])
    assert card_matches("the picture", cards) == ("kind", ["b2"])
    assert card_matches("that one", cards) == ("newest", ["c3"])
    assert card_matches("the changes card", cards) == ("title", ["c3"])
    assert card_matches("the cat", cards) is None
    images = [("i1", "image", ("dog",)), ("i2", "image", ("dog", "park"))]
    # jfollow6: the ranked title branch keeps only the best tier, so the exact
    # "dog" title beats the longer "dog park" one (pinned test changed with the default).
    assert card_matches("the dog picture", images) == ("title", ["i1"])
    assert card_matches("the picture", images) == ("kind", ["i2", "i1"])
    dup = [("i2", "image", ("cat",)), ("i1", "image", ("dog",)), ("i2", "image", ("dog", "park"))]
    assert card_matches("the dog picture", dup) == ("title", ["i1"])
    assert match_card("the changes card", cards) == ("c3", "kind")  # unchanged


def test_card_remove_leaves_place_and_topic_asks_alone():
    """A target with a place or topic stays today's kind (base kinds measured at 2d585fc)."""
    assert parse("remove the picture from my notes", apps=()).kind == "agent"
    assert parse("remove the photo from my email draft", apps=()).kind == "agent"
    assert parse("dismiss the card about my calendar", apps=()).kind == "agent"
    for text in ("close the diagram in Preview", "remove the image from the slide",
                 "take down the diagram on the website"):
        assert parse(text, apps=()).kind == "answer", text
    assert parse("close the picture of the dog", apps=()).kind == "card_remove"


def test_card_matches_rank():
    """jfollow6: the title branch ranks hits; only the best (suffix_hits, extra_words)
    tier survives, newest-first inside it (parent criterion 5)."""
    nested = [("j1", "info", ("jarvis", "companion", "capabilities")),
              ("j2", "info", ("jarvis", "capabilities"))]
    # (a) "jarvis capabilities": the exact title "jarvis capabilities" wins over the longer
    # nested one ("companion" is an extra word nobody said).
    assert card_matches("jarvis capabilities", nested) == ("title", ["j2"])
    assert card_matches("jarvis capabilities", list(reversed(nested))) == ("title", ["j2"])
    assert match_card("jarvis capabilities", nested) == ("j2", "title")
    # (b) A word only the longer title has picks it alone.
    assert card_matches("the companion capabilities", nested) == ("title", ["j1"])
    assert match_card("the companion capabilities", nested) == ("j1", "title")
    # (c) A word both titles share keeps both (newest first).
    assert card_matches("jarvis", nested) == ("title", ["j2"])
    assert match_card("jarvis", nested) == ("j2", "title")
    # (d) An exact name word beats a 4-letter suffix hit ("smartphone" ends with "phone").
    phones = [("p1", "flow", ("phone", "main", "components")),
              ("p2", "flow", ("smartphone", "architecture"))]
    assert card_matches("the phone diagram", phones) == ("title", ["p1"])
    assert match_card("the phone diagram", phones) == ("p1", "title")
    # (e) Equal-rank titles both stay, newest first (and match_card takes the newest).
    equal = [("e1", "info", ("jarvis", "companion", "capabilities")),
             ("e2", "info", ("jarvis", "overlay", "capabilities"))]
    assert card_matches("the jarvis capabilities", equal) == ("title", ["e2", "e1"])
    assert match_card("the jarvis capabilities", equal) == ("e2", "title")
    # Advice negative: titles differing only before a shared suffix stay ambiguous.
    wired = [("n1", "flow", ("phone", "network", "diagram")),
             ("n2", "flow", ("phone", "wiring", "diagram"))]
    assert card_matches("the phone diagram", wired) == ("title", ["n2", "n1"])


def test_clear_verb_one_card():
    """jfollow6: "clear" removes one NAMED card; its pointer and whole-overlay forms
    stay the plural clear (parent criterion 9)."""
    assert parse("clear the capabilities card", apps=()) == Intent(
        "card_remove", "target", "capabilities")
    for text in ("remove the jarvis capabilities card from the overlay",
                 "clear the jarvis capabilities card from the screen"):
        assert parse(text, apps=()) == Intent(
            "card_remove", "target", "jarvis capabilities"), text
    for text in ("clear", "clear the card", "clear the cards", "clear everything",
                 "clear the screen", "clear the overlay", "clear it"):
        assert parse(text, apps=()).kind == "clear", text
    for text in ("clear that", "clear this card"):
        assert parse(text, apps=()).kind == "answer", text
    assert card_remove_request("clear that") is None
    assert card_remove_request("clear this card") is None
    assert parse("remove the picture from my notes", apps=()).kind != "card_remove"


def test_card_pick_table():
    """jfollow6: card_pick/pick_shaped over a two-card pick (parent criterion 10)."""
    names = [("jarvis", "companion", "capabilities"), ("jarvis", "overlay", "capabilities")]
    for text, want in (("the companion one", [0]),
                       ("the jarvis overlay capabilities card", [1]),
                       ("the second one", [1]), ("second", [1]),
                       ("number two", [1]), ("the last one", [1]),
                       ("the first one", [0]),
                       ("both", [0, 1]), ("both of them", [0, 1]), ("all of them", [0, 1]),
                       ("okay, the companion one please", [0]),
                       ("the purple one", None), ("yes", None),
                       ("what time is it", None), ("jarvis capabilities", None),
                       ("the fourth one", None)):
        assert card_pick(text, names) == want, text
    for text, want in (("the purple one", True), ("the third card", True),
                       ("the fourth one", True), ("both", True),
                       ("yes", False), ("what time is it", False),
                       ("jarvis capabilities", False)):
        assert pick_shaped(text) is want, text


def test_claims_overlay_change():
    """jfollow6: a model line claiming an overlay change it cannot make (criterion 17)."""
    for line in ("Understood, sir, clearing the capabilities card from the overlay now.",
                 "I've removed the card.", "Closing that card now.",
                 "Done, the card is gone, sir.", "Hiding the cards now, sir.",
                 "I'll enlarge the diagram card."):
        assert claims_overlay_change(line) is True, line
    for line in ("I can't remove cards myself.", "Say remove, then the card's name.",
                 "The card shows the removed lines.",
                 "Clearing up the confusion: Saturn has 146 moons.",
                 "I removed the duplicate line from your file."):
        assert claims_overlay_change(line) is False, line


def test_names_one_card():
    """jfollow6: names_one_card sees one card in an ask, not the overlay (criterion 19)."""
    for text in ("remove the jarvis thing", "clear the capabilities card from the overlay",
                 "the jarvis capabilities card"):
        assert names_one_card(text) is True, text
    for text in ("close the overlay", "dismiss the overlay", "remove these",
                 "clear everything", "remove the cards"):
        assert names_one_card(text) is False, text


def test_names_one_card_whole_overlay():
    """jfollow6 round 2: a whole-overlay ask with a modifier or particle names no one card,
    so C3 never drops the model clear it asked for; "clear every ..." stays a model ask."""
    for text in ("clear the whole screen", "clear up the screen", "clear the entire overlay",
                 "dismiss the whole overlay", "close down the overlay", "clear every single card",
                 "remove every card", "close each card", "clear both cards"):
        assert names_one_card(text) is False, text
    for text in ("clear every single card", "clear each card", "clear the whole overlay"):
        assert parse(text, apps={}).kind == "answer", text  # base kind: never a one-card remove


def test_card_help_parse():
    """jfollow6: "can you remove a single card" is a how-to question (criterion 16)."""
    for text in ("Can you not remove a single card", "can you remove one card",
                 "can you remove a single card", "can't you remove one card",
                 "how do I remove a card", "Hey Jarvis, could you remove one card?",
                 "is there a way to remove a card from the overlay"):
        assert parse(text, apps=()) == Intent("card_help"), text
    assert parse("can you remove the cat card", apps=()) == Intent(
        "card_remove", "target", "cat")
    assert parse("can you remove cards", apps=()).kind != "card_help"
    assert parse("remove one card", apps=()) == Intent("card_remove", "target", "one")


def test_sessions_topic_table():
    """jfollow6: wider read-only sessions questions (criterion 21, Design D)."""
    for text in ("what are my sessions doing", "what's running on my Jarvis Companion",
                 "what sessions are on my driver's companion",
                 "sessions on my drivers companion",
                 "how are the sessions on my Jarvis Companion",
                 "any sessions running", "show me my sessions",
                 "what's the status of my sessions",
                 "Hey Jarvis, what's running on the mini PC"):
        assert parse(text, apps=()) == Intent("sessions", "", ""), text
    for text, want in (("start a new session about my sessions list", "session"),
                       ("stop the voice-test session", "session"),
                       ("remember that my sessions start at nine", "memory"),
                       ("what is a session cookie", "answer"),
                       ("how do sessions work in flask", "answer"),
                       ("what's running in this terminal", "answer")):
        assert parse(text, apps=()).kind == want, text
