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
