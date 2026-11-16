"""jfollow10c ruling 2: the allow-list SVG sanitiser (common/svgsafe.py)."""

from __future__ import annotations

import random
import re
import sys
from pathlib import Path
from xml.parsers import expat

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common import svgsafe  # noqa: E402

DRAWINGS = ROOT / "tests" / "fixtures" / "drawings"
ATTACK = DRAWINGS / "attack"
BAD = re.compile(r"(?i)<script|<style|foreignObject|<image|<iframe|<animate|<set\b|\son[a-z]+=|javascript:|data:"
                 r"|https?:|url\((?!#)|@import|xlink:|ns0:")
# sample -> the reason it must be refused with, or None when a cleaned picture may come out
ATTACKS = {
    "script": None, "onload": None, "javascript_href": None, "external_image": None, "external_css": None,
    "entity_bomb": "doctype", "foreignobject_html": None, "animate_href_swap": None, "use_external": None,
    "huge_size": "too_big", "deep_nesting": "too_deep", "data_html": None, "xlink_http": None,
    "style_url": None, "cdata_script": None, "utf16_doctype": "doctype", "class_style_sheet": None,
}
V = 'viewBox="0 0 100 100"'
XMLNS = 'xmlns="http://www.w3.org/2000/svg"'


def test_attack_table_is_complete():
    assert len(ATTACKS) >= 17
    assert {p.stem for p in ATTACK.glob("*.svg")} == set(ATTACKS)


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_attacks(name):
    # latin-1: a file in any encoding (UTF-16 too) reaches the sanitiser as some str, never an error
    res = svgsafe.sanitize((ATTACK / f"{name}.svg").read_bytes().decode("latin-1"))
    if ATTACKS[name] is not None:
        assert res.svg is None and res.reason == ATTACKS[name]
        return
    if res.svg is None:
        assert res.reason in ("empty", "unparseable")
        return
    assert res.reason == "ok"
    # the one fixed xmlns declaration is the SVG namespace name, not a link: checked apart
    assert res.svg.count(XMLNS) == 1
    body = res.svg.replace(XMLNS, "")
    assert not BAD.search(body), BAD.search(body)
    assert res.removed >= 1 or res.attrs_removed >= 1


def test_parser_refuses_doctype_without_the_text_scan(monkeypatch):
    """The expat handlers refuse a DOCTYPE/ENTITY even when the text scan misses it."""
    monkeypatch.setattr(svgsafe, "_DOCTYPE", re.compile(r"(?!x)x"))
    bomb = (ATTACK / "entity_bomb.svg").read_text()
    assert svgsafe.sanitize(bomb).reason == "doctype"
    assert svgsafe.sanitize(f'<!DOCTYPE svg><svg {V}><rect width="1" height="1"/></svg>').reason == "doctype"
    # UTF-16 bytes with an encoding= declaration: never decoded behind the scan's back
    utf16 = (ATTACK / "utf16_doctype.svg").read_bytes()
    assert svgsafe.sanitize(utf16.decode("utf-16")).reason == "doctype"
    assert svgsafe.sanitize(utf16.decode("latin-1")).svg is None


def _tags(svg: str) -> list[str]:
    tags: list[str] = []
    p = expat.ParserCreate(namespace_separator=" ")
    p.StartElementHandler = lambda name, _a: tags.append(name.rpartition(" ")[2])
    p.Parse(svg, True)
    return tags


def _text(svg: str) -> str:
    out: list[str] = []
    p = expat.ParserCreate()
    p.CharacterDataHandler = out.append
    p.Parse(svg, True)
    return "".join(out).strip()


def _view_box(svg: str) -> str:
    return re.search(r'<svg[^>]*\sviewBox="([^"]+)"', svg).group(1)


