"""Quick-ask endpoint: POST /ask — a question in, a terse single answer out.

Routed vault → web → brain (autopilot-0 spec §Q1): one Jev classifier call on
OpenRouter's Decisions API picks the route (keyword heuristic over the vault
when OpenRouter is unreachable/unconfigured), the vault path answers from
Obsidian snippets via one generative call (NOVAULT sentinel → web), the web
path runs a LOCAL CLI — `agy` (Antigravity) first, `claude -p` with WebSearch
as fallback (UNKNOWN sentinel → brain; owner rule 2026-10-06: no separate
search API) — and every failure lands on the bounded `claude -p` brain. Empty,
sentinel, or question-echoing replies are treated as "no answer" so the model
can never confabulate. `brain: "skip"` never calls the brain
— the jarvis-phone hard requirement. Secrets are never read by this module:
config goes through common.config, and the vault walk prunes `Secrets/`.
"""

from __future__ import annotations

import asyncio
import difflib
import os
import re
import shutil
import subprocess
import tempfile
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
WEB_TIMEOUT_SECONDS = 40  # the phone app's own timeout raise is a separate iOS change; this cap must sit under it

DEFAULT_QUICK_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_ROUTER_MODEL = "typesafe/jev-1.13"

_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

TERSE_INSTRUCTION = "Answer with exactly the answer — one short sentence or phrase, no preamble."
NOVAULT = "NOVAULT"

# Imperative openers: a command for the local assistant, never a lookup — the
# brain gets it without spending a Jev call. Question words (what/who/…) never
# lead, so they can't trip this.
ACTION_VERBS = ("open", "close", "launch", "start", "stop", "run", "play", "pause",
                "search", "google", "send", "email", "text", "message", "call", "type",
                "click", "tap", "press", "scroll", "book", "buy", "order", "pay",
                "install", "download", "set", "turn", "wake", "shut", "restart",
                "reboot", "lock", "unlock", "show", "take")

_VAULT_ROOT = Path("~/Desktop/ObsidianVault").expanduser()
_EXCLUDED_DIRS = frozenset({"Secrets", ".obsidian", ".trash"})
_SNIPPET_BUDGET = 2500
_MAX_NOTES = 4
_SNIPPET_CONTEXT_LINES = 4

# Function words only — domain words (jarvis, port, <project>, …) never go here.
STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "of", "in", "on", "to", "for",
    "and", "or", "nor", "but", "what", "which", "who", "whom", "whose", "when",
    "where", "how", "why", "does", "did", "done", "doing", "have", "has", "had",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "about", "into", "onto", "over", "under", "than", "then", "them", "they",
    "there", "their", "this", "that", "these", "those", "with", "from", "your",
    "yours", "much", "many", "some", "such", "only", "also", "just", "very",
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


def _keywords(q: str) -> list[str]:
    """Content words of the question: ≥4 letters, function words dropped."""
    return [t for t in re.findall(r"[a-z0-9]+", q.lower()) if len(t) >= 4 and t not in STOPWORDS]


def _is_action(q: str) -> bool:
    """True when the question opens with an imperative verb (ACTION_VERBS)."""
    first = re.match(r"[a-z0-9]+", q.strip().lower())
    return bool(first and first.group(0) in ACTION_VERBS)


_QUESTION_WORDS = re.compile(r"\b(who|what|when|where|which|how|why|whose|whom)\b", re.IGNORECASE)


def _extract_question(text: str) -> str:
    """Text from the LAST question-word to the end — STT gives no punctuation, so a
    long multi-intent utterance is answered by its trailing question. No question
    word → the text unchanged."""
    matches = list(_QUESTION_WORDS.finditer(text))
    return text[matches[-1].start():].strip() if matches else text.strip()


# ponytail: the owner's active company — one constant, no config, no list
DEFAULT_PROJECT = "<project>"
_DEFAULT_PROJECT_RE = re.compile(r"\b(?:my|the|our) project\b", re.IGNORECASE)
_FUZZY_MIN_RATIO = 0.5


