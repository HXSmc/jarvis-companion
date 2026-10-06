"""Quick-ask endpoint: POST /ask — a question in, a terse single answer out.

Routed vault → web → brain (autopilot-0 spec §Q1): one Jev classifier call on
OpenRouter's Decisions API picks the route (keyword heuristic over the vault
when OpenRouter is unreachable/unconfigured), the vault path answers from
Obsidian snippets via one generative call (NOVAULT sentinel → web), the web
path uses OpenRouter's `openrouter:web_search` server tool, and every failure
lands on the bounded `claude -p` brain. `brain: "skip"` never calls the brain
— the jarvis-phone hard requirement. Secrets are never read by this module:
config goes through common.config, and the vault walk prunes `Secrets/`.
"""

from __future__ import annotations

import asyncio
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from os import walk
from os.path import expanduser
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from common.config import DEFAULT_VAULT_PATH, read_section
from gateway.auth import require_bearer_any
from mac.voice import brain as _brain

router = APIRouter()

# Dedicated pool (siri.py pattern): a timed-out thread never blocks the loop's
# default-executor shutdown or starves brain slots.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jarvis-quick")

BRAIN_TIMEOUT_SECONDS = 15
ROUTER_TIMEOUT_SECONDS = 5.0
ANSWER_TIMEOUT_SECONDS = 20.0

DEFAULT_QUICK_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_ROUTER_MODEL = "typesafe/jev-1.13"

_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

TERSE_INSTRUCTION = "Answer with exactly the answer — one short sentence or phrase, no preamble."
NOVAULT = "NOVAULT"
_TIMEOUT_REPLY = "Jarvis is taking too long, try again."

_VAULT_ROOT = Path("~/Desktop/ObsidianVault").expanduser()
_EXCLUDED_DIRS = frozenset({"Secrets", ".obsidian", ".trash"})
_SNIPPET_BUDGET = 2500
_MAX_NOTES = 6
_SNIPPET_CONTEXT_LINES = 4

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "of", "in", "on", "to", "for",
    "and", "or", "what", "who", "when", "where", "how", "why", "does", "do",
    "did", "my", "me", "i", "it", "this", "that", "with", "from", "at", "by",
    "be", "can", "you", "your",
})


@dataclass
class QuickAnswer:
    """The pinned /ask reply shape (W1 and jarvis-phone build on it)."""

    answer: str | None
    route: str  # "vault" | "web" | "brain"
    ms: int
    source: str | None

    def as_dict(self) -> dict:
        return {"answer": self.answer, "route": self.route, "ms": self.ms, "source": self.source}


def _settings() -> dict[str, str]:
    """Vault section keys via common.config — the only Secrets access here."""
    return read_section(expanduser(DEFAULT_VAULT_PATH)) or {}


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _tokens(q: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", q.lower()) if len(t) > 1 and t not in _STOPWORDS]


def retrieve(q: str, vault_root: Path | None = None) -> tuple[list[str], int]:
    """Pure-Python keyword walk of the vault (rg is not installed on this Mac).

    Returns (snippets, total_keyword_hits): notes ranked by keyword hit count,
    top ≤6 with ±4 lines around each note's best-matching line, ~2,500 chars
    total. `Secrets/`, `.obsidian/`, `.trash/` are pruned at any depth.
    """
    tokens = _tokens(q)
    root = vault_root if vault_root is not None else _VAULT_ROOT
    scored: list[tuple[int, str]] = []
    total_hits = 0
    if not tokens:
        return [], 0
    for dirpath, dirnames, filenames in walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for name in filenames:
            if not name.endswith(".md"):
                continue
            path = Path(dirpath) / name
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # Filename counts toward the hit total (spec heuristic: filenames+contents).
            haystack = (name + "\n" + text).lower()
            hits = sum(haystack.count(t) for t in tokens)
            if hits <= 0:
                continue
            total_hits += hits
            lines = text.splitlines()
            best_i, best_count = 0, -1
            for i, line in enumerate(lines):
                count = sum(line.lower().count(t) for t in tokens)
                if count > best_count:
                    best_i, best_count = i, count
            snippet = "\n".join(
                lines[max(0, best_i - _SNIPPET_CONTEXT_LINES): best_i + _SNIPPET_CONTEXT_LINES + 1]
            )
            scored.append((hits, snippet))
    scored.sort(key=lambda item: -item[0])
    snippets: list[str] = []
    budget = _SNIPPET_BUDGET
    for _hits, snippet in scored[:_MAX_NOTES]:
        if budget <= 0:
            break
        part = snippet[:budget]
        snippets.append(part)
        budget -= len(part)
    return snippets, total_hits


def _openrouter(url: str, payload: dict, *, key: str, timeout_s: float) -> dict:
    """One blocking OpenRouter POST. The seam tests monkeypatch."""
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
        resp.raise_for_status()
        return resp.json()


def route_question(q: str, vault_hits: int) -> str:
    """"vault" | "web" | "brain" — one Jev choice call, heuristic fallback.

    Jev is a classifier (no text generation), called via the Decisions API.
    Any failure/timeout/non-conforming reply → keyword heuristic: hits ≥1 →
    vault, else web.
    """
    settings = _settings()
    key = (settings.get("openrouter_key") or "").strip()
    if key:
        payload = {
            "model": (settings.get("router_model") or "").strip() or DEFAULT_ROUTER_MODEL,
            "state": {"question": q},
            "questions": {
                "route": {
                    "type": "choice",
                    "instructions": "Where should this question be answered?",
                    "criteria": {
                        "vault": "Personal or private facts the user's own notes would contain: "
                                 "their projects, keys, settings, preferences, plans, names.",
                        "web": "Current or public facts that need a web search.",
                        "brain": "Open-ended or complex tasks needing a full assistant.",
                    },
                }
            },
        }
        try:
            data = _openrouter(_DECISIONS_URL, payload, key=key, timeout_s=ROUTER_TIMEOUT_SECONDS)
            choice = data["answers"]["route"].get("choice")
            if choice in ("vault", "web", "brain"):
                return str(choice)
        except Exception:
            pass  # heuristic below — a dead OpenRouter never breaks routing
    return "vault" if vault_hits >= 1 else "web"


def _content_text(data: dict) -> str:
    """Assistant text from a chat-completions reply ('' when malformed)."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):  # Responses-style content blocks
        return "".join(b.get("text", "") for b in content if isinstance(b, dict)).strip()
    return ""


_URL_RE = re.compile(r"https?://\S+")


def _first_url(data: dict) -> str | None:
    """First cited URL: annotations first, else the first URL in the content."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    for annotation in message.get("annotations") or []:
        if isinstance(annotation, dict) and annotation.get("url"):
            return str(annotation["url"])
    match = _URL_RE.search(_content_text(data))
    if match:
        return match.group(0).rstrip(".,)")
    return None


def _vault_answer(q: str, snippets: list[str], model: str, key: str) -> str | None:
    """Terse answer from vault snippets; None on NOVAULT (→ web path)."""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    f"{TERSE_INSTRUCTION} If the notes do not contain the answer, "
                    f"reply with exactly {NOVAULT}."
                ),
            },
            {
                "role": "user",
                "content": f"Question: {q}\n\nNotes:\n" + "\n---\n".join(snippets),
            },
        ],
    }
    data = _openrouter(_CHAT_URL, payload, key=key, timeout_s=ANSWER_TIMEOUT_SECONDS)
    text = _content_text(data)
    if not text or text == NOVAULT:
        return None
    return text