@pytest.mark.parametrize("name", ["phone", "process", "cross_section"])
def test_clean_samples_survive(name):
    src = (DRAWINGS / f"{name}.svg").read_text()
    res = svgsafe.sanitize(src)
    assert res.reason == "ok" and res.removed == 0
    out = res.svg
    assert _view_box(out) == _view_box(src)
    assert _tags(out) == _tags(src)
    assert _text(out) == _text(src)
    callouts = lambda s: len(re.findall(r">\s*\d{1,2}\s*</text>", s))  # the numbered callouts
    assert callouts(out) == callouts(src) >= 3
    assert out.count('xmlns="http://www.w3.org/2000/svg"') == 1 and out.count("xmlns") == 1
    assert "ns0:" not in out
    assert svgsafe.sanitize(out).svg == out


def test_unwrap_link_keeps_text():
    res = svgsafe.sanitize(f'<svg {V}><a href="https://x.example"><text x="1" y="30">Lens</text></a></svg>')
    assert res.reason == "ok" and "Lens" in res.svg
    assert "<a" not in res.svg and "href" not in res.svg


def test_limits_and_floor():
    rect = '<rect x="1" y="1" width="2" height="2"/>'
    assert svgsafe.sanitize(f"<svg {V}>" + "<g/>" * 2000 + "</svg>").reason == "too_many"  # 2,001 with the root
    assert svgsafe.sanitize(f"<svg {V}>" + "<g/>" * 1998 + rect + "</svg>").reason == "ok"
    assert svgsafe.sanitize(f"<svg {V}>" + "<g>" * 24 + rect + "</g>" * 24 + "</svg>").reason == "too_deep"
    assert svgsafe.sanitize(f"<svg {V}>" + "<g>" * 22 + rect + "</g>" * 22 + "</svg>").reason == "ok"
    for vb in ("", 'viewBox="0 0 a b"', 'viewBox="0 0 0 10"', 'viewBox="0 0 10 10001"', 'viewBox="0 0 nan 1"'):
        assert svgsafe.sanitize(f"<svg {vb}>{rect}</svg>").reason == "no_viewbox"
    assert svgsafe.sanitize("<html><body/></html>").reason == "not_svg"
    assert svgsafe.sanitize('<x:svg xmlns:x="urn:other" viewBox="0 0 1 1"/>').reason == "not_svg"
    assert svgsafe.sanitize(f"<svg {V}><script>alert(1)</script></svg>").reason == "empty"
    assert svgsafe.sanitize(f"<svg {V}><rect").reason == "unparseable"

    def font(value):
        return svgsafe.sanitize(f'<svg {V}><text x="1" y="1" font-size="{value}">A</text></svg>').svg
    assert 'font-size="22"' in font("10")
    assert 'font-size="30"' in font("30px")
    assert "font-size" not in font("1em")
    assert 'fill="url(#g1)"' in svgsafe.sanitize(f'<svg {V}><rect width="1" height="1" fill="url(#g1)"/></svg>').svg
    assert "fill" not in svgsafe.sanitize(f'<svg {V}><rect width="1" height="1" fill="url(https://x)"/></svg>').svg
    assert '<use href="#a">' in svgsafe.sanitize(f'<svg {V}><use href="#a"/></svg>').svg
    assert "href" not in svgsafe.sanitize(f'<svg {V}><use href="b.svg#a"/></svg>').svg
    big = f"<svg {V}><text x=\"1\" y=\"1\">{'A' * 48_001}</text></svg>"
    assert svgsafe.sanitize(big).reason == "too_big"
    rng = random.Random(10)
    for _ in range(200):
        junk = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 400))).decode("latin-1")
        res = svgsafe.sanitize(junk)
        assert res.svg is None or res.reason == "ok"
    assert svgsafe.sanitize(None).svg is None and svgsafe.sanitize(b"<svg/>").svg is None


def test_only_stdlib_imports():
    src = (ROOT / "common" / "svgsafe.py").read_text()
    mods = {m.split(".")[0] for m in re.findall(r"^\s*(?:import|from) ([\w.]+)", src, re.M)}
    assert mods <= {"re", "math", "xml", "dataclasses", "typing"}
    assert "defusedxml" not in src
