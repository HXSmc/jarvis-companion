"""jauto2: "open the note X" / "open https://..." really open, without a go-ahead.

From the owner's transcript only: a note, document, path or URL nothing installed answers to goes to the agent
(sub "notes" for note words), a URL with a scheme goes to the fixed site opener (always https), and
`open -a <viewer>` / `open -e` of a regular file runs auto even after untrusted text (viewers: Obsidian,
Preview, TextEdit, QuickTime Player). Browsers, other apps, folders with -a, URLs and code-ish files still ask.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from mac.voice import agent, agent_policy, auto_policy, intents  # noqa: E402
from test_overlay_agent import Clock, FakeAgent, client, reset, result, setup  # noqa: F401,E402
from test_overlay_jauto import (  # noqa: F401,E402
    H, OK, OUT, OWNER_NOTE, RC, ctx, fill, v, _deep, _switch_off)
from test_overlay_jfollow10a import _steps, auto_on, fresh_auto_state  # noqa: F401,E402
from test_overlay_openapp import APPS, run_turn  # noqa: E402
from test_overlay_service import says, turn  # noqa: F401,E402


# --- criterion 1: "open a note / document / md path" goes to the agent ----------------------

A = {"safari": "Safari", "notes": "Notes", "maps": "Maps", "music": "Music"}

DOC_ROWS = [
    ("open the note llm-switch-local-vs-agents-benchmark-2026-11-18", "notes"),  # jauto2: note word -> notes sub
    ("open my note shopping ideas", "notes"),
    ("open the file Brain/Research/x.md in Obsidian", "notes"),
    ("open Brain/Research/x.md", "notes"),  # jauto2: .md path -> notes sub
    ("open report.pdf", ""),  # jauto2: no note word and no .md -> plain agent
    ("open the document quarterly plan", ""),
]


@pytest.mark.parametrize("text,sub", DOC_ROWS)
def test_open_note_goes_to_agent(text, sub):
    got = intents.parse(text, apps=A)
    assert got.kind == "agent" and got.sub == sub  # jauto2: no installed app matches, so the agent handles it


# --- criterion 2: URLs still open as sites --------------------------------------------------

def test_open_url_goes_to_site():
    for text, arg in (("open https://example.com", "example.com"),  # jauto2: scheme stripped, case kept
                      ("open HTTPS://Example.com/docs/a", "Example.com/docs/a"),
                      ("open http://example.com", "example.com"),
                      ("open github.com", "github.com")):  # jauto2: no scheme, still a plain site
        assert intents.parse(text, apps=A) == intents.Intent("open_site", arg)
    ran = []
    assert intents.open_site("example.com", run=lambda argv, **k: ran.append(list(argv))) == "https://example.com"
    assert ran == [["open", "https://example.com"]]
    for text in ("open https://example.com/search?q=a&b=c", "open https://evil.example; rm -rf ~"):
        assert intents.parse(text, apps=A).kind == "agent"  # jauto2: query string / shell char -> agent
    assert intents.open_site("evil.example; rm -rf ~", run=lambda argv, **k: pytest.fail("must not run")) is None


# --- criterion 3: existing pinned intents unchanged ------------------------------------------

PIN_ROWS = [
    ("open Photoshop", APPS),  # unknown app: open_app unchanged
    ("open notes", {"notes": "Notes", "safari": "Safari"}),
    ("open the notes", {"notes": "Notes", "safari": "Safari"}),
    ("open safari", {"notes": "Notes", "safari": "Safari"}),
    ("open stop", {}),
    ("open a session", {}),
    ("open sessions", {}),
    ("open sessions on the Mac", {}),
    ("open maps", {}),
    ("open code", APPS),  # tie: open_app still resolves the tie question, not the agent
    ("open the note Projects in the Secrets folder", {"notes": "Notes"}),  # secrets vault note: unchanged
    ("open notes.app", {}),  # .app name: open_site, not the notes agent
    ("open Secrets/Projects.md", {}),  # jauto2: a Secrets path is the refusal, never routed to the agent
    ("open the note in the Secrets folder", {}),
]


@pytest.mark.parametrize("text,apps", PIN_ROWS)
def test_open_app_rows_unchanged(text, apps):
    kind = "vault_secrets" if "Secrets" in text else ("open_site" if text.endswith(".app") else "open_app")
    assert intents.parse(text, apps=apps).kind == kind  # jauto2: doc routing never steals these


# --- criterion 4: the gateway routes an open-note turn to the agent -------------------------

def test_gateway_open_note_reaches_agent(client, auto_on, monkeypatch):
    vault = Path.home() / "Desktop" / "Vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OVERLAY_VAULT", str(vault))
    clock = Clock()
    ran: list = []
    pool, _deps = setup(clock, lambda: FakeAgent(clock, script=[result({"say": "Done."})]),
                        run=lambda argv, **k: ran.append(list(argv)), apps={})
    events = turn(client, "open the note shopping ideas", conv_id="jauto2n")
    assert _steps("intent")[-1]["kind"] == "agent"  # jauto2: agent turn, not open_app
    assert ran == []  # no launch happened in the gateway turn itself
    assert not any("couldn't find" in t for t in says(events))
    assert len(pool.made) == 1 and "shopping ideas" in pool.made[0].prompts[0]  # the agent saw the text


# --- criterion 5: gateway open-site and unknown-app turns unchanged -------------------------

def test_gateway_open_url_opens(client):
    ran, events = run_turn(client, "open https://example.com", [])
    assert ran == [["open", "https://example.com"]]  # jauto2: scheme stripped, https added, open_site runs
    assert says(events) == ["Opening https://example.com."]  # B4: no ack before the line
    assert _steps("intent")[-1]["kind"] == "open_site"
    ran, events = run_turn(client, "open Photoshop", [])
    assert ran == []
    assert says(events) == ["I couldn't find Photoshop."]  # B4: no ack before the line


# --- criterion 6: viewer `open -a` of files runs even tainted --------------------------------

def test_open_a_obsidian_file_tainted_runs(H, ctx, auto_on):
    (H / "Documents" / "x.mov").write_text("x")
    rows = [("Obsidian", H / OWNER_NOTE),  # jauto2: the owner's own note opens in Obsidian, tainted too
            ("Preview", H / "Documents" / "a.pdf"),
            ("TextEdit", H / "Documents" / "a.txt"),
            ("QuickTime Player", H / "Documents" / "x.mov"),
            ("Obsidian.app", H / OWNER_NOTE)]
    for app, path in rows:
        command = f"open -a {shlex.quote(app)} {shlex.quote(str(path))}"
        assert v(command, ctx, tainted=True) == OK, command
    assert v(f"open -e {shlex.quote(str(H / 'Documents' / 'a.txt'))}", ctx, tainted=True) == OK  # -e counts too


# --- criterion 7: long viewer open still Confirm, not refused -------------------------------

def test_long_open_a_obsidian_not_refused(H, ctx, auto_on):
    note = _deep(H, "n.md")
    command = f"open -a Obsidian {note}"
    assert len(f"run {command}") > 120
    got = agent_policy.decide("Bash", {"command": command}, ctx)
    assert isinstance(got, agent_policy.Confirm) and got.spoken_ok is False  # jauto2: length no longer refuses
    assert v(command, ctx, tainted=True) == OK
    got = agent_policy.decide("Bash", {"command": f"open -a Safari {note}"}, ctx)
    assert isinstance(got, agent_policy.Deny) and agent_policy.reason_code(got.reason) == "form"  # Safari not a viewer


# --- criterion 8: other apps / folders / URLs / -e folders still ask -------------------------

ASK_VIEWER_ROWS = [
    "open -a Safari H/Documents/doc.md",
    "open -a 'Google Chrome' H/Documents/doc.pdf",
    "open -a Firefox H/Documents/doc.md",
    "open -a Arc H/Documents/doc.md",
    "open -a Finder H/Documents/doc.md",
    "open -a Pages H/Documents/doc.pages",
    "open -a 'Microsoft Word' H/Documents/doc.docx",
    "open -a Music H/Documents/doc.mp3",
    "open -a Photos H/Documents/doc.png",
    "open -a Obsidian H/Documents/Folder",  # jauto2: folder operand is not exempt
    "open -a Preview H/Documents/Folder",
    "open -a Obsidian H/Documents/doc.md H/Documents/Folder",  # every operand must be a file
    "open -a Obsidian https://example.com",  # jauto2: URL operand is not exempt
    "open -e H/Documents/Folder",
    "open -e -a Safari H/Documents/doc.txt",  # -e with -a: the app must be a viewer too
]


@pytest.mark.parametrize("command", ASK_VIEWER_ROWS)
def test_open_a_tainted_still_asks(command, H, ctx, auto_on):
    for ext in ("md", "pdf", "txt", "pages", "docx", "mp3", "png"):
        (H / "Documents" / f"doc.{ext}").write_text("x")
    (H / "Documents" / "Folder").mkdir()
    assert v(fill(command, H), ctx, tainted=True) == ("asked", "untrusted"), command  # taint asks
    assert v(fill(command, H), ctx) == OK, command  # but every row runs clean


# --- criterion 9: OPEN_LOCAL_APPS pinned, ascii-only ----------------------------------------

def test_open_local_apps_pinned(H, ctx, auto_on):
    assert auto_policy.OPEN_LOCAL_APPS == frozenset({"obsidian", "preview", "textedit", "quicktime player"})
    assert auto_policy.OPEN_LOCAL_APPS <= auto_policy.OPEN_APPS
    assert auto_policy.OPEN_LOCAL_APPS & {"safari", "google chrome", "firefox", "arc", "finder", "music", "photos",
                                          "pages", "numbers", "keynote", "microsoft word", "microsoft excel",
                                          "microsoft powerpoint"} == set()
    (H / "Documents" / "doc.md").write_text("x")
    for app in ("ſafari", "OBSİDİAN"):  # jauto2: non-ascii lookalikes are not viewer names
        assert v(fill(f"open -a {app} H/Documents/doc.md", H), ctx, tainted=True)[0] == "asked", app
        assert auto_policy._open_local(["open", "-a", app, str(H / "Documents" / "doc.md")]) is False, app
    for app in ("obſidian", "OBSİDİAN", "/Applications/Obsidian.app"):  # the new guard itself, past classify
        assert auto_policy._open_local(["open", "-a", app, str(H / "Documents" / "doc.md")]) is False, app
    assert auto_policy._open_local(["open", "-a", "Obsidian", str(H / "Documents" / "doc.md")]) is True


# --- criterion 10: states, code files, secrets unchanged ------------------------------------

def test_states_unchanged_jauto2(H, ctx, auto_on):
    owner = f"open -a Obsidian {H / OWNER_NOTE}"
    assert v(owner, ctx, barge=True) == ("asked", "barge")
    assert v(owner, ctx, gate_ok=False) == ("asked", "followup")
    (H / "Documents" / "report.pdf").write_bytes(b"book\0\0\0\0mark")  # alias file: runs_code by bytes
    for path in ("H/Documents/x.command", "H/Documents/run.md", "H/Documents/report.pdf"):
        command = fill(f"open -a Obsidian {path}", H)
        assert v(command, ctx) == RC, command
        assert v(command, ctx, tainted=True) == RC, command
    assert v(fill("open -a Obsidian H/Desktop/ObsidianVault/Secrets/a.md", H), ctx,
             tainted=True)[0] == "asked"  # secrets ask
    assert v(f"curl https://x && open -a Obsidian {H / OWNER_NOTE}", ctx, tainted=True) == ("asked", "network")
    _switch_off()
    assert v(owner, ctx) == ("asked", "off")


# --- criterion 11: quoting and scheme shapes ------------------------------------------------

def test_form_shapes(H, ctx, auto_on):
    (H / "Documents" / "a b.md").write_text("x")
    for command in (f"open {shlex.quote(str(H / 'Documents' / 'a b.md'))}",  # single-quoted space: Confirm
                    f'open "{H}/Documents/a b.md"'):
        got = agent_policy.decide("Bash", {"command": command}, ctx)
        assert isinstance(got, agent_policy.Confirm), command
        assert v(command, ctx) == OK, command
    got = agent_policy.decide("Bash", {"command": f"open {H}/Documents/a\\ b.md"}, ctx)
    assert isinstance(got, agent_policy.Deny) and agent_policy.reason_code(got.reason) == "form"  # backslash form
    got = agent_policy.decide("Bash", {"command": 'open "obsidian://open?vault=V&file=x"'}, ctx)
    assert isinstance(got, agent_policy.Deny) and agent_policy.reason_code(got.reason) == "form"  # & is a shell metachar
    assert v("open obsidian://open?vault=V", ctx) == RC  # obsidian:// actions write notes: never auto
    assert v("open '~/Documents/a.md'", ctx) == OUT  # ~ does not expand: outside


# --- criterion 12: the agent prompt teaches the shapes --------------------------------------

def test_agent_prompt_teaches_open():
    assert "open '<absolute path>'" in agent.AGENT_PROMPT  # jauto2: the new open guidance line
    assert "obsidian://" in agent.AGENT_PROMPT
