"""Quick-ask (/ask) checks: routing fallbacks, vault retrieval, endpoint flow.

No network: OpenRouter is stubbed at the `_openrouter` seam, routing at
`route_question`, the brain at `mac.voice.brain.reply`; retrieval runs against
temp-dir vaults (never the real one, never Secrets).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from common import config
from conftest import AUTH_HEADER, IOS_AUTH_HEADER, IOS_TOKEN

TEST_SETTINGS = {
    "openrouter_key": "test-openrouter-key",
    "quick_model": "google/gemini-2.5-flash-lite",
    "router_model": "typesafe/jev-1.13",
}


@pytest.fixture
def quick_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stubbed auth token + settings seam: no vault read, no OpenRouter access.

    /ask is iOS-only, so only the ios_token name resolves a token here.
    """
    from gateway import quick

    monkeypatch.setattr(
        config, "get_token",
        lambda path=None, name="token": IOS_TOKEN if name == "ios_token" else None,
    )
    monkeypatch.setattr(quick, "_settings", lambda: dict(TEST_SETTINGS))


def _post(
    payload: dict, headers: dict[str, str] | None = IOS_AUTH_HEADER
) -> httpx.Response:
    """POST /ask against a minimal app holding only the quick router.

    Defaults to the iOS header — /ask is phone-app-only by design.
    """
    from gateway.quick import router

    async def run() -> httpx.Response:
        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/ask", json=payload, headers=headers or {})

    return asyncio.run(run())