def _project_tokens() -> set[str]:
    """Lowercase ≥4-letter tokens from Projects/*.md basenames (the note-name vocab)."""
    tokens: set[str] = set()
    projects = _VAULT_ROOT / "Projects"
    for path in projects.glob("*.md"):
        for part in re.split(r"[^A-Za-z0-9]+", path.stem):
            if len(part) >= 4:
                tokens.add(part.lower())
    return tokens


def _resolve_project(q: str, tokens: set[str] | None = None) -> str:
    """'my/the/our project' → DEFAULT_PROJECT; STT-variant tokens → the closest
    project token (first-3-chars shared AND difflib ratio ≥ 0.5, so '<name>'/'tawid'
    → '<project>' but 'towed' never does)."""
    q = _DEFAULT_PROJECT_RE.sub(DEFAULT_PROJECT, q)
    if tokens is None:
        tokens = _project_tokens()
    for token in {t for t in re.findall(r"[a-z0-9]+", q.lower())
                  if len(t) >= 4 and t not in STOPWORDS and t not in tokens}:
        candidates = [p for p in tokens if p[:3] == token[:3]]
        if not candidates:
            continue
        best = max(candidates, key=lambda p: difflib.SequenceMatcher(None, token, p).ratio())
        if difflib.SequenceMatcher(None, token, best).ratio() >= _FUZZY_MIN_RATIO:
            q = re.sub(rf"\b{re.escape(token)}\b", best, q, flags=re.IGNORECASE)
    return q


def retrieve(q: str, vault_root: Path | None = None) -> tuple[list[str], tuple[int, int] | None]:
    """Pure-Python keyword walk of the vault (rg is not installed on this Mac).

    A note qualifies only when it contains ≥2 distinct keywords (≥1 when the
    question has a single keyword). Score = distinct keywords matched + 3 per
    keyword in the note's relative path/name + 1 per distinct keyword in the
    body; the top ≤4 notes become snippets (±4 lines around each note's
    best-matching line, ~2,500 chars total). Returns (snippets, best) where
    best is the top note's (distinct keywords matched, keywords in its
    path/name) — the heuristic router's input; None when nothing qualifies.
    `Secrets/`, `.obsidian/`, `.trash/` are pruned at any depth.
    """
    keywords = _keywords(q)
    root = vault_root if vault_root is not None else _VAULT_ROOT
    if not keywords:
        return [], None
    single = len(keywords) == 1
    scored: list[tuple[int, int, int, str]] = []  # (score, distinct, in_name, snippet)
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
            rel = str(path.relative_to(root)).lower()
            body = text.lower()
            in_name = {k for k in keywords if k in rel}
            in_body = {k for k in keywords if k in body}
            matched = in_name | in_body
            if len(matched) < 2 and not (single and matched):
                continue  # single-common-word matches are the false-positive source
            score = len(matched) + 3 * len(in_name) + len(in_body)
            lines = text.splitlines()
            best_i, best_count = 0, -1
            for i, line in enumerate(lines):
                count = sum(line.lower().count(k) for k in keywords)
                if count > best_count:
                    best_i, best_count = i, count
            snippet = "\n".join(
                lines[max(0, best_i - _SNIPPET_CONTEXT_LINES): best_i + _SNIPPET_CONTEXT_LINES + 1]
            )
            scored.append((score, len(matched), len(in_name), snippet))
    if not scored:
        return [], None
    scored.sort(key=lambda item: -item[0])
    snippets: list[str] = []
    budget = _SNIPPET_BUDGET
    for _score, _distinct, _in_name, snippet in scored[:_MAX_NOTES]:
        if budget <= 0:
            break
        part = snippet[:budget]
        snippets.append(part)
        budget -= len(part)
    _score, distinct, in_name, _snippet = scored[0]
    return snippets, (distinct, in_name)


def _openrouter(url: str, payload: dict, *, key: str, timeout_s: float) -> dict:
    """One blocking OpenRouter POST. The seam tests monkeypatch."""
    with httpx.Client(timeout=timeout_s) as client:
        resp = client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
        resp.raise_for_status()
        return resp.json()