def _web_answer(q: str, model: str, key: str) -> tuple[str, str | None]:
    """One call with the `openrouter:web_search` server tool → (answer, source)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": TERSE_INSTRUCTION},
            {"role": "user", "content": q},
        ],
        "tools": [{"type": "openrouter:web_search", "parameters": {"max_results": 3}}],
    }
    data = _openrouter(_CHAT_URL, payload, key=key, timeout_s=ANSWER_TIMEOUT_SECONDS)
    return _content_text(data), _first_url(data)


async def _offload(fn, *args):
    """Run a blocking helper (vault walk / OpenRouter POST) off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(_executor, partial(fn, *args))


async def _brain_answer(q: str, skip: bool, start: float) -> QuickAnswer:
    """Bounded brain fallback (siri.py executor pattern). Skip → no brain call."""
    if skip:
        return QuickAnswer(None, "brain", _ms(start), None)
    try:
        text = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _executor, _brain.reply, f"{TERSE_INSTRUCTION} {q}", BRAIN_TIMEOUT_SECONDS
            ),
            timeout=BRAIN_TIMEOUT_SECONDS,
        )
        return QuickAnswer(str(text), "brain", _ms(start), None)
    except Exception:
        # Timeout or any brain failure: the caller gets an answer, never a 500.
        return QuickAnswer(_TIMEOUT_REPLY, "brain", _ms(start), None)


async def ask(q: str, brain: str = "auto") -> QuickAnswer:
    """Route one question vault → web → brain and answer it tersely."""
    start = time.monotonic()
    skip = brain == "skip"
    snippets, hits = await _offload(retrieve, q)
    route = await _offload(route_question, q, hits)
    settings = _settings()
    key = (settings.get("openrouter_key") or "").strip()
    model = (settings.get("quick_model") or "").strip() or DEFAULT_QUICK_MODEL
    if key and route in ("vault", "web"):
        try:
            if route == "vault":
                answer = await _offload(_vault_answer, q, snippets, model, key)
                if answer is not None:
                    return QuickAnswer(answer, "vault", _ms(start), None)
                # NOVAULT — the notes lack the answer; fall through to web.
            answer, source = await _offload(_web_answer, q, model, key)
            if answer:
                return QuickAnswer(answer, "web", _ms(start), source)
        except Exception:
            pass  # any OpenRouter failure on vault/web → brain fallback
    return await _brain_answer(q, skip, start)


@router.post("/ask", dependencies=[Depends(require_bearer_any("token", "ios_token"))])
async def ask_endpoint(request: Request) -> dict:
    """`{"q": "...", "brain": "auto"|"skip"}` → QuickAnswer.as_dict().

    The body is read off the Request (not a Pydantic param) so POSTs without
    a Content-Type header work, same as /siri/hey.
    """
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail='expected JSON body {"q": "..."}')
    if not isinstance(body, dict):
        body = {}
    q = str(body.get("q") or "")
    brain_flag = str(body.get("brain") or "auto")
    if not q.strip():
        raise HTTPException(status_code=400, detail='expected JSON body {"q": "..."}')
    if brain_flag not in ("auto", "skip"):
        brain_flag = "auto"
    return (await ask(q, brain_flag)).as_dict()
