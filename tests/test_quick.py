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
    """Jev seam failing + best note ≥2 distinct keywords with a name match → vault."""
    from gateway import quick

    def boom(*args, **kwargs):
        raise RuntimeError("jev down")

    monkeypatch.setattr(quick, "_openrouter", boom)
    assert quick.route_question("what is the <project> resend key", (3, 1)) == "vault"


def test_route_heuristic_web_when_no_hits(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Jev seam failing + no qualifying note → heuristic says web."""
    from gateway import quick

    def boom(*args, **kwargs):
        raise RuntimeError("jev down")

    monkeypatch.setattr(quick, "_openrouter", boom)
    assert quick.route_question("who is the ceo of amd", None) == "web"


def test_route_heuristic_web_when_best_has_no_name_match(
    quick_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best note matched ≥2 keywords but only in the body → web, not vault."""
    from gateway import quick

    def boom(*args, **kwargs):
        raise RuntimeError("jev down")

    monkeypatch.setattr(quick, "_openrouter", boom)
    assert quick.route_question("how many rings does saturn have", (2, 0)) == "web"


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

    snippets, best = retrieve("what is the <project> resend key", vault_root=tmp_path)

    # 2 distinct keywords matched (<project>, resend — "key" is under the 4-letter
    # bar), <project> in the path/name.
    assert best == (2, 1)
    assert snippets
    assert any("RESEND_KEY_XYZ" in s for s in snippets)


def test_retrieve_excludes_secrets(tmp_path) -> None:
    from gateway.quick import retrieve

    secrets = tmp_path / "Secrets"
    secrets.mkdir()
    (secrets / "Projects.md").write_text(
        "the wakanda passphrase is SECRET_MARKER_9\n", encoding="utf-8"
    )

    snippets, best = retrieve("wakanda passphrase", vault_root=tmp_path)

    # The only matching note lives under Secrets/ — pruned, so nothing qualifies.
    assert best is None
    assert snippets == []
    assert all("SECRET_MARKER_9" not in s for s in snippets)


def test_retrieve_caps_snippets(tmp_path) -> None:
    from gateway.quick import retrieve

    for i in range(10):
        (tmp_path / f"note{i}.md").write_text(
            "wakanda " + "x" * 400 + "\n", encoding="utf-8"
        )

    snippets, best = retrieve("wakanda", vault_root=tmp_path)

    # Single-keyword question → every note qualifies, but only the top 4 ship.
    assert best == (1, 0)
    assert len(snippets) == 4
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
    # Web request shape: the openrouter:web_search server tool, exa engine, ≤3 results.
    assert calls[1]["tools"] == [
        {"type": "openrouter:web_search", "parameters": {"engine": "exa", "max_results": 3}}
    ]
    assert "UNKNOWN" in calls[1]["messages"][0]["content"]
    assert "ONLY from the web search results" in calls[1]["messages"][0]["content"]


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


def _web_route_env(monkeypatch: pytest.MonkeyPatch, web_content: str) -> list[dict]:
    """Stub retrieve/routing/_openrouter so the web call returns `web_content`."""
    from gateway import quick

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: ([], None))
    monkeypatch.setattr(quick, "route_question", lambda q, vault_hits: "web")
    calls: list[dict] = []

    def fake_openrouter(url, payload, *, key, timeout_s):
        calls.append(payload)
        return {"choices": [{"message": {"content": web_content}}]}

    monkeypatch.setattr(quick, "_openrouter", fake_openrouter)
    return calls


def test_ask_web_echo_falls_back_to_brain(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A web reply echoing the question is no answer → brain (auto)."""
    from mac.voice import brain

    q = "what is the capital of Chile?"
    calls = _web_route_env(monkeypatch, q)  # the model parrots the question back
    monkeypatch.setattr(brain, "reply", lambda transcript, timeout_s=60: "stub brain answer")

    resp = _post({"q": q})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "stub brain answer"
    assert body["route"] == "brain"
    assert len(calls) == 1  # no second web attempt after the echo


def test_ask_web_echo_skip_returns_null(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Echo + brain=skip → no brain call, {"answer": null, "route": "web"}."""
    from mac.voice import brain

    def spy(*args, **kwargs):
        raise AssertionError("brain.reply must not be called when brain=skip")

    q = "what is the capital of Chile?"
    _web_route_env(monkeypatch, q)
    monkeypatch.setattr(brain, "reply", spy)

    resp = _post({"q": q, "brain": "skip"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] is None
    assert body["route"] == "web"


def test_ask_web_unknown_brain_down(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Web UNKNOWN + brain failure → "I don't know.", route brain."""
    from mac.voice import brain

    def boom(*args, **kwargs):
        raise RuntimeError("brain down")

    q = "what is the capital of Chile?"
    _web_route_env(monkeypatch, "UNKNOWN")
    monkeypatch.setattr(brain, "reply", boom)

    resp = _post({"q": q})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "I don't know."
    assert body["route"] == "brain"


def test_retrieve_ranks_name_match_above_body_only(tmp_path) -> None:
    """A keyword in the note's path/name outranks body-only matches; a note
    matching a single common keyword never qualifies."""
    from gateway.quick import retrieve

    projects = tmp_path / "Projects"
    projects.mkdir()
    (projects / "jarvis-companion.md").write_text(
        "jarvis companion gateway settings NAME_MATCH_MARKER\n", encoding="utf-8"
    )
    (tmp_path / "bodyonly.md").write_text(
        "the jarvis companion gateway config lives here BODY_MATCH_MARKER\n", encoding="utf-8"
    )
    (tmp_path / "onlyone.md").write_text(
        "config values are stored in env files ONE_MATCH_MARKER\n", encoding="utf-8"
    )

    snippets, best = retrieve(
        "where is the jarvis companion gateway config", vault_root=tmp_path
    )

    # jarvis/companion/gateway matched (config is bodyonly.md-only); the two
    # path/name keywords make this note outrank the 4-keyword body-only match.
    assert best == (3, 2)
    name_i = next(i for i, s in enumerate(snippets) if "NAME_MATCH_MARKER" in s)
    body_i = next(i for i, s in enumerate(snippets) if "BODY_MATCH_MARKER" in s)
    assert name_i < body_i
    assert all("ONE_MATCH_MARKER" not in s for s in snippets)


def test_retrieve_unrelated_vault_routes_web(tmp_path, quick_env, monkeypatch) -> None:
    """General-knowledge question vs unrelated notes → zero qualifying files,
    heuristic route web."""
    from gateway import quick
    from gateway.quick import retrieve

    notes = tmp_path / "Notes"
    notes.mkdir()
    (notes / "cooking.md").write_text("sourdough starter needs feeding daily.\n", encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("jev down")

    snippets, best = retrieve("what is the capital of Chile?", vault_root=tmp_path)
    assert snippets == []
    assert best is None
    monkeypatch.setattr(quick, "_openrouter", boom)
    assert quick.route_question("what is the capital of Chile?", best) == "web"
