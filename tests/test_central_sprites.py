"""Role sprites (Ask 2): public (no bearer) SVG + PNG per role.

Ownership split (fable 2, 2026-10-16): the real art lives under
gateway/static/sprites/ (fable 2's own worktree/branch, never touched here).
This module's route serves a static file from there when one exists, and
falls back to gateway/sprites.py's procedural generator otherwise — these
tests cover both paths.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway import central, sprites


def _get(path: str, headers: dict | None = None) -> httpx.Response:
    from gateway.app import app

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
            return await client.get(path, headers=headers)

    return asyncio.run(run())


def test_sprites_served_without_auth():
    resp = _get("/central/sprites/BUILDER.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.text.startswith("<svg")


def test_sprite_png_served_without_auth():
    resp = _get("/central/sprites/BUILDER@2x.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("scale", ["1", "2", "3"])
def test_every_scale_served(scale):
    resp = _get(f"/central/sprites/BUILDER@{scale}x.png")
    assert resp.status_code == 200 and resp.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("role", sprites.ROLES)
def test_every_contract_role_has_a_sprite(role):
    svg = _get(f"/central/sprites/{role}.svg")
    png = _get(f"/central/sprites/{role}@2x.png")
    assert svg.status_code == 200 and svg.text.startswith("<svg")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_unknown_role_falls_back_to_worker_not_404():
    svg = _get("/central/sprites/NOPE_NOT_A_ROLE.svg")
    assert svg.status_code == 200
    assert svg.text == sprites.svg_for("WORKER")
    png = _get("/central/sprites/NOPE_NOT_A_ROLE@1x.png")
    assert png.status_code == 200
    assert png.content == sprites.png_bytes_for("WORKER", sprites.PNG_SCALES["1x"])


def test_sprite_route_is_cacheable():
    resp = _get("/central/sprites/OWNER.svg")
    assert "max-age" in resp.headers["cache-control"]


def test_sprites_roles_listing():
    resp = _get("/central/sprites")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["roles"]) == set(sprites.ROLES)
    # Contract roles (fable2-watchdog-roles-app-side.md) must all be present.
    for role in (
        "ARCHITECT", "RESEARCHER", "REVIEWER", "BUILDER", "SCOUT", "FIXER", "VERIFIER",
        "SECURITY", "DESIGNER", "MARKETER", "SPEC_WRITER", "GLM_WORKER",
    ):
        assert role in body["roles"]


# --- fable 2's static art, once it lands, wins over the generator -----------------

def test_static_svg_file_wins_over_generator(tmp_path, monkeypatch):
    (tmp_path / "BUILDER.svg").write_text("<svg data-source='fable2-art'></svg>")
    monkeypatch.setattr(central, "SPRITES_STATIC_DIR", tmp_path)
    resp = _get("/central/sprites/BUILDER.svg")
    assert resp.status_code == 200
    assert "fable2-art" in resp.text


def test_static_png_file_wins_over_generator(tmp_path, monkeypatch):
    real_png = b"\x89PNG\r\n\x1a\nnot-really-a-real-png-but-thats-fine-for-this-test"
    (tmp_path / "BUILDER@2x.png").write_bytes(real_png)
    monkeypatch.setattr(central, "SPRITES_STATIC_DIR", tmp_path)
    resp = _get("/central/sprites/BUILDER@2x.png")
    assert resp.status_code == 200
    assert resp.content == real_png


def test_static_file_missing_falls_back_to_generator(tmp_path, monkeypatch):
    # tmp_path exists but has no sprite files in it at all.
    monkeypatch.setattr(central, "SPRITES_STATIC_DIR", tmp_path)
    resp = _get("/central/sprites/BUILDER.svg")
    assert resp.status_code == 200
    assert resp.text == sprites.svg_for("BUILDER")


def test_static_1x_png_bare_filename_matches_fable2_naming(tmp_path, monkeypatch):
    """fable 2's build_sprites.py names the 1x file `{role}.png` (bare, no
    `@1x` suffix) — only @2x/@3x carry a scale suffix. Confirmed live against
    her worktree (jarvis-companion-sprites/gateway/static/sprites/) 2026-10-16."""
    real_png = b"\x89PNG\r\n\x1a\nreal-1x-art-bare-filename"
    (tmp_path / "BUILDER.png").write_bytes(real_png)
    monkeypatch.setattr(central, "SPRITES_STATIC_DIR", tmp_path)
    resp = _get("/central/sprites/BUILDER@1x.png")
    assert resp.status_code == 200
    assert resp.content == real_png


# --- roles.json drives known-role resolution, not a hardcoded list ---------------

def test_roles_json_role_resolves_even_if_not_in_generator_set(tmp_path, monkeypatch):
    """A role fable 2's roles.json declares should resolve to itself (and
    therefore try her static file) even if it were somehow outside
    sprites.ROLES — roles.json is the authority on what she's shipped art for."""
    import json as _json

    (tmp_path / "roles.json").write_text(_json.dumps([{"role": "BUILDER", "label": "Builder", "file": "BUILDER.svg"}]))
    (tmp_path / "BUILDER.svg").write_text("<svg data-source='fable2-art'></svg>")
    monkeypatch.setattr(central, "SPRITES_STATIC_DIR", tmp_path)
    resp = _get("/central/sprites/BUILDER.svg")
    assert resp.status_code == 200
    assert "fable2-art" in resp.text


def test_roles_not_yet_in_roles_json_still_served_by_generator(tmp_path, monkeypatch):
    """EXPLORER/WORKER aren't in fable 2's v1 roles.json yet (her v2 note) —
    they must still resolve via sprites.ROLES and get a generated sprite,
    not fall through to a 404 or an unrelated role."""
    import json as _json

    (tmp_path / "roles.json").write_text(_json.dumps([{"role": "BUILDER", "label": "Builder", "file": "BUILDER.svg"}]))
    monkeypatch.setattr(central, "SPRITES_STATIC_DIR", tmp_path)
    resp = _get("/central/sprites/EXPLORER.svg")
    assert resp.status_code == 200
    assert resp.text == sprites.svg_for("EXPLORER")


def test_missing_roles_json_falls_back_to_generator_set(tmp_path, monkeypatch):
    # tmp_path has no roles.json at all (pre-merge state, matches today).
    monkeypatch.setattr(central, "SPRITES_STATIC_DIR", tmp_path)
    resp = _get("/central/sprites/BUILDER.svg")
    assert resp.status_code == 200
    assert resp.text == sprites.svg_for("BUILDER")


def test_malformed_roles_json_does_not_crash(tmp_path, monkeypatch):
    (tmp_path / "roles.json").write_text("not valid json {{{")
    monkeypatch.setattr(central, "SPRITES_STATIC_DIR", tmp_path)
    resp = _get("/central/sprites/BUILDER.svg")
    assert resp.status_code == 200
    assert resp.text == sprites.svg_for("BUILDER")
