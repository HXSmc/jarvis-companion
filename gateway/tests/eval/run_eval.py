"""Answer-quality eval for the overlay: runs each question through overlay.run_turn (the typed-turn path).

  python gateway/tests/eval/run_eval.py [--out result.json] [--compare baseline.json] [--no-model] [--strict]

Intent picks are graded for every question. The model answer is run only when the turn lands on a
safe intent (answer/stop/clear/card_remove/clock); any other intent is cut right after the pick so
nothing opens, starts or searches. The model part needs the `claude` CLI logged in (opt-in, not for CI).
An intent passes when the `intent` row's kind OR the `intent_pick` row's pick is expected (jfollow10e).
Exit 0 always, except --compare: 1 if a question that passed in the baseline now fails; --strict: 1 on
any miss of a row without "model_only": true.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from gateway import overlay  # noqa: E402
from mac.voice import acks, lean  # noqa: E402

try:  # absent at commits before jfollow10e: the runner is copied there for the latency baseline (criterion 13)
    from mac.voice import intent_pick  # noqa: E402
except ImportError:
    intent_pick = None

SAFE = {"answer", "about_companion", "stop", "clear", "card_remove", "clock", "card_help"}


class _Cut(Exception):
    pass


class _NoAgent:  # the tool-using agent tier never runs in the eval
    def get(self, conv_id):
        raise _Cut

    def drop(self, conv_id):
        pass


def _deps(model: bool) -> overlay.OverlayDeps:
    cache = acks.AckCache(None)
    cache.prerender()
    if model:
        mk = lambda prompt, tools: lean.Conversations(lambda: lean.LeanSession(  # noqa: E731
            model="sonnet", system_prompt=prompt, tools=tools))
        answer = overlay.answer_prompt() if hasattr(overlay, "answer_prompt") else overlay.ANSWER_PROMPT
        quiet_p = overlay.quiet_prompt() if hasattr(overlay, "quiet_prompt") else overlay.QUIET_PROMPT
        conv, quiet = mk(answer, "WebSearch,WebFetch"), mk(quiet_p, "")
    else:
        conv = quiet = _NoAgent()
    picker = None
    if model and intent_pick is not None:
        picker = intent_pick.Picker()
        for _ in range(600):  # wait up to 60 s for the prewarm, as a running gateway has long before the first turn
            if picker._session is not None:
                break
            time.sleep(0.1)
    extra = {"intent_pick": picker.ask if picker else None} if intent_pick is not None else {}
    return overlay.OverlayDeps(acks=cache, synth=None, conversations=conv, quiet_conversations=quiet,
                               vlm_ask=lambda p, j: '{"id": null}', run=lambda *a, **k: None,
                               apps={}, agent_conversations=_NoAgent(), **extra)


async def _once(q: dict, n: int, deps, model: bool) -> dict:
    kinds: list[str] = []
    picks: list[dict] = []
    said: list[str] = []
    metrics: dict = {}
    cut = False

    def on_step(step, **f):
        nonlocal cut
        if step == "intent_pick":
            picks.append(dict(kv.split("=", 1) for kv in f.get("detail", "").split()))
        if step == "intent":
            kinds.append(f.get("kind", ""))
            if f.get("kind") not in SAFE:
                cut = True
                raise _Cut

    body = overlay.TurnBody(text=q["text"], conv_id=f"eval-{q['id']}-{n}")
    try:
        async with asyncio.timeout(90):
            async for ev in overlay.run_turn(body, deps, on_step=on_step):
                if ev.get("type") == "say":
                    said.append(ev["text"])
                elif ev.get("type") == "done":
                    metrics = ev.get("metrics") or {}
    except _Cut:
        pass
    except Exception as e:  # noqa: BLE001 - a failed turn is a result, not a crash
        said.append(f"[error {type(e).__name__}: {str(e)[:120]}]")
    answer = " ".join(said)
    want = q["expect_intent"] if isinstance(q["expect_intent"], list) else [q["expect_intent"]]
    why = []
    got = kinds + [p.get("pick", "") for p in picks]
    if not any(k in want for k in got):
        why.append(f"intent {got or 'none'} not in {want}")
    # Answer checks need an answer: intents cut after the pick, or a no-model run, are NOT RUN.
    ran = model and not cut and bool(answer) and "[error" not in answer
    need = bool(q["expect_answer_contains"] or q["expect_answer_not_contains"])
    low = answer.lower()
    if need and ran:
        for alt in q["expect_answer_contains"]:
            if not any(a.lower() in low for a in alt.split("|")):
                why.append(f"answer lacks '{alt}'")
        for bad in q["expect_answer_not_contains"]:
            if bad.lower() in low:
                why.append(f"answer has '{bad}'")
    elif need and not cut and "[error" in answer:
        why.append("answer part errored: " + answer[-140:])
    pk = picks[-1] if picks else {}
    return {"id": q["id"], "pass": not why, "intent_got": kinds,
            "pick": pk.get("pick"), "pick_path": pk.get("path"),
            "pick_ms": int(pk["ms"]) if "ms" in pk else None,
            "ack_ms": metrics.get("ack_ms"), "total_s": metrics.get("total_s"),
            "answer_got": answer[:300], "answer_run": ran if need else None,
            "why": "; ".join(why) or ("ok" if (ran or not need) else "ok (answer NOT RUN)")}


async def main_async(args) -> list[dict]:
    qs = json.loads((Path(__file__).parent / "jarvis_eval.json").read_text())
    deps = _deps(not args.no_model)
    overlay.set_deps(deps)
    out = []
    for q in qs:
        r = await _once(q, 1, deps, not args.no_model)
        if not r["pass"]:  # one rerun before a miss counts
            r2 = await _once(q, 2, deps, not args.no_model)
            if r2["pass"]:
                r2["why"] = "passed on rerun"
            r = r2
        print(("PASS " if r["pass"] else "MISS ") + q["id"], "-", r["why"], file=sys.stderr)
        out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--compare")
    ap.add_argument("--no-model", action="store_true", help="intent picks only; answers NOT RUN")
    ap.add_argument("--strict", action="store_true", help="exit 1 on a miss of a row without model_only")
    args = ap.parse_args()
    res = asyncio.run(main_async(args))
    print(f"SCORE {sum(r['pass'] for r in res)} of {len(res)}", file=sys.stderr)
    text = json.dumps(res, indent=1)
    if args.out:
        Path(args.out).write_text(text)
    else:
        print(text)
    if args.strict:
        flags = {q["id"]: q.get("model_only", False)
                 for q in json.loads((Path(__file__).parent / "jarvis_eval.json").read_text())}
        missed = [r["id"] for r in res if not r["pass"] and not flags.get(r["id"])]
        if missed:
            print("STRICT MISS:", ", ".join(missed), file=sys.stderr)
            return 1
    if args.compare:
        base = {r["id"]: r["pass"] for r in json.loads(Path(args.compare).read_text())}
        broke = [r["id"] for r in res if base.get(r["id"]) and not r["pass"]]
        if broke:
            print("REGRESSION:", ", ".join(broke), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