def test_route_heuristic_vault_when_hits(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Jev seam failing + vault hits ≥1 → heuristic says vault."""
    from gateway import quick

    def boom(*args, **kwargs):
        raise RuntimeError("jev down")

    monkeypatch.setattr(quick, "_openrouter", boom)
    assert quick.route_question("what is the <project> resend key", 3) == "vault"


def test_route_heuristic_web_when_no_hits(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Jev seam failing + zero vault hits → heuristic says web."""
    from gateway import quick

    def boom(*args, **kwargs):
        raise RuntimeError("jev down")

    monkeypatch.setattr(quick, "_openrouter", boom)
    assert quick.route_question("who is the ceo of amd", 0) == "web"


def test_route_jev_choice_respected(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A conforming Jev reply wins over the heuristic; request body pinned."""
    from gateway import quick

    seen: dict = {}

    def fake_openrouter(url, payload, *, key, timeout_s):
        seen["url"], seen["payload"] = url, payload
        return {
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": "web",
                    "confidence": 0.9,
                    "probabilities": {"web": 0.9, "vault": 0.1, "brain": 0.0},
                }
            }
        }

    monkeypatch.setattr(quick, "_openrouter", fake_openrouter)
    # Vault hits say vault, Jev says web — Jev wins.
    assert quick.route_question("what is the <project> resend key", 5) == "web"
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert seen["payload"]["model"] == "typesafe/jev-1.13"
    question = seen["payload"]["questions"]["route"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"vault", "web", "brain"}


def test_retrieve_finds_planted_fact(tmp_path) -> None:
    from gateway.quick import retrieve

    notes = tmp_path / "Projects"
    notes.mkdir()
    (notes / "<project>.md").write_text(
        "filler line\nThe <project> resend key is RESEND_KEY_XYZ in the ledger.\nfiller\n",
        encoding="utf-8",
    )

    snippets, hits = retrieve("what is the <project> resend key", vault_root=tmp_path)

    assert hits >= 1
    assert snippets
    assert any("RESEND_KEY_XYZ" in s for s in snippets)


def test_retrieve_excludes_secrets(tmp_path) -> None:
    from gateway.quick import retrieve

    secrets = tmp_path / "Secrets"
    secrets.mkdir()
    (secrets / "Projects.md").write_text(
        "the wakanda passphrase is SECRET_MARKER_9\n", encoding="utf-8"
    )

    snippets, hits = retrieve("wakanda passphrase", vault_root=tmp_path)

    # The only matching note lives under Secrets/ — pruned, so zero hits.
    assert hits == 0
    assert snippets == []
    assert all("SECRET_MARKER_9" not in s for s in snippets)


def test_retrieve_caps_snippets(tmp_path) -> None:
    from gateway.quick import retrieve

    for i in range(10):
        (tmp_path / f"note{i}.md").write_text(
            "wakanda " + "x" * 400 + "\n", encoding="utf-8"
        )

    snippets, hits = retrieve("wakanda", vault_root=tmp_path)

    assert hits >= 10
    assert len(snippets) <= 6
    assert sum(len(s) for s in snippets) <= 2500


def test_ask_vault_hit(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway import quick

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: (["<project>: RESEND_API_KEY"], 4))
    monkeypatch.setattr(quick, "route_question", lambda q, vault_hits: "vault")

    seen: dict = {}

    def fake_openrouter(url, payload, *, key, timeout_s):
        seen["url"], seen["payload"] = url, payload
        return {"choices": [{"message": {"content": "RESEND_API_KEY"}}]}

    monkeypatch.setattr(quick, "_openrouter", fake_openrouter)

    resp = _post({"q": "what is the <project> resend key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "RESEND_API_KEY"
    assert body["route"] == "vault"
    assert body["source"] is None
    assert isinstance(body["ms"], int) and body["ms"] >= 0
    # Generative request shape: model slug + exact terse system instruction.
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["payload"]["model"] == "google/gemini-2.5-flash-lite"
    system = seen["payload"]["messages"][0]["content"]
    assert "Answer with exactly the answer — one short sentence or phrase, no preamble." in system
    assert "NOVAULT" in system
    # Same surface without the header → 401.
    assert _post({"q": "x"}, headers=None).status_code == 401


def test_ask_rejects_shared_token(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared token (PC disk / Shortcut role) must not read the vault via /ask.

    quick.ask stays reachable in-process for /siri/hey; over HTTP the endpoint
    is ios_token-only, so the shared-token header gets no answer body.
    """
    from gateway import quick

    monkeypatch.setattr(
        quick, "retrieve", lambda q, vault_root=None: (["<project>: RESEND_API_KEY"], 4)
    )
    monkeypatch.setattr(quick, "route_question", lambda q, vault_hits: "vault")

    resp = _post({"q": "what is the <project> resend key"}, headers=AUTH_HEADER)

    assert resp.status_code in (401, 403)
    assert "answer" not in resp.json()


def test_ask_no_vault_answer_goes_web(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway import quick

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: (["irrelevant note"], 2))
    monkeypatch.setattr(quick, "route_question", lambda q, vault_hits: "vault")

    calls: list[dict] = []

    def fake_openrouter(url, payload, *, key, timeout_s):
        calls.append(payload)
        if len(calls) == 1:  # vault generative call
            return {"choices": [{"message": {"content": "NOVAULT"}}]}
        return {
            "choices": [
                {"message": {"content": "Saturn has several ring groups. https://science.nasa.gov/saturn/rings/"}}
            ]
        }

    monkeypatch.setattr(quick, "_openrouter", fake_openrouter)

    resp = _post({"q": "how many rings does saturn have"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] == "web"
    assert body["source"] == "https://science.nasa.gov/saturn/rings/"
    assert len(calls) == 2
    # Web request shape: the openrouter:web_search server tool, ≤3 results.
    assert calls[1]["tools"] == [
        {"type": "openrouter:web_search", "parameters": {"max_results": 3}}
    ]


def test_ask_openrouter_failure_falls_back_to_brain(
    quick_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway import quick
    from mac.voice import brain

    def boom(*args, **kwargs):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: ([], 0))
    monkeypatch.setattr(quick, "route_question", lambda q, vault_hits: "vault")
    monkeypatch.setattr(quick, "_openrouter", boom)
    monkeypatch.setattr(brain, "reply", lambda transcript, timeout_s=60: "stub brain answer")

    resp = _post({"q": "tell me about the <project> ledger"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] == "brain"
    assert body["answer"] == "stub brain answer"
    assert body["source"] is None


def test_ask_brain_skip_short_circuits(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    from gateway import quick
    from mac.voice import brain

    def spy(*args, **kwargs):
        raise AssertionError("brain.reply must not be called when brain=skip")

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: ([], 0))
    monkeypatch.setattr(quick, "route_question", lambda q, vault_hits: "brain")
    monkeypatch.setattr(brain, "reply", spy)

    resp = _post({"q": "write me a haiku about backups", "brain": "skip"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] is None
    assert body["route"] == "brain"
    assert body["source"] is None
    assert isinstance(body["ms"], int) and body["ms"] >= 0
