"""GET /central/engines (gateway/central.py): the router's engine list, allowlisted.

Fixtures and the auth-token stubs are reused from tests/test_central.py
(`stub_router` is autouse there and stays autouse here). The router stub has no
/engines route yet, so these tests monkeypatch `central._router_json` directly;
the vault and the mini PC router are never touched.
"""

from __future__ import annotations

import pytest

from gateway import central
from tests.test_central import IOS, _call, stub_router  # noqa: F401  (autouse fixture)


def _stub_engines(monkeypatch: pytest.MonkeyPatch, data):
    async def fake_json(method: str, path: str, body=None):
        assert (method, path) == ("GET", "/engines")
        if isinstance(data, Exception):
            raise data
        return data

    monkeypatch.setattr(central, "_router_json", fake_json)


ENGINES = {"engines": [
    {"id": "glm-4", "label": "GLM", "kind": "cloud", "available": True, "default": True,
     "reason": None, "base_url": "http://router.internal", "key_env": "SECRET_NAME",
     "model": "secret-model"},
    {"id": "codex", "label": "Codex", "kind": "cli", "available": False, "default": False,
     "reason": "not installed", "junk": 1},
    "not-a-dict",
]}
ALLOWED = {"engines": [
    {"id": "glm-4", "label": "GLM", "kind": "cloud", "available": True, "default": True,
     "reason": None},
    {"id": "codex", "label": "Codex", "kind": "cli", "available": False, "default": False,
     "reason": "not installed"},
]}


def test_engines_allowlisted(monkeypatch: pytest.MonkeyPatch, stub_router) -> None:
    _stub_engines(monkeypatch, ENGINES)

    resp = _call("GET", "/central/engines", headers=IOS)

    assert resp.status_code == 200, resp.text
    assert resp.json() == ALLOWED
    for item in resp.json()["engines"]:
        assert set(item) == set(central._ENGINE_KEYS)


def test_engines_without_token_is_401(stub_router) -> None:
    assert _call("GET", "/central/engines").status_code == 401


def test_engines_router_error_is_502(monkeypatch: pytest.MonkeyPatch, stub_router) -> None:
    _stub_engines(monkeypatch, central.RouterError(central.ROUTER_UNAVAILABLE))

    resp = _call("GET", "/central/engines", headers=IOS)

    assert resp.status_code == 502
    assert resp.json()["detail"] == "router unavailable"


@pytest.mark.parametrize("data", [{"engines": "nope"}, {"engines": None}, {}])
def test_engines_router_without_a_list_is_502(monkeypatch: pytest.MonkeyPatch,
                                              stub_router, data) -> None:
    _stub_engines(monkeypatch, data)

    resp = _call("GET", "/central/engines", headers=IOS)

    assert resp.status_code == 502