def route_question(q: str, vault_hits: tuple[int, int] | None) -> str:
    """"vault" | "web" | "brain" — one Jev choice call, heuristic fallback.

    Jev is a classifier (no text generation), called via the Decisions API.
    Any failure/timeout/non-conforming reply → keyword heuristic: vault only
    when the best-matching note matched ≥2 distinct keywords with at least one
    in its path/name (`vault_hits` is retrieve()'s best tuple), else web.
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
    return "vault" if vault_hits and vault_hits[0] >= 2 and vault_hits[1] >= 1 else "web"


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


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return " ".join(_NON_ALNUM.split(text.lower()))


# No-answer markers: the brain's own failure strings (mac/voice/brain.py) plus
# the terse refusals a web/vault model emits instead of an answer — normalised
# the same way as the reply, so "can't" matches "can t".
_FAILURE_PATTERNS = tuple(
    _norm(p)
    for p in (
        "the brain is unavailable", "brain unavailable", "not logged in",
        "cannot fulfill", "can't fulfill", "i cannot", "i can't", "as an ai",
        "i'm unable", "i am unable",
    )
)


def _is_answer(q: str, text: str | None) -> bool:
    """False for empty/sentinel/refusal replies and echoes of the question."""
    if not text:
        return False
    norm = _NON_ALNUM.sub(" ", text.lower()).strip()
    if not norm or norm in ("unknown", "novault"):
        return False
    if any(p in norm for p in _FAILURE_PATTERNS):
        return False
    qn = _NON_ALNUM.sub(" ", q.lower()).strip()
    return not (qn and (norm == qn or qn in norm))


_URL_RE = re.compile(r"https?://\S+")


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
    if not _is_answer(q, text):  # empty, NOVAULT, or a question echo
        return None
    return text


def _web_prompt(q: str) -> str:
    """The pinned prompt both local web engines receive."""
    return (
        "Answer with exactly the answer and nothing else — no preamble, no "
        "markdown, one short sentence or phrase. Search the web if you need "
        "to. If you cannot find it, reply exactly UNKNOWN.\n\nQuestion: " + q
    )


def _parse_cli_stdout(stdout: str) -> tuple[str | None, str | None]:
    """(last non-empty line, quotes/backticks stripped, URL in it or None)."""
    for line in reversed(stdout.splitlines()):
        text = line.strip().strip('"`').strip()
        if text:
            match = _URL_RE.search(text)
            return text, match.group(0).rstrip(".,)") if match else None
    return None, None


def _agy_answer(q: str) -> tuple[str | None, str | None]:
    """Local `agy` (Antigravity) CLI with its own web search → (answer, source).

    Flag order is load-bearing: --dangerously-skip-permissions BEFORE
    --print; --print-timeout is never passed (the subprocess timeout below
    is the cap). AGY_KIND=quick tags the call for usage accounting.
    """
    try:
        proc = subprocess.run(
            ["agy", "--dangerously-skip-permissions", "--print", _web_prompt(q)],
            capture_output=True,
            text=True,
            timeout=WEB_TIMEOUT_SECONDS,
            env={**os.environ, "AGY_KIND": "quick"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if proc.returncode != 0:
        return None, None
    return _parse_cli_stdout(proc.stdout)


def _claude_web_answer(q: str) -> tuple[str | None, str | None]:
    """`claude -p` with WebSearch — brain.py's invocation style → (answer, source).

    Same slim config dir as the brain (~/.claude-jarvis-brain, owner-gated
    login), so until that one-time login this replies "Not logged in",
    which the sentinel check below treats as no answer.
    """
    argv = [
        "claude", "-p",
        "--tools", "WebSearch",
        "--mcp-config", '{"mcpServers":{}}',
        "--strict-mcp-config",
        "--setting-sources", "",
        "--append-system-prompt", TERSE_INSTRUCTION,
        "--max-turns", "3",
    ]
    env = {k: os.environ[k] for k in _brain._BRAIN_ENV_KEYS if k in os.environ}
    env["CLAUDE_CONFIG_DIR"] = str(_brain.BRAIN_CONFIG_DIR)
    scratch = tempfile.mkdtemp(prefix="jarvis-quick-web-")
    try:
        proc = subprocess.run(
            argv,
            input=_web_prompt(q),
            capture_output=True,
            text=True,
            cwd=scratch,
            env=env,
            timeout=WEB_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if proc.returncode != 0:
        return None, None
    return _parse_cli_stdout(proc.stdout)


async def _offload(fn, *args):
    """Run a blocking helper (vault walk / CLI / OpenRouter POST) off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(_executor, partial(fn, *args))


