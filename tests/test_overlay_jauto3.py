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
    got = intents.parse("open the note banana bread", apps=A, note_names=names)
    assert got.kind == "note_open"
    assert note_find.find(names, got.arg, literal=got.sub == "literal") == []  # no match: opener says nothing
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
    assert note_find.speak_name(BENCH) == "llm switch local vs agents benchmark 2026 11 18"


# --- criterion 16: note_open is word-only and both modules import clean -----------------------------


def test_word_only_has_note_open():
    assert "note_open" in intent_pick.WORD_ONLY and "note_open" not in intent_pick.PICKS
    for code in ("import mac.voice.intents, mac.voice.note_find",
                 "import mac.voice.note_find, mac.voice.intents"):  # no import cycle either way
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True)
        assert r.returncode == 0, r.stderr.decode()


# --- gateway wiring: the hunks above through the real utterance pipeline ------------------

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
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


@pytest.mark.parametrize("mode", ["marked", "auto_tainted", "barge", "followup", "off", "timeout"])
def test_gateway_long_url_denied_when_unsafe(mode, client, auto_on, monkeypatch):
    doc = Path.home() / "Documents" / "a.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("x")
    conv, extra = "j3d", {}
    if mode == "auto_tainted":
        overlay._auto_tainted.add(conv)
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
    assert says(events) == ["Opening llm switch local vs agents benchmark 2026 11 18."]
    assert not _confirm_card(events)
    assert pool.made == []  # the note never reaches the agent
    assert _steps("intent")[-1]["kind"] == "note_open"
    assert _steps("intent_pick")[-1]["detail"].endswith("reason=word_only")


# --- criterion 14: misses and ties speak their fixed lines --------------------------------


def test_gateway_note_lines(client, monkeypatch):
    vault, ran, pool = _note_client(client, monkeypatch)
    events = turn(client, "open the note banana bread", conv_id="j3m1")
    assert says(events) == ["I couldn't find a note called banana bread."]
    assert ran == []
    events = turn(client, "open the llm note", conv_id="j3m2")
    lines = says(events)
    assert len(lines) == 1 and lines[0].startswith("Which note: ")
    assert "llm local models" in lines[0] and "llm switch local vs agents benchmark 2026 11 18" in lines[0]
    assert ran == []
    recipes = Path.home() / "Desktop" / "J3Recipes"
    recipes.mkdir(parents=True)
    for name in ("soup", "cake", "bread", "salad", "pie"):
        (recipes / f"recipe-{name}.md").write_text("x")
    monkeypatch.setenv("OVERLAY_VAULT", str(recipes))
    events = turn(client, "open the note recipe", conv_id="j3r")
    assert says(events) == ["Which note: recipe bread, recipe cake, recipe pie or one of 2 more?"]
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
    assert says(events) == ["Opening llm switch local vs agents benchmark 2026 11 18."]
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
