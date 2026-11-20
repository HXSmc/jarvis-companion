"""jauto3 - long web links on a clean turn (2A), spoken note names find the note (3A)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from mac.voice import intent_pick, intents, note_find  # noqa: E402

A = {"safari": "Safari", "notes": "Notes", "maps": "Maps", "music": "Music"}
BENCH = "Brain/Research/llm-switch-local-vs-agents-benchmark-2026-11-18.md"


@pytest.fixture(autouse=True)
def _fresh_scan():
    note_find.clear_cache()
    yield
    note_find.clear_cache()
    from gateway import overlay as _ov
    _ov._titles_seen.clear()  # round 1 (S1): the flag is per conv, tests reuse conv ids


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "Brain" / "Research").mkdir(parents=True)
    (v / "Brain" / "Research" / "llm-switch-local-vs-agents-benchmark-2026-11-18.md").write_text("x")
    (v / "Brain" / "Research" / "llm-local-models.md").write_text("x")
    (v / "Projects").mkdir()
    (v / "Projects" / "shopping-ideas.md").write_text("x")
    (v / "Secrets").mkdir()
    (v / "Secrets" / "Projects.md").write_text("x")
    (v / ".obsidian").mkdir()
    (v / ".obsidian" / "x.md").write_text("x")
    outside = tmp_path / "outside"  # real files the symlinks point at
    outside.write_text("x")
    (v / "Brain" / "out.md").symlink_to(outside)
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "inner.md").write_text("x")
    (v / "Brain" / "linked").symlink_to(outside_dir, target_is_directory=True)
    return v


def test_recency_asks_keep_agent_route(vault):
    """jauto3b: "latest" must not loosely match a "latent" note; recency asks reach the agent."""
    (vault / "Research").mkdir()
    (vault / "Research" / "latent-space-models.md").write_text("x")
    (vault / "Research" / "daily-2026-11-19.md").write_text("x")
    names = note_find.scan(str(vault))
    for text in ("open the latest note", "open my last note", "open the most recent note",
                 "open the newest note", "open the note I wrote yesterday"):
        assert intents.parse(text, apps=A, note_names=names) == intents.Intent("agent", text, "notes"), text
    got = intents.parse("open the note latent spaces", apps=A, note_names=names)
    assert got.kind == "note_open"
    assert note_find.find(names, got.arg, literal=True) == ["Research/latent-space-models.md"]
    assert note_find.find(names, "latest") == []


# --- jauto3b: recency and filler asks reach the agent notes route (B1), "about"/"called" query (B2) --


def test_jauto3b_recency_and_filler_routes(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Research").mkdir(parents=True)
    (vault / "Research" / "latent-space-models.md").write_text("x")
    (vault / "Daily").mkdir()
    (vault / "Daily" / "2026-11-19.md").write_text("x")
    (vault / "pricing-ideas.md").write_text("x")
    names = note_find.scan(str(vault))
    asks = ("open the latest note", "open my last note", "open the most recent note",
            "open the newest note", "open the note I wrote yesterday", "open the note I wrote today",
            "open the note about pricing", "open yesterday's note")
    for text in asks:
        assert intents.parse(text, apps=A, note_names=names) == intents.Intent("agent", text, "notes"), text
        assert intents.parse(text, apps=A, note_names=None) == intents.Intent("agent", text, "notes"), text
    assert intents.parse("open the note latent spaces", apps=A, note_names=names).kind == "note_open"
    assert intents.parse("open notes", apps=A, note_names=names).kind == "open_app"
    assert intents.parse("open safari", apps=A, note_names=names).kind == "open_app"
    assert intents.parse("open the latest note in Secrets", apps=A, note_names=names).kind \
        not in ("open_app", "note_open")


def test_most_and_wrote_left_stop(tmp_path):
    """jauto3b r1: 'most' and 'wrote' left STOP; the recency asks still reach the agent."""
    vault = tmp_path / "vault"
    (vault / "Archive").mkdir(parents=True)
    (vault / "Archive" / "most-wanted.md").write_text("x")
    names = note_find.scan(str(vault))
    for text in ("open the most recent note", "open the note I wrote yesterday"):
        assert intents.parse(text, apps=A, note_names=names) == intents.Intent("agent", text, "notes"), text


# --- criterion 6: a typed query/hash URL opens, a spoken-looking one still goes to the agent --------


def test_typed_query_url_opens():
    got = intents.parse("open https://example.com/search?q=a&b=c", apps=A)
    assert got == intents.Intent("open_site", "https://example.com/search?q=a&b=c")
    ran = []
    out = intents.open_site(got.arg, run=lambda argv, **k: ran.append(list(argv)))
    assert out == "the link on example.com" and ran == [["open", "https://example.com/search?q=a&b=c"]]
    long_path = "https://example.com/" + ("ab/" * 100) + "cd"  # 300-char path, plain letters and slashes
    got = intents.parse("open " + long_path, apps=A)
    assert got.kind == "open_site"
    ran = []
    assert intents.open_site(got.arg, run=lambda argv, **k: ran.append(list(argv))) == "the link on example.com"
    assert ran == [["open", long_path]]
    got = intents.parse("open http://example.com/x?y=1", apps=A)
    assert got.kind == "open_site"
    ran = []
    assert intents.open_site(got.arg, run=lambda argv, **k: ran.append(list(argv))) == "the link on example.com"
    assert ran == [["open", "https://example.com/x?y=1"]]  # jauto3: http upgrades to https
    assert got.arg == "https://example.com/x?y=1"  # parse upgrades; open_site itself refuses http (overlay_intents pin)
    assert intents.open_site("http://example.com/x?y=1", run=lambda argv, **k: pytest.fail("must not run")) is None
    for url in ("https://user@example.com/x", "https://example.com:8080/x", "https://example.com/a b",
                'https://example.com/"x"', "https://exa mple.com", "https://example.com/" + "a" * 1981,
                "https://evil.example; rm -rf ~", "https://example.com/café"):
        assert intents.parse("open " + url, apps=A).kind == "agent", url
        assert intents.open_site(url, run=lambda argv, **k: pytest.fail("must not run")) is None, url
    assert intents.link_label("https://example.com") == "https://example.com"
    assert intents.site_row_arg("example.com/search?q=1") == "example.com"
    assert intents.site_row_arg("example.com/docs") == "example.com/docs"
    assert intents.site_row_arg("https://example.com/" + "a" * 80) == "example.com"


# --- criterion 9: the owner's transcripts reach the benchmark note ----------------------------------


SPOKEN = [
    "Open the No LLM Switch localal versus agents",
    "Open the No LLM Switch Local versus Agents Benchmark in Brain Research",
    "Open the No LAM Switch Local versus Agents' Benchmark 2026 1118",
    "Open the node. LLM Switch Local Versus Agents Benchmark, from my brain research folder",
    "open the llm switch benchmark note",
    "open the note llm-switch-local-vs-agents-benchmark-2026-11-18",
    "Open the notes LLM switch local versus agents benchmark",
]


@pytest.mark.parametrize("utterance", SPOKEN)
def test_spoken_note_transcripts(utterance, vault):
    names = note_find.scan(str(vault))
    got = intents.parse(utterance, apps=A, note_names=names)
    assert got.kind == "note_open"
    assert note_find.find(names, got.arg, literal=got.sub == "literal") == [BENCH]


# --- criterion 10: ties, misses, secrets and a saved workspace keep their old behavior -------------


def test_note_ties_and_misses(vault):
    (vault / "Misc").mkdir()
    (vault / "Misc" / "made-in-china.md").write_text("x")
    (vault / "meeting.md").write_text("x")
    names = note_find.scan(str(vault))
    got = intents.parse("open the llm note", apps=A, note_names=names)
    assert got.kind == "note_open"
    assert note_find.find(names, got.arg, literal=got.sub == "literal") == [
        "Brain/Research/llm-local-models.md", BENCH]  # the tie keeps both
    got = intents.parse("open the note made in china", apps=A, note_names=names)
    assert note_find.find(names, got.arg, literal=got.sub == "literal") == ["Misc/made-in-china.md"]
    for text in ("open the note banana bread", "open the latest note", "open my last note",
                 "open the note about pricing"):  # round 1 (U1): no match -> today's route (the agent)
        got = intents.parse(text, apps=A, note_names=names)
        assert got == intents.parse(text, apps=A) and got.kind != "note_open", text
    assert intents.parse("open the note banana bread", apps=A, note_names=names).kind == "agent"
    for text in ("open the node modules folder", "open the no thanks"):
        assert intents.parse(text, apps=A, note_names=names).kind == intents.parse(text, apps=A).kind
    got = intents.parse("open the note projects", apps=A, note_names=names)
    hits = note_find.find(names, got.arg, literal=got.sub == "literal")
    assert "Secrets/Projects.md" not in hits and "Secrets/Projects.md" not in names
    assert intents.parse("open meeting notes", apps=A, workspace_names=("meeting notes",),
                         note_names=names).kind == "workspace"  # a saved name wins over the note step
    assert intents.parse("open the note in the Secrets folder", apps={}, note_names=names).kind == "vault_secrets"
    assert intents.parse("open notes.app", apps={}, note_names=names).kind == "open_site"
    assert intents.parse("open notes app", apps=A, note_names=names).kind == "open_app"


# --- criterion 11: note words alone never change today's kinds -------------------------------------


KEEP_ROWS = [
    ("open notes", A),
    ("open the notes", A),
    ("open Notes app", A),
    ("open my notes folder", A),
    ("open no", A),
    ("open stop", A),
    ("open safari", A),
    ("open maps", {}),
    ("open a session", A),
    ("open sessions on the Mac", A),
    ("open notes.app", {}),
    ("open notes app", {}),
    ("open note app", A),  # jauto3 builder: the Notes app ask, never "a note called app"
    ("open the note app", A),
    ("no", A),
    ("stop", A),
    ("No.", A),
    ("open the note in the Secrets folder", {}),
]


@pytest.mark.parametrize("text,apps", KEEP_ROWS)
def test_note_words_keep_old_kinds(text, apps, vault):
    names = note_find.scan(str(vault))
    assert intents.parse(text, apps=apps, note_names=names).kind == intents.parse(text, apps=apps).kind


# --- criterion 12: the scan is bounded, symlink-free and secrets-free ------------------------------


def test_note_scan_bounds(vault, tmp_path, monkeypatch):
    names = note_find.scan(str(vault))
    assert "Secrets/Projects.md" not in names and ".obsidian/x.md" not in names
    assert "Brain/out.md" not in names and not any(n.startswith("Brain/linked") for n in names)
    for n in (BENCH, "Brain/Research/llm-local-models.md", "Projects/shopping-ideas.md"):
        assert n in names
    assert note_find.scan(None) == ()
    assert note_find.scan(str(tmp_path / "missing")) == ()
    deep = tmp_path / "deep"
    five = deep / "d1/d2/d3/d4/d5"
    five.mkdir(parents=True)
    (five / "six.md").write_text("x")  # depth 6: kept
    (five / "d6").mkdir()
    (five / "d6" / "seven.md").write_text("x")  # depth 7: never reached
    assert note_find.scan(str(deep)) == ("d1/d2/d3/d4/d5/six.md",)
    assert note_find.FILES_MAX == 20000
    many = tmp_path / "many"
    many.mkdir()
    for i in range(8):
        (many / f"n{i}.md").write_text("x")
    monkeypatch.setattr(note_find, "FILES_MAX", 5)
    assert len(note_find.scan(str(many))) == 5  # stands for the 20000 bound
    assert note_find.tokens("Agents' Benchmark 2026 versus v") == ["agents", "benchmark", "vs", "vs"]
    assert note_find.speak_name(BENCH) == "llm switch local vs agents benchmark"  # round 1 (U3): no date words
    assert note_find.speak_name("x/2026-11-18.md") == "2026 11 18"  # only digits: kept


# --- criterion 16: note_open is word-only and both modules import clean -----------------------------


def test_word_only_has_note_open():
    assert "note_open" in intent_pick.WORD_ONLY and "note_open" not in intent_pick.PICKS
    for code in ("import mac.voice.intents, mac.voice.note_find",
                 "import mac.voice.note_find, mac.voice.intents"):  # no import cycle either way
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True)
        assert r.returncode == 0, r.stderr.decode()


# --- gateway wiring: the hunks above through the real utterance pipeline ------------------

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from gateway import overlay  # noqa: E402
from mac.voice import agent_policy, auto_policy  # noqa: E402
from test_overlay_agent import (  # noqa: F401,E402
    Clock, FakeAgent, client, reset, result, setup)
from test_overlay_jauto import H, ctx, v, _deep, _switch_off  # noqa: F401,E402
from test_overlay_jfollow10a import (  # noqa: F401,E402
    LOUD, QUIET, RUN_LINE, _confirm_card, _rows, _steps, auto_on, bash, fresh_auto_state, run_agent)
from test_overlay_openapp import run_turn  # noqa: E402
from test_overlay_service import says, turn  # noqa: F401,E402

LONG = "open https://example.com/" + "/".join(["section-name"] * 13)  # 193 chars, no & or ?


# --- criterion 1: the helper upgrade itself ------------------------------------------------


def test_long_url_helper(H, ctx, auto_on):
    cmd = "open https://example.com/a/" + "b" * 150
    assert auto_policy.long_url_open(cmd, ctx, tainted=False, barge=False, gate_ok=True) == cmd
    assert auto_policy.long_url_open(cmd, ctx, tainted=True, barge=False, gate_ok=True) is None
    assert auto_policy.long_url_open(cmd, ctx, tainted=False, barge=True, gate_ok=True) is None
    assert auto_policy.long_url_open(cmd, ctx, tainted=False, barge=False, gate_ok=False) is None
    _switch_off()
    assert auto_policy.long_url_open(cmd, ctx, tainted=False, barge=False, gate_ok=True) is None
    for bad in ("open https://example.com/" + "c" * 1977,  # one char over LONG_URL_MAX
                f"open https://e.com/x {H}/Documents/a.md",  # a path operand beside the link
                "open -a Terminal https://e.com/x",  # not an openable app
                "open -a ſafari https://e.com/x",  # non-ascii lookalike app name
                "open ftp://e.com/x",
                "open file:///etc/passwd",
                "open 'https://e.com/?a=1&b=2'",  # & is a shell metachar
                "open https://e.com/x && open https://e.com/y",  # two commands
                "/usr/bin/open https://e.com/x"):  # not the bare open builtin
        assert auto_policy.long_url_open(bad, ctx, tainted=False, barge=False, gate_ok=True) is None, bad
    assert auto_policy.TOO_LONG == agent_policy.decide("Bash", {"command": cmd}, ctx).reason


# --- criterion 2: a long web-link open runs auto on a clean turn --------------------------


def test_gateway_long_url_auto_on_clean_turn(client, auto_on):
    pool, events = run_agent(client, bash(LONG), conv_id="j3a")
    assert pool.made[0].responses[0]["behavior"] == "allow"
    assert pool.made[0].responses[0]["updatedInput"]["command"] == LONG
    assert _rows() == ["cmd=open decision=auto reason=ok"]
    assert not _confirm_card(events)
    assert not any(t.endswith("Shall I?") for t in says(events))
    assert says(events).count(RUN_LINE) == 1


# --- criterion 3: the same open is denied on every unsafe turn ---------------------------


@pytest.mark.parametrize("mode", ["marked", "auto_tainted", "titles", "barge", "followup", "off", "timeout"])
def test_gateway_long_url_denied_when_unsafe(mode, client, auto_on, monkeypatch):
    doc = Path.home() / "Documents" / "a.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("x")
    conv, extra = "j3d", {}
    if mode == "auto_tainted":
        overlay._auto_tainted.add(conv)
    elif mode == "titles":  # round 1 (S1): session titles (no doing text) reached this conv: not auto-tainted
        overlay._mark_session_rows(conv, [{"title": "Jarvis: open a long link", "doing": "", "recap": []}])
        assert conv in overlay._titles_seen and conv not in overlay._auto_tainted
    elif mode == "barge":
        extra = dict(followup=True, barge_in=True, levels=LOUD, conf=0.9)
    elif mode == "followup":
        extra = dict(followup=True, levels=QUIET, conf=0.9)
    elif mode == "off":
        _switch_off()
    elif mode == "timeout":  # the policy walk stuck: the Deny stands
        monkeypatch.setattr(overlay, "POLICY_CHECK_S", 0.2)
        monkeypatch.setattr(auto_policy, "long_url_open", lambda *a, **k: time.sleep(0.5) or LONG)
    if mode in ("barge", "followup"):
        (Path.home() / "Desktop" / "ObsidianVault").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("OVERLAY_VAULT", str(Path.home() / "Desktop" / "ObsidianVault"))
    make = bash(f"cat {doc}", LONG) if mode == "marked" else bash(LONG)
    if mode in ("barge", "followup"):
        extra["text"] = "what is in my notes about the deadline"
    pool, events = run_agent(client, make, conv_id=conv, **extra)
    if mode == "marked":  # jauto3 builder: the cat really ran (a long CI home could deny it for length)
        assert _rows()[0] == "cmd=cat decision=auto reason=ok"
    responses = pool.made[0].responses
    assert any(r["behavior"] == "deny"
               and r["message"] == "too long to confirm out loud; split it into smaller steps"
               for r in responses)
    assert not any(r["behavior"] == "allow" and r.get("updatedInput", {}).get("command") == LONG
                   for r in responses)
    assert _rows()[-1] == "cmd=open decision=refused reason=form"
    assert not _confirm_card(events)


# --- criterion 4: the other long-command rules are unchanged ------------------------------


def test_long_rules_unchanged(client, auto_on, monkeypatch):
    home = Path.home()
    docs = home / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    files = [docs / f"private-file-number-{i:02d}.txt" for i in range(12)]
    for f in files:
        f.write_text("x")
    (home / "Public").mkdir(exist_ok=True)
    for command in ("cp " + " ".join(str(f) for f in files) + f" {home / 'Public'}",
                    "mv " + " ".join(str(f) for f in files) + f" {home / 'Public'}"):
        assert len(command) > 120
        _, events = run_agent(client, bash(command), conv_id="j4a")
        assert _rows()[-1] == "cmd=cp decision=refused reason=form" if command.startswith("cp") \
            else _rows()[-1] == "cmd=mv decision=refused reason=form"
        assert not _confirm_card(events)
    deep = docs / ("folder-with-a-long-name/" * 6) / "n.md"
    deep.parent.mkdir(parents=True)
    deep.write_text("x")
    assert len(f"open {deep}") > 120
    monkeypatch.setenv("OVERLAY_VAULT", str(home / "Desktop" / "ObsidianVault"))
    _, events = run_agent(client, bash(f"open {deep}"), text="what is in my notes about the deadline",
                          conv_id="j4b", followup=True, barge_in=True, levels=LOUD, conf=0.9)
    assert _confirm_card(events)
    assert _rows()[-1] == "cmd=open decision=asked reason=barge"
    _, events = run_agent(client, bash(f"cat {docs / 'private-file-number-00.txt'}",
                                       "open https://example.com"), conv_id="j4c")
    assert _rows()[-2] == "cmd=cat decision=auto reason=ok"  # jauto3 builder: the cat marked the session
    assert _rows()[-1] == "cmd=open decision=asked reason=untrusted"


# --- criterion 7: typed query and long URLs open through the site opener ------------------


def test_gateway_typed_query_url(client):
    ran, events = run_turn(client, "open https://example.com/search?q=a&b=c", [])
    assert ran == [["open", "https://example.com/search?q=a&b=c"]]
    assert says(events) == ["Opening the link on example.com."]
    assert _steps("intent")[-1]["arg"] == "example.com"  # jauto3: the row keeps only the host
    long_path = "https://example.com/" + "a" * 80
    ran, events = run_turn(client, "open " + long_path, [])
    assert ran == [["open", long_path]]
    assert says(events) == ["Opening the link on example.com."]
    assert _steps("intent")[-1]["arg"] == "example.com"
    ran, events = run_turn(client, "open https://example.com", [])
    assert ran == [["open", "https://example.com"]]
    assert says(events) == ["Opening https://example.com."]
    assert _steps("intent")[-1]["arg"] == "example.com"  # today's form; Q3 host-only starts at queries
    ran, events = run_turn(client, "open github.com/docs", [])
    assert ran == [["open", "https://github.com/docs"]]
    assert _steps("intent")[-1]["arg"] == "github.com/docs"


# --- criterion 12 (overlay part): the vault scan only runs for note words ------------------


def test_overlay_note_names_gate(vault, monkeypatch):
    monkeypatch.delenv("OVERLAY_VAULT", raising=False)
    assert overlay._note_names("open the note x") == ()
    monkeypatch.setenv("OVERLAY_VAULT", str(vault / "missing"))
    assert overlay._note_names("open the note x") == ()
    monkeypatch.setenv("OVERLAY_VAULT", str(vault))
    assert overlay._note_names("what time is it") == ()
    assert overlay._note_names("Open the No x") != ()


def _note_client(client, monkeypatch):
    vault = Path.home() / "Desktop" / "J3Vault"
    (vault / "Brain" / "Research").mkdir(parents=True)
    (vault / "Brain" / "Research" / "llm-switch-local-vs-agents-benchmark-2026-11-18.md").write_text("x")
    (vault / "Brain" / "Research" / "llm-local-models.md").write_text("x")
    monkeypatch.setenv("OVERLAY_VAULT", str(vault))
    ran: list = []
    clock = Clock()
    pool, _deps = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Done."})]),
                        run=lambda argv, **k: ran.append(list(argv)), apps={})
    return vault, ran, pool


# --- criterion 13: the spoken note name opens the note in Obsidian ------------------------


def test_gateway_note_open(client, monkeypatch):
    vault, ran, pool = _note_client(client, monkeypatch)
    events = turn(client, "Open the No LLM Switch localal versus agents", conv_id="j3n")
    assert ran == [["open", "-a", "Obsidian", os.path.realpath(vault / BENCH)]]
    assert says(events) == ["Opening llm switch local vs agents benchmark."]
    assert not _confirm_card(events)
    assert pool.made == []  # the note never reaches the agent
    assert _steps("intent")[-1]["kind"] == "note_open"
    assert _steps("intent_pick")[-1]["detail"].endswith("reason=word_only")


# --- criterion 14: misses and ties speak their fixed lines --------------------------------


def test_gateway_note_lines(client, monkeypatch):
    vault, ran, pool = _note_client(client, monkeypatch)
    events = turn(client, "open the note banana bread", conv_id="j3m1")  # round 1 (U1): the agent searches
    assert not any("couldn't find" in t for t in says(events)) and ran == []
    assert _steps("intent")[-1]["kind"] == "agent" and len(pool.made) == 1
    assert "banana bread" in pool.made[0].prompts[0]
    events = turn(client, "open the llm note", conv_id="j3m2")
    assert says(events) == ["Two notes match: llm local models and llm switch local vs agents benchmark."
                            " Say more of the name."]  # round 1 (U2/U3): a hint, no question, no date words
    assert ran == []
    recipes = Path.home() / "Desktop" / "J3Recipes"
    recipes.mkdir(parents=True)
    for name in ("soup", "cake", "bread", "salad", "pie"):
        (recipes / f"recipe-{name}.md").write_text("x")
    monkeypatch.setenv("OVERLAY_VAULT", str(recipes))
    events = turn(client, "open the note recipe", conv_id="j3r")
    assert says(events) == ["Five notes match: recipe bread, recipe cake, recipe pie and 2 more. Say more of the name."]
    assert ran == []
    bad = Path.home() / "Desktop" / "J3Bad"
    bad.mkdir(parents=True)
    (bad / "bad-exec.md").write_text("x")
    (bad / "bad-exec.md").chmod(0o755)  # executable: never auto-opened
    (bad / "alias-memo.md").write_bytes(b"book\0\0\0\0mark")  # alias file: runs code by bytes
    monkeypatch.setenv("OVERLAY_VAULT", str(bad))
    for text, conv in (("open the note bad exec", "j3b1"), ("open the note alias memo", "j3b2")):
        events = turn(client, text, conv_id=conv)
        assert says(events) == ["I can't open that note."]
        assert ran == []


# --- criterion 15: note names never leave this Mac in a row or a log ----------------------


def test_note_names_stay_local(client, monkeypatch):
    vault, ran, pool = _note_client(client, monkeypatch)
    turn(client, "open the llm note", conv_id="j3p")
    events = turn(client, "open the llm switch note", conv_id="j3p")
    assert ran == [["open", "-a", "Obsidian", os.path.realpath(vault / BENCH)]]
    assert pool.made == []
    assert says(events) == ["Opening llm switch local vs agents benchmark."]
    turns = overlay.debug_log.snapshot()["turns"][:2]  # newest first; these are the only two
    leaks = ("benchmark", "local-models", "llm-switch", "Brain/Research", "llm local models")
    for t in turns:
        for step in t["steps"]:
            if step.get("step") == "transcribed":
                continue
            dumped = json.dumps(step)
            for leak in leaks:
                assert leak not in dumped, (leak, dumped)
            if step.get("step") == "spoke":
                assert step["text"] == "[note line]"
    for row in _steps("intent"):
        assert "arg" not in row
        assert re.fullmatch(r"hits=\d+ name_len=\d+", row["detail"])
    for row in _steps("intent_pick"):
        assert row["detail"].endswith("reason=word_only")
    hist = list(overlay._history["j3p"])
    assert hist[-1] == ("open the llm switch note", "I opened a note.")
    assert hist[-2][1] == "I listed matching notes."
    # jauto3 builder: every top-level turn field too, and the next agent prompt sees the fixed line, no stem
    for t in turns:
        dumped = json.dumps({k: val for k, val in t.items() if k != "steps"})
        assert not any(leak in dumped for leak in leaks), dumped
    turn(client, "what is in my notes about the deadline", conv_id="j3p")
    assert len(pool.made) == 1 and pool.made[0].prompts
    prompt = pool.made[0].prompts[0]
    assert "I opened a note." in prompt and "I listed matching notes." in prompt
    assert not any(leak in prompt for leak in leaks), prompt


# --- round 1 (S3): a typed query or long link stays out of the transcribed row and the history ----


def test_typed_link_private_in_rows_and_history(client):
    ran, events = run_turn(client, "open https://example.com/search?q=secret-token", [], conv_id="j3l")
    assert ran == [["open", "https://example.com/search?q=secret-token"]]
    assert _steps("transcribed")[-1]["text"] == "[open link]"
    assert not any("secret-token" in json.dumps(t) for t in overlay.debug_log.snapshot()["turns"])
    assert list(overlay._history["j3l"])[-1] == ("(link opened)", "the link on example.com")
    ran, events = run_turn(client, "open https://example.com", [], conv_id="j3s")  # short plain link: as before
    assert _steps("transcribed")[-1]["text"] == "open https://example.com"
    assert list(overlay._history["j3s"])[-1][0] == "open https://example.com"


# --- round 1 (C1/C2): find() is cheap on a full vault and runs once per turn, off the loop ----------


def test_find_fast_on_20000_notes():
    words = [f"w{i:04d}x{chr(97 + i % 26)}" for i in range(4000)]
    names = tuple(f"F{i % 40}/S{i % 7}/" + "-".join(words[(i * 7 + k * 13) % 4000] for k in range(7)) + ".md"
                  for i in range(note_find.FILES_MAX))
    note_find._prepared(names)  # scan() does this in the policy pool
    start = time.monotonic()
    note_find.find(names, "open the no alpha bravo charlie delta echo")
    note_find.find(names, " ".join(words[:5]))
    assert time.monotonic() - start < 1.0
    start = time.monotonic()
    note_find.find(names, " ".join(words[:5]))  # the overlay's second ask: the cached answer
    assert time.monotonic() - start < 0.01


def test_find_runs_once_per_note_turn(client, monkeypatch):
    vault, ran, pool = _note_client(client, monkeypatch)
    calls = []
    real = note_find._match
    monkeypatch.setattr(note_find, "_match", lambda q, t: calls.append(q) or real(q, t))
    turn(client, "Open the No LLM Switch localal versus agents", conv_id="j3o")
    assert ran and calls  # one scoring pass (warm-up in the pool), parse and the overlay reuse it
    n = len(calls)
    turn(client, "Open the No LLM Switch localal versus agents", conv_id="j3o2")
    assert len(calls) == n  # same words, same vault: no rescoring


def test_note_find_timeout_takes_agent_route(client, monkeypatch):
    vault, ran, pool = _note_client(client, monkeypatch)
    real = note_find.find
    monkeypatch.setattr(overlay, "POLICY_CHECK_S", 0.2)
    monkeypatch.setattr(overlay, "_note_names", lambda text: note_find.scan(overlay._vault()))  # no warm-up
    def slow(*a, **k):
        if threading.current_thread() is not threading.main_thread() and "policy" in threading.current_thread().name:
            time.sleep(0.5)
        return real(*a, **k)
    monkeypatch.setattr(note_find, "find", slow)
    events = turn(client, "open the llm switch note", conv_id="j3to")
    assert ran == [] and _steps("intent")[-1]["kind"] == "agent" and len(pool.made) == 1


def test_scan_dirs_bound(tmp_path, monkeypatch):
    root = tmp_path / "dirs"
    for i in range(10):
        (root / f"d{i}").mkdir(parents=True)
        (root / f"d{i}" / f"n{i}.md").write_text("x")
    assert note_find.DIRS_MAX == 4000
    monkeypatch.setattr(note_find, "DIRS_MAX", 3)  # stands for the 4000 bound: root + 2 folders
    assert len(note_find.scan(str(root))) == 2


# --- round 2 ---


def _tie_line(hits):
    return asyncio.run(overlay._note_open(list(hits), "x", None))[0]


def test_round2_dated_tie_keeps_the_date_words():
    hits = ["Research/llm-switch-benchmark-2026-11-18.md", "Research/llm-switch-benchmark-2026-11-19.md"]
    assert note_find.find(tuple(hits), "open the llm switch benchmark note") == hits  # a real tie
    line = _tie_line(hits)
    assert line == "Two notes match: llm switch benchmark 2026 11 18 and llm switch benchmark 2026 11 19." \
                   " Say more of the name."


def test_round2_folder_tie_names_the_folder():
    assert _tie_line(["A/Meeting.md", "B/Meeting.md"]) == \
        "Two notes match: Meeting in A and Meeting in B. Say more of the name."
    assert _tie_line(["Meeting.md", "Projects/Meeting.md"]) == \
        "Two notes match: Meeting in Projects and Meeting in the vault top folder. Say more of the name."


def test_round2_distinct_tie_line_unchanged():
    assert _tie_line(["Notes/llm-local-models.md", "Notes/llm-switch-bench.md"]) == \
        "Two notes match: llm local models and llm switch bench. Say more of the name."


def test_round2_refused_typed_link_never_spoken(client, monkeypatch):
    monkeypatch.setattr(intents, "open_site", lambda arg, **k: None)  # refused after parse accepted it
    ran, events = run_turn(client, "open https://example.com/search?q=secret-token", [], conv_id="j3r2")
    assert ran == []
    assert says(events) == ["I couldn't find example.com."]
    assert not any("secret-token" in json.dumps(t) for t in overlay.debug_log.snapshot()["turns"])


def test_round2_long_ask_capped_at_12_words():
    names = ("Research/llm-switch-benchmark.md", "Research/llm-local.md")
    filler = " ".join(f"word{i}" for i in range(90))
    assert note_find.find(names, f"open the note {filler}") == []  # returns, never ties a worker for long
    keys = list(note_find._index["found"])
    assert keys and all(len(k[0]) <= 12 for k in keys)  # at most 12 unique words scored, order kept
    assert note_find.find(names, "open the llm switch benchmark note") == [names[0]]  # normal asks match


# --- round 3 ---


def test_round3_digits_narrow_a_dated_tie():
    names = ("Research/llm-switch-benchmark-2026-11-18.md", "Research/llm-switch-benchmark-2026-11-19.md")
    assert note_find.find(names, "open the note llm switch benchmark 2026 11 18") == [names[0]]
    assert note_find.find(names, "llm switch benchmark 2026-11-19") == [names[1]]
    assert note_find.find(names, "llm switch benchmark 20261118") == [names[0]]
    assert note_find.find(names, "open the llm switch benchmark note") == list(names)  # no digits: tie stands
    assert note_find.find(names, "llm switch benchmark 2025") == list(names)  # digits match neither: tie kept


def test_round3_digit_tie_cached_apart():
    names = ("Research/llm-switch-benchmark-2026-11-18.md", "Research/llm-switch-benchmark-2026-11-19.md")
    assert note_find.find(names, "llm switch benchmark 2026 11 18") == [names[0]]
    assert note_find.find(names, "llm switch benchmark 2026 11 19") == [names[1]]  # same words, other digits


def test_round3_folder_tie_names_two_folders():
    assert _tie_line(["X/B/Meeting.md", "Y/B/Meeting.md"]) == \
        "Two notes match: Meeting in X B and Meeting in Y B. Say more of the name."


# --- jauto3c: a date said in words picks the note ---------------------------------------------------


N7 = "Ideas/saudi-whale-ideas-2026-11-07.md"
N18 = "Ideas/saudi-whale-ideas-2026-11-18.md"


@pytest.fixture
def whale_vault(tmp_path):
    v = tmp_path / "vault"
    (v / "Ideas").mkdir(parents=True)
    (v / "Ideas" / "saudi-whale-ideas-2026-11-07.md").write_text("x")
    (v / "Ideas" / "saudi-whale-ideas-2026-11-18.md").write_text("x")
    return v


@pytest.mark.parametrize("ask", [
    "saudi whale ideas november 18th",
    "saudi whale ideas 18th of november",
    "saudi whale ideas the eighteenth of november",
    "saudi whale ideas nov 18",
    "saudi whale ideas november eighteen",
    "saudi whale ideas november 18 2026",
])
def test_jauto3c_spoken_date_picks_the_note(ask, whale_vault):
    names = note_find.scan(str(whale_vault))
    assert note_find.find(names, ask) == [N18], ask


def test_jauto3c_day_first_spoken_date_picks_the_other_note(whale_vault):
    names = note_find.scan(str(whale_vault))
    assert note_find.find(names, "saudi whale ideas 7th november") == [N7]


def test_jauto3c_no_such_date_keeps_the_tie(whale_vault):
    names = note_find.scan(str(whale_vault))
    assert note_find.find(names, "saudi whale ideas december 3rd") == [N7, N18]
    assert note_find.find(names, "saudi whale ideas") == [N7, N18]


def test_jauto3c_parse_and_find_pick_one(whale_vault):
    text = "open the note saudi whale ideas november 18th"
    names = note_find.scan(str(whale_vault))
    got = intents.parse(text, apps=A, note_names=names)
    assert got.kind == "note_open"
    assert note_find.find(names, got.arg, literal=got.sub == "literal") == [N18]


def test_jauto3c_digits_still_narrow(whale_vault):
    names = note_find.scan(str(whale_vault))
    assert note_find.find(names, "saudi whale ideas 2026 11 18") == [N18]
    assert note_find.find(names, "saudi whale ideas 1118") == [N18]


def test_jauto3c_may_alone_stays_a_name_word(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "may-ideas.md").write_text("x")
    names = note_find.scan(str(vault))
    assert note_find.find(names, "may ideas") == ["may-ideas.md"]  # no day beside "may"


# --- jauto3c round 1: date words leave the query only when they form a spoken date ---


def _vault(tmp_path, *rels):
    v = tmp_path / "vault"
    for rel in rels:
        p = v / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return note_find.scan(str(v))


def test_jauto3c_r1_month_word_is_a_name_without_a_day(tmp_path):
    names = _vault(tmp_path, "Plans/march-budget.md", "Plans/april-budget.md")
    assert note_find.find(names, "march budget") == ["Plans/march-budget.md"]
    got = intents.parse("open the march budget notes", apps=A, note_names=names)
    assert got.kind == "note_open"  # base behavior, not open_app


def test_jauto3c_r1_ordinal_word_is_not_a_mishear_match(tmp_path):
    names = _vault(tmp_path, "Drafts/first-draft.md")
    assert note_find.find(names, "second draft") == []  # base behavior: no fuzzy cross-match


def test_jauto3c_r1_month_named_note_opens(tmp_path):
    names = _vault(tmp_path, "january.md")
    got = intents.parse("open the january note", apps=A, note_names=names)
    assert got.kind == "note_open"
    assert note_find.find(names, got.arg, literal=got.sub == "literal") == ["january.md"]


def test_jauto3c_r1_number_word_names_still_match(tmp_path):
    names = _vault(tmp_path, "first-principles.md", "second-brain.md")
    assert note_find.find(names, "first principles") == ["first-principles.md"]
    got = intents.parse("open the second brain notes", apps=A, note_names=names)
    assert got.kind == "note_open"
    assert note_find.find(names, got.arg) == ["second-brain.md"]


# --- jauto3c round 2 ---


def test_jauto3c_r2_tens_before_the_month(tmp_path):
    names = _vault(tmp_path, *[f"Meetings/team-standup-2026-{md}.md"
                               for md in ("11-01", "11-21", "12-01", "12-31")])
    for ask, want in (
        ("team standup the twenty first of november", "Meetings/team-standup-2026-11-21.md"),
        ("team standup twenty-first of november", "Meetings/team-standup-2026-11-21.md"),
        ("team standup the thirty first of december", "Meetings/team-standup-2026-12-31.md"),
        ("team standup november twenty first", "Meetings/team-standup-2026-11-21.md"),
        ("team standup the first of november", "Meetings/team-standup-2026-11-01.md"),
        ("team standup twenty one november", "Meetings/team-standup-2026-11-21.md"),
    ):
        assert note_find.find(names, ask) == [want], ask


# --- jauto3c round 3 ---


def test_jauto3c_r3_digits_never_join_a_unit_word(tmp_path):
    names = _vault(tmp_path, "Meetings/weekly-one-on-one-2026-11-20.md",
                   "Meetings/weekly-one-on-one-2026-11-21.md")
    for ask in ("weekly one on one 20th of november", "weekly one on one 20 november",
                "november 20 one on one weekly", "weekly one on one november 20th"):
        assert note_find.find(names, ask) == ["Meetings/weekly-one-on-one-2026-11-20.md"], ask
    assert note_find.find(names, "weekly one on one the twenty first of november") \
        == ["Meetings/weekly-one-on-one-2026-11-21.md"]


def test_jauto3c_r3_thirty_digits_stay_the_said_day(tmp_path):
    names = _vault(tmp_path, "Meetings/weekly-one-on-one-2026-12-30.md",
                   "Meetings/weekly-one-on-one-2026-12-31.md")
    assert note_find.find(names, "weekly one on one 30th of december") \
        == ["Meetings/weekly-one-on-one-2026-12-30.md"]


# --- jauto3c round 3b ---


def test_jauto3c_r3b_a_name_one_never_joins_the_tens(tmp_path):
    names = _vault(tmp_path, "Meetings/weekly-one-on-one-2026-11-20.md",
                   "Meetings/weekly-one-on-one-2026-11-21.md")
    for ask in ("weekly one on one twenty november", "weekly one on one twenty of november",
                "one on one twenty november weekly"):
        assert note_find.find(names, ask) == ["Meetings/weekly-one-on-one-2026-11-20.md"], ask


def test_jauto3c_r3b_a_name_one_never_joins_thirty(tmp_path):
    names = _vault(tmp_path, "Meetings/weekly-one-on-one-2026-12-30.md",
                   "Meetings/weekly-one-on-one-2026-12-31.md")
    assert note_find.find(names, "weekly one on one thirty december") \
        == ["Meetings/weekly-one-on-one-2026-12-30.md"]


# --- jauto3c round 3c ---


def test_jauto3c_r3c_bare_unit_after_the_month_is_only_a_fallback(tmp_path):
    names = _vault(tmp_path, "Meetings/weekly-one-on-one-2026-11-01.md",
                   "Meetings/weekly-one-on-one-2026-11-20.md",
                   "Meetings/weekly-one-on-one-2026-11-21.md",
                   "Meetings/weekly-one-on-one-2026-12-01.md",
                   "Meetings/weekly-one-on-one-2026-12-30.md",
                   "Notes/two-pager-2026-11-18.md",
                   "Notes/two-pager-2026-11-19.md")
    for ask, want in (
        ("20th of november one on one weekly", "Meetings/weekly-one-on-one-2026-11-20.md"),
        ("20 november one on one weekly", "Meetings/weekly-one-on-one-2026-11-20.md"),
        ("twenty november one on one weekly", "Meetings/weekly-one-on-one-2026-11-20.md"),
        ("twenty first of november one on one weekly",
         "Meetings/weekly-one-on-one-2026-11-21.md"),
        ("thirty december one on one weekly", "Meetings/weekly-one-on-one-2026-12-30.md"),
        ("weekly one on one november", "Meetings/weekly-one-on-one-2026-11-01.md"),
        ("18 november two pager", "Notes/two-pager-2026-11-18.md"),
        ("two pager november 19th", "Notes/two-pager-2026-11-19.md"),
    ):
        assert note_find.find(names, ask) == [want], ask


# --- jauto3c date table ---


def test_jauto3c_date_table(tmp_path):
    """Every probe from the jauto3c round 0..3d reviews on one vault: dates before and after
    the name, digits, ordinals, word days, twenty/thirty forms, wrong dates keep the tie,
    and the name-word notes (march budget, second draft, january, first principles, second
    brain) stay unchanged."""
    vault = tmp_path / "vault"
    rels = [f"Meetings/weekly-one-on-one-2026-{md}.md" for md in
            ("11-01", "11-03", "11-05", "11-09", "11-20", "11-21",
             "12-01", "12-02", "12-05", "12-30", "12-31")] + [
        "Notes/two-pager-2026-11-18.md", "Notes/two-pager-2026-11-19.md",
        "Ideas/saudi-whale-ideas-2026-11-07.md", "Ideas/saudi-whale-ideas-2026-11-18.md",
        "Plans/march-budget.md", "Plans/april-budget.md", "Drafts/first-draft.md",
        "january.md", "first-principles.md", "second-brain.md", "may-ideas.md"]
    for rel in rels:
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    names = note_find.scan(str(vault))
    W = lambda md: f"Meetings/weekly-one-on-one-2026-{md}.md"  # noqa: E731
    T = lambda md: f"Notes/two-pager-2026-{md}.md"  # noqa: E731
    S7, S18 = "Ideas/saudi-whale-ideas-2026-11-07.md", "Ideas/saudi-whale-ideas-2026-11-18.md"
    TIE = [S7, S18]
    rows = [
        # round 0: the 11-18 family and the 11-07 family
        ("saudi whale ideas november 18th", S18),
        ("saudi whale ideas the 18th of november", S18),
        ("saudi whale ideas nov 18", S18),
        ("saudi whale ideas november the eighteenth", S18),
        ("saudi whale ideas 18 november", S18),
        ("saudi whale ideas november eighteen", S18),
        ("saudi whale ideas november 18 2026", S18),
        ("saudi whale ideas on november 18th please", S18),
        ("saudi whale ideas 2026 11 18", S18),
        ("saudi whale ideas 1118", S18),
        ("saudi whale ideas 7th november", S7),
        ("saudi whale ideas november 7", S7),
        ("saudi whale ideas nov 07", S7),
        ("saudi whale ideas the seventh of november", S7),
        # round 0: a wrong or partial date keeps the tie (also the bogus days of round 2/3c)
        ("saudi whale ideas december 3rd", TIE),
        ("saudi whale ideas november 1st", TIE),
        ("saudi whale ideas november twenty first", TIE),
        ("saudi whale ideas november", TIE),
        ("saudi whale ideas february 30", TIE),
        ("saudi whale ideas the thirty second of december", TIE),
        # round 2: a tens+unit day before or after the month
        ("weekly one on one the twenty first of november", W("11-21")),
        ("weekly one on one twenty-first of november", W("11-21")),
        ("weekly one on one the thirty first of december", W("12-31")),
        ("weekly one on one november twenty first", W("11-21")),
        ("weekly one on one the first of november", W("11-01")),
        ("weekly one on one twenty one november", W("11-21")),
        ("weekly one on one december first", W("12-01")),
        ("weekly one on one december thirty first", W("12-31")),
        ("weekly one on one the thirty-first december", W("12-31")),
        ("weekly one on one 2026 the twenty first of november", W("11-21")),
        # round 3: digits never join a unit word; the words twenty/thirty keep the said day
        ("weekly one on one 20th of november", W("11-20")),
        ("weekly one on one 20 november", W("11-20")),
        ("november 20 one on one weekly", W("11-20")),
        ("weekly one on one november 20th", W("11-20")),
        ("weekly one on one the twenty first of november", W("11-21")),
        ("weekly one on one 30th of december", W("12-30")),
        # round 3b: a name's "one" never joins the tens word
        ("weekly one on one twenty november", W("11-20")),
        ("weekly one on one twenty of november", W("11-20")),
        ("one on one twenty november weekly", W("11-20")),
        ("weekly one on one thirty december", W("12-30")),
        # round 3c: a bare unit after the month is only a fallback; a day before the month wins
        ("20th of november one on one weekly", W("11-20")),
        ("20 november one on one weekly", W("11-20")),
        ("twenty november one on one weekly", W("11-20")),
        ("twenty first of november one on one weekly", W("11-21")),
        ("thirty december one on one weekly", W("12-30")),
        ("weekly one on one november", W("11-01")),
        ("18 november two pager", T("11-18")),
        ("two pager november 19th", T("11-19")),
        # round 3d: a unit word that is part of a note name is not the day
        ("weekly one on one november three", W("11-03")),
        ("weekly one on one november five", W("11-05")),
        ("weekly one on one november nine", W("11-09")),
        ("weekly one on one december five", W("12-05")),
        ("three november one on one weekly", W("11-03")),
        ("five december one on one weekly", W("12-05")),
        # rounds 0/1: name words alone stay name tokens
        ("march budget", "Plans/march-budget.md"),
        ("first principles", "first-principles.md"),
        ("second brain", "second-brain.md"),
        ("may ideas", "may-ideas.md"),
        ("second draft", []),
    ]
    for ask, want in rows:
        assert note_find.find(names, ask) == ([want] if isinstance(want, str) else want), ask
    assert note_find.find(names, "january", literal=True) == ["january.md"]
    for text in ("open the march budget notes", "open the january note",
                 "open the note weekly one on one november three",
                 "open the second brain notes"):
        assert intents.parse(text, apps=A, note_names=names).kind == "note_open", text