async def _web_answer(q: str, engine: str) -> tuple[str | None, str | None]:
    """Local-CLI web answer: agy first (unless engine=="claude"), claude fallback.

    Any no-answer (CLI missing, non-zero exit, timeout, empty stdout,
    UNKNOWN, refusal, question echo) falls through to the next engine and
    finally to None → the caller's brain fallback. Stderr and progress
    chatter never ship as an answer.
    """
    if engine != "claude":
        answer, source = await _offload(_agy_answer, q)
        if _is_answer(q, answer):
            return answer, source
    answer, source = await _offload(_claude_web_answer, q)
    if _is_answer(q, answer):
        return answer, source
    return None, None


async def _brain_answer(
    q: str, skip: bool, start: float, context: str | None = None
) -> QuickAnswer:
    """Bounded brain fallback (siri.py executor pattern). Skip → no brain call.

    The toolless brain can never know vault facts by itself, so vault snippets
    ride along as prompt context when available."""
    if skip:
        return QuickAnswer(None, "brain", _ms(start), None)
    prompt = f"{TERSE_INSTRUCTION} {q}"
    if context:
        prompt = (f"{TERSE_INSTRUCTION} Answer from the notes when they contain the answer.\n"
                  f"Notes:\n{context}\n\nQuestion: {q}")
    try:
        text = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _executor, _brain.reply, prompt, BRAIN_TIMEOUT_SECONDS
            ),
            timeout=BRAIN_TIMEOUT_SECONDS,
        )
        if _is_answer(q, str(text)):
            return QuickAnswer(str(text), "brain", _ms(start), None)
    except Exception:
        pass  # Timeout or any brain failure: the caller gets an answer, never a 500.
    return QuickAnswer(None, "brain", _ms(start), None)


async def ask(q: str, brain: str = "auto") -> QuickAnswer:
    """Route one question vault → web → brain and answer it tersely.

    Two Jev-saving overrides run before the classifier: an imperative opener
    (ACTION_VERBS) is a command for the brain, and a strong vault match (best
    note ≥2 distinct keywords with ≥1 in its path/name) goes straight to the
    vault — Jev misroutes those (a stranger's name for an owner-fact question).
    """
    start = time.monotonic()
    q = _extract_question(q)
    q = _resolve_project(q)
    skip = brain == "skip"
    snippets, best = await _offload(retrieve, q)
    if _is_action(q):
        return await _brain_answer(q, skip, start)
    if isinstance(best, tuple) and best[0] >= 2 and best[1] >= 1:
        route = "vault"  # strong note match — vault-first, no Jev call
    else:
        route = await _offload(route_question, q, best)
    settings = _settings()
    key = (settings.get("openrouter_key") or "").strip()
    model = (settings.get("quick_model") or "").strip() or DEFAULT_QUICK_MODEL
    web_engine = (settings.get("web_engine") or "").strip().lower() or "agy"
    if route == "vault" and key:
        try:
            answer = await _offload(_vault_answer, q, snippets, model, key)
            if answer is not None:
                return QuickAnswer(answer, "vault", _ms(start), None)
            # NOVAULT/echo — the notes lack the answer; fall through to web.
        except Exception:
            pass  # vault OpenRouter failure → web/brain below
    if route in ("vault", "web") and web_engine != "off":
        answer, source = await _web_answer(q, web_engine)
        if answer is not None:
            return QuickAnswer(answer, "web", _ms(start), source)
        # Web no-answer: brain, unless skip pins the reply to a null web answer.
        if skip:
            return QuickAnswer(None, "web", _ms(start), None)
    return await _brain_answer(
        q, skip, start, context="\n---\n".join(snippets) if snippets else None
    )


@router.post("/ask", dependencies=[Depends(require_bearer_any("ios_token"))])
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
