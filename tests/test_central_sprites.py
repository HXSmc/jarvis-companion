"""Role sprites (Ask 2): public (no bearer) SVG + JSON grid per role, served
from gateway/sprites.py's single pixel-grid source of truth."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway import sprites


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


def test_sprite_json_served_without_auth():
    resp = _get("/central/sprites/BUILDER.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"] == "BUILDER"
    assert body["w"] == sprites.GRID_W and body["h"] == sprites.GRID_H
    assert len(body["rows"]) == sprites.GRID_H
    assert len(body["rows"][0]) == sprites.GRID_W


@pytest.mark.parametrize("role", sprites.ROLES)
def test_every_contract_role_has_a_sprite(role):
    svg = _get(f"/central/sprites/{role}.svg")
    j = _get(f"/central/sprites/{role}.json")
    assert svg.status_code == 200 and svg.text.startswith("<svg")
    assert j.status_code == 200 and j.json()["role"] == role


def test_unknown_role_falls_back_to_worker_not_404():
    svg = _get("/central/sprites/NOPE_NOT_A_ROLE.svg")
    assert svg.status_code == 200
    assert svg.text == sprites.svg_for("WORKER")
    j = _get("/central/sprites/NOPE_NOT_A_ROLE.json")
    assert j.status_code == 200 and j.json()["role"] == "WORKER"


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
