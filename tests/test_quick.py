"""Quick-ask (/ask) checks: routing fallbacks, vault retrieval, endpoint flow.

No network: OpenRouter is stubbed at the `_openrouter` seam, routing at
`route_question`, the local web CLIs at `subprocess.run`, the brain at
`mac.voice.brain.reply`; retrieval runs against temp-dir vaults (never the
real one, never Secrets). No real agy/claude call ever happens.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

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
    # Hermetic ask() runs: _resolve_project gets a fixed token set, never the
    # real vault's Projects glob.
    monkeypatch.setattr(
        quick, "_project_tokens", lambda: {"<project>", "notekit", "jarvis", "companion"}
    )


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


def _stub_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agy_stdout: str = "",
    agy_rc: int = 0,
    claude_stdout: str = "",
    claude_rc: int = 0,
    agy_error: Exception | None = None,
) -> list[dict]:
    """Stub `subprocess.run` for the two web CLIs; records every call.

    agy and claude are told apart by argv[0]; each returns its own stdout/rc.
    """
    from gateway import quick

    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        if argv[0] == "agy":
            if agy_error is not None:
                raise agy_error
            return subprocess.CompletedProcess(argv, agy_rc, stdout=agy_stdout, stderr="")
        return subprocess.CompletedProcess(argv, claude_rc, stdout=claude_stdout, stderr="")

    monkeypatch.setattr(quick.subprocess, "run", fake_run)
    return calls


def test_ask_no_vault_answer_goes_web(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Vault NOVAULT → the local agy CLI answers; a URL in the line is the source."""
    from gateway import quick

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: (["irrelevant note"], 2))
    monkeypatch.setattr(quick, "route_question", lambda q, vault_hits: "vault")
    monkeypatch.setattr(
        quick, "_openrouter",
        lambda url, payload, *, key, timeout_s: {
            "choices": [{"message": {"content": "NOVAULT"}}]
        },
    )
    calls = _stub_cli(
        monkeypatch,
        agy_stdout="Saturn has several ring groups. https://science.nasa.gov/saturn/rings/",
    )

    resp = _post({"q": "how many rings does saturn have"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] == "web"
    assert body["answer"] == "Saturn has several ring groups. https://science.nasa.gov/saturn/rings/"
    assert body["source"] == "https://science.nasa.gov/saturn/rings/"
    assert [c["argv"][0] for c in calls] == ["agy"]  # agy answered — no claude call


def test_ask_web_agy_argv_and_env(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """agy call shape: exact argv prefix, pinned prompt, AGY_KIND=quick, 40 s cap."""
    calls = _web_route_env(monkeypatch, agy_stdout="Santiago")

    resp = _post({"q": "what is the capital of Chile?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Santiago"
    assert body["route"] == "web"
    assert body["source"] is None  # no URL in the answer line

    (call,) = calls
    assert call["argv"][:3] == ["agy", "--dangerously-skip-permissions", "--print"]
    prompt = call["argv"][3]
    assert prompt.startswith("Answer with exactly the answer and nothing else")
    # _resolve_project rebuilds from the word list → normalized (lowercase, no "?")
    assert prompt.endswith("Question: what is the capital of chile")
    assert "reply exactly UNKNOWN" in prompt
    assert call["env"]["AGY_KIND"] == "quick"
    assert call["timeout"] == 40
    assert call["capture_output"] is True and call["text"] is True


def test_ask_web_agy_chatter_ignored(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress chatter before the answer is ignored — the LAST line ships."""
    _web_route_env(monkeypatch, agy_stdout="Searching the web...\nworking...\nSantiago")

    resp = _post({"q": "what is the capital of Chile?"})
    assert resp.status_code == 200
    assert resp.json()["answer"] == "Santiago"


def test_ask_web_agy_timeout_claude_fallback(
    quick_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agy timeout → the claude fallback runs with WebSearch + the brain config dir."""
    calls = _web_route_env(
        monkeypatch,
        agy_error=subprocess.TimeoutExpired(["agy"], 25),
        claude_stdout="Santiago",
    )

    resp = _post({"q": "what is the capital of Chile?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Santiago"
    assert body["route"] == "web"

    assert [c["argv"][0] for c in calls] == ["agy", "claude"]
    claude = calls[1]
    assert claude["argv"][:2] == ["claude", "-p"]
    i_tools = claude["argv"].index("--tools")
    assert claude["argv"][i_tools + 1] == "WebSearch"
    i_turns = claude["argv"].index("--max-turns")
    assert claude["argv"][i_turns + 1] == "3"
    assert claude["env"]["CLAUDE_CONFIG_DIR"].endswith(".claude-jarvis-brain")
    assert claude["input"].endswith("Question: what is the capital of chile")
    # remaining share of the shared deadline — full 40 s when agy failed instantly
    assert 5 <= claude["timeout"] <= 40


def test_ask_web_claude_not_logged_in_brain_down(
    quick_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agy fails, claude says "Not logged in" (owner gate not open yet) → no
    answer → brain; brain down → null."""
    from mac.voice import brain

    def boom(*args, **kwargs):
        raise RuntimeError("brain down")

    _web_route_env(monkeypatch, agy_rc=1, claude_stdout="Not logged in")
    monkeypatch.setattr(brain, "reply", boom)

    resp = _post({"q": "what is the capital of Chile?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] is None
    assert body["route"] == "brain"


def test_ask_web_engine_off_no_subprocess(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """web_engine: off → the brain answers directly, no CLI subprocess at all."""
    from gateway import quick
    from mac.voice import brain

    monkeypatch.setattr(quick, "_settings", lambda: dict(TEST_SETTINGS, web_engine="off"))

    def spy(argv, **kwargs):
        raise AssertionError("no subprocess may run when web_engine is off")

    monkeypatch.setattr(quick.subprocess, "run", spy)
    monkeypatch.setattr(brain, "reply", lambda transcript, timeout_s=60: "stub brain answer")

    resp = _post({"q": "what is the capital of Chile?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "stub brain answer"
    assert body["route"] == "brain"


def test_ask_web_engine_claude_skips_agy(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """web_engine: claude → agy is never called; claude answers directly."""
    from gateway import quick

    monkeypatch.setattr(quick, "_settings", lambda: dict(TEST_SETTINGS, web_engine="claude"))
    calls = _stub_cli(monkeypatch, claude_stdout="Santiago")

    resp = _post({"q": "what is the capital of Chile?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Santiago"
    assert body["route"] == "web"
    assert [c["argv"][0] for c in calls] == ["claude"]


def test_web_path_openrouter_search_removed() -> None:
    """The OpenRouter web_search server tool is gone from quick.py (W2)."""
    src = Path(__file__).resolve().parents[1] / "gateway" / "quick.py"
    assert "openrouter:web_search" not in src.read_text(encoding="utf-8")


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
    # Both web CLIs fail too — the brain is the last resort.
    _stub_cli(monkeypatch, agy_rc=1, claude_rc=1)
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


def _web_route_env(monkeypatch: pytest.MonkeyPatch, **cli_kwargs) -> list[dict]:
    """Stub retrieve/routing so the web path runs with the given CLI outputs.

    Both engines get the same stdout unless the caller splits them via
    agy_stdout/claude_stdout kwargs (see _stub_cli).
    """
    from gateway import quick

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: ([], None))
    monkeypatch.setattr(quick, "route_question", lambda q, vault_hits: "web")
    shared = cli_kwargs.pop("web_content", None)
    if shared is not None:
        cli_kwargs.setdefault("agy_stdout", shared)
        cli_kwargs.setdefault("claude_stdout", shared)
    return _stub_cli(monkeypatch, **cli_kwargs)


def test_ask_web_echo_falls_back_to_brain(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A web reply echoing the question is no answer → brain (auto)."""
    from mac.voice import brain

    q = "what is the capital of Chile?"
    # Both CLIs parrot the question back — never an answer.
    calls = _web_route_env(monkeypatch, web_content=q)
    monkeypatch.setattr(brain, "reply", lambda transcript, timeout_s=60: "stub brain answer")

    resp = _post({"q": q})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "stub brain answer"
    assert body["route"] == "brain"
    assert [c["argv"][0] for c in calls] == ["agy", "claude"]  # both engines tried once


def test_ask_web_echo_skip_returns_null(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Echo + brain=skip → no brain call, {"answer": null, "route": "web"}."""
    from mac.voice import brain

    def spy(*args, **kwargs):
        raise AssertionError("brain.reply must not be called when brain=skip")

    q = "what is the capital of Chile?"
    _web_route_env(monkeypatch, web_content=q)
    monkeypatch.setattr(brain, "reply", spy)

    resp = _post({"q": q, "brain": "skip"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] is None
    assert body["route"] == "web"


def test_ask_web_unknown_brain_down(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Web UNKNOWN + brain failure → null, route brain."""
    from mac.voice import brain

    def boom(*args, **kwargs):
        raise RuntimeError("brain down")

    q = "what is the capital of Chile?"
    _web_route_env(monkeypatch, web_content="UNKNOWN")
    monkeypatch.setattr(brain, "reply", boom)

    resp = _post({"q": q})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] is None
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


def test_ask_strong_vault_match_skips_jev(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Best note ≥2 distinct keywords with a name hit → vault-first: Jev is
    never called, the vault generative call answers."""
    from gateway import quick

    monkeypatch.setattr(
        quick, "retrieve", lambda q, vault_root=None: (["karajy: the co-founder is Sara"], (3, 1))
    )

    def jev_spy(*args, **kwargs):
        raise AssertionError("Jev must not be called on a strong vault match")

    monkeypatch.setattr(quick, "route_question", jev_spy)
    monkeypatch.setattr(
        quick, "_openrouter",
        lambda url, payload, *, key, timeout_s: {
            "choices": [{"message": {"content": "Sara, the co-founder"}}]
        },
    )

    resp = _post({"q": "who is the co-founder of Karajy?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Sara, the co-founder"
    assert body["route"] == "vault"


def test_ask_action_routes_brain_without_jev(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Imperative opener → brain with no Jev call; brain=skip → null/brain."""
    from gateway import quick
    from mac.voice import brain

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: (["safari tips"], None))

    def jev_spy(*args, **kwargs):
        raise AssertionError("Jev must not be called on an action intent")

    monkeypatch.setattr(quick, "route_question", jev_spy)
    monkeypatch.setattr(quick, "_openrouter", jev_spy)
    monkeypatch.setattr(brain, "reply", lambda transcript, timeout_s=60: "stub brain answer")

    resp = _post({"q": "open safari and search flights"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "stub brain answer"
    assert body["route"] == "brain"

    def brain_spy(*args, **kwargs):
        raise AssertionError("brain.reply must not be called when brain=skip")

    monkeypatch.setattr(brain, "reply", brain_spy)
    resp = _post({"q": "open safari and search flights", "brain": "skip"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] is None
    assert body["route"] == "brain"


def test_ask_jev_called_without_override(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No strong match, not an action → Jev decides (Decisions API is hit),
    then the agy CLI answers the web route."""
    from gateway import quick

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: ([], None))
    seen: list[str] = []

    def fake_openrouter(url, payload, *, key, timeout_s):
        seen.append(url)
        return {"answers": {"route": {"type": "choice", "choice": "web"}}}

    monkeypatch.setattr(quick, "_openrouter", fake_openrouter)
    _stub_cli(monkeypatch, agy_stdout="Santiago")

    resp = _post({"q": "what is the capital of Chile?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "Santiago"
    assert body["route"] == "web"
    assert quick._DECISIONS_URL in seen  # Jev was consulted


@pytest.mark.parametrize("failure", ["the brain is unavailable", "brain unavailable"])
def test_ask_brain_failure_sentinel(
    failure: str, quick_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brain's own failure text is not an answer → null."""
    from gateway import quick
    from mac.voice import brain

    monkeypatch.setattr(quick, "retrieve", lambda q, vault_root=None: ([], None))
    monkeypatch.setattr(quick, "route_question", lambda q, vault_hits: "brain")
    monkeypatch.setattr(brain, "reply", lambda transcript, timeout_s=60: failure)

    resp = _post({"q": "tell me about the <project> ledger"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] is None
    assert body["route"] == "brain"


def test_ask_web_refusal_falls_to_idontknow(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A web refusal ("Cannot fulfill request") is no answer → brain; brain
    down → null — never the refusal text itself."""
    from mac.voice import brain

    def boom(*args, **kwargs):
        raise RuntimeError("brain down")

    q = "what is the tallest building in Riyadh?"
    _web_route_env(monkeypatch, web_content="Cannot fulfill request")
    monkeypatch.setattr(brain, "reply", boom)

    resp = _post({"q": q})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] is None
    assert body["route"] == "brain"


def test_extract_question_trailing_clause() -> None:
    """STT gives no punctuation — a multi-intent utterance is answered by its
    trailing question clause; a plain question returns itself."""
    from gateway.quick import _extract_question

    utterance = (
        "doing well just keep on doing what you're doing make sure that you fix "
        "the brain issue and also check that you Jarvis phone do have brain now "
        "and can answer me questions for example can you answer me who are the "
        "co-founders of <name>"
    )
    assert _extract_question(utterance) == "who are the co-founders of <name>"
    assert _extract_question("who are the co-founders of <name>") == "who are the co-founders of <name>"


def test_resolve_project_aliases() -> None:
    """STT variants sharing the first 3 chars + ratio ≥0.5 resolve to the project
    token; 'towed' never does; my/the/our project → the default project."""
    from gateway.quick import _resolve_project

    tokens = {"<project>", "notekit"}
    assert "<project>" in _resolve_project("who are the co-founders of <name>", tokens).lower()
    towed = _resolve_project("who are the co-founders of towed", tokens).lower()
    assert "towed" in towed and "<project>" not in towed
    for phrase in ("my project", "the project", "our project"):
        assert "<project>" in _resolve_project(
            f"who are the co-founders of {phrase}", tokens
        ).lower()


def test_brain_failure_returns_null(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Brain stub raising + web stub no-answer → {"answer": None} — never a
    canned miss string the phone would speak verbatim."""
    from mac.voice import brain

    def boom(*args, **kwargs):
        raise RuntimeError("brain down")

    _web_route_env(monkeypatch, web_content="UNKNOWN")
    monkeypatch.setattr(brain, "reply", boom)

    resp = _post({"q": "what is the capital of Chile?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] is None
    assert body["route"] == "brain"


def test_brain_fallback_gets_vault_snippets(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Vault NOVAULT + web no-answer → the brain fallback prompt carries the
    vault snippets and the question — the toolless brain cannot read the vault
    itself, so the context rides along (R6)."""
    from gateway import quick
    from mac.voice import brain

    captured: list[str] = []

    def fake_reply(transcript, timeout_s=60):
        captured.append(transcript)
        return "stub brain answer"

    monkeypatch.setattr(
        quick, "retrieve",
        lambda q, vault_root=None: (["the <project> founders are Ali, <name> and <name>"], (3, 1)),
    )
    monkeypatch.setattr(
        quick, "_openrouter",
        lambda url, payload, *, key, timeout_s: {
            "choices": [{"message": {"content": "NOVAULT"}}]
        },
    )
    _stub_cli(monkeypatch, agy_rc=1, claude_rc=1)
    monkeypatch.setattr(brain, "reply", fake_reply)

    resp = _post({"q": "who are the co-founders of <project>"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "stub brain answer"
    assert body["route"] == "brain"
    assert captured, "brain.reply was never called"
    assert "the <project> founders are Ali, <name> and <name>" in captured[0]
    # _resolve_project rebuilds from the word list → normalized question text
    assert "Question: who are the co founders of <project>" in captured[0]


def test_ask_action_checked_on_raw_utterance(quick_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1: an action is detected on the RAW utterance — no extraction, no
    project resolution, straight to the brain with the full command."""
    from gateway import quick
    from mac.voice import brain

    q = "send <name> an email about what we discussed"

    def spy(text):
        raise AssertionError("extraction/resolution must not run on an action")

    monkeypatch.setattr(quick, "_extract_question", spy)
    monkeypatch.setattr(quick, "_resolve_project", spy)

    captured: list[str] = []

    def fake_reply(transcript, timeout_s=60):
        captured.append(transcript)
        return "stub brain answer"

    monkeypatch.setattr(brain, "reply", fake_reply)

    resp = _post({"q": q})
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] == "brain"
    assert body["answer"] == "stub brain answer"
    assert q in captured[0]  # the brain got the raw command


def test_extract_question_compound_keeps_subject() -> None:
    """R3: contiguous question clauses join — the trailing "when did it start"
    must not drop the "who are the co-founders" subject."""
    from gateway.quick import _extract_question

    q = "who are the co-founders of <project> and when did it start"
    assert _extract_question(q) == q


def test_extract_question_noncontiguous_question_excluded() -> None:
    """R3: an early question clause separated by non-question chatter ("what
    you're doing … check that …") stays out — only the trailing question ships."""
    from gateway.quick import _extract_question

    owner_call = (
        "doing well just keep on doing what you're doing make sure that you fix "
        "the brain issue and also check that you Jarvis phone do have brain now "
        "and can answer me questions for example can you answer me who are the "
        "co-founders of <name>"
    )
    assert _extract_question(owner_call) == "who are the co-founders of <name>"


def test_extract_question_keeps_tail_without_question_word() -> None:
    """R1: a trailing clause with no question word of its own is never split
    away — the text from the cut point to the END of the utterance ships."""
    from gateway.quick import _extract_question

    assert _extract_question("who are <name> and <name>") == "who are <name> and <name>"
    status = "what is the status of <project> and <name>"
    assert _extract_question(status) == status
    compound = "who are the co-founders of <project> and when did it start"
    assert _extract_question(compound) == compound
    owner = (
        "doing well just keep on doing what you're doing make sure that you fix "
        "the brain issue and also check that you Jarvis phone do have brain now "
        "and can answer me questions for example can you answer me who are the "
        "co-founders of <name>"
    )
    assert _extract_question(owner) == "who are the co-founders of <name>"


def test_resolve_project_requires_project_shape() -> None:
    """R2: a token is rewritten only in project-name position with a close,
    prefix-sharing, length-compatible candidate — common words never move."""
    from gateway.quick import _resolve_project

    tokens = {"<project>", "notekit", "dealwatch", "audit"}
    # must resolve
    assert "<project>" in _resolve_project("who are the co-founders of <name>", tokens)
    assert "<project>" in _resolve_project("the <name> project status", tokens)  # before "project"
    # must NOT resolve — each comes back unchanged
    for q in (
        "take a note",                                   # "note" follows "a", not a preposition
        "tell me about the deal with microsoft",          # "deal" follows "the"
        "what happened in 2025",                          # digits — not alphabetic
        "play the audio",                                 # "audio" follows "the", no preposition
    ):
        assert _resolve_project(q, tokens) == q


def test_resolve_project_rewrites_only_gated_occurrence() -> None:
    """R2: a repeated word is rewritten ONLY at the occurrence that passed the
    position gate — its twins elsewhere in the utterance never move."""
    from gateway.quick import _resolve_project

    tokens = {"<project>"}
    q = "<name> notes and a summary of <name>"
    # only the "<name>" after "of" resolves; the subject-position twin stays
    assert _resolve_project(q, tokens) == "<name> notes and a summary of <project>"


def test_resolve_project_never_rewrites_stopwords() -> None:
    """R2: a STOPWORDS token in gated position ("their" after "about") is
    skipped even when a project token is a near-exact match."""
    from gateway.quick import _resolve_project

    assert _resolve_project("notes about their plan", {"theirs"}) == "notes about their plan"


def test_web_answer_second_attempt_gets_remaining_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """R4: one shared deadline — the claude fallback's timeout is the REMAINING
    time after agy consumed part of the window, never the full 40 s again."""
    from gateway import quick

    def slow_agy(q):
        time.sleep(0.3)
        return None, None

    monkeypatch.setattr(quick, "_agy_answer", slow_agy)
    calls = _stub_cli(monkeypatch, claude_stdout="Santiago")

    answer, _source = asyncio.run(quick._web_answer("what is the capital of Chile?", "agy"))
    assert answer == "Santiago"
    (call,) = calls
    assert call["argv"][0] == "claude"
    assert 5 <= call["timeout"] <= quick.WEB_TIMEOUT_SECONDS - 0.3


def test_web_answer_skips_second_attempt_when_time_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """R4: under 5 s remaining after the first attempt → claude never runs."""
    from gateway import quick

    def slow_agy(q):
        time.sleep(0.3)
        return None, None

    monkeypatch.setattr(quick, "_agy_answer", slow_agy)
    monkeypatch.setattr(quick, "WEB_TIMEOUT_SECONDS", 4)
    calls = _stub_cli(monkeypatch, claude_stdout="Santiago")

    result = asyncio.run(quick._web_answer("what is the capital of Chile?", "agy"))
    assert result == (None, None)
    assert calls == []
