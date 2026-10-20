#!/usr/bin/env python3
"""mkspec.py <story-id> <flash|5.3> <minutes>  -> writes .orchestrator/specs/<tid>.md from prd.json, prints tid"""
import json, sys, time, random, pathlib, re, os
sid, tier, mins = sys.argv[1], sys.argv[2], int(sys.argv[3])
root = pathlib.Path(os.environ.get('JARVIS_REPO_DIR')
                    or sys.exit('JARVIS_REPO_DIR is not set (the repo root)'))
prd = json.load(open(root/'.orchestrator/state/prd.json'))
st = next(s for s in prd['stories'] if s['id'] == sid)
tid = f"t{int(time.time())}{random.randint(0,32767)}"
persona = pathlib.Path.home()/'.claude/orchestrator/agents/spokes'/('glm-mechanic.md' if tier == 'flash' else 'glm-implementer.md')
acc = st['acceptance']
m = re.search(r'Owns:\s*(.*?)(?:\.\s|$)', acc[0])
owns = [re.sub(r'\s*\(.*?\)', '', p).strip() for p in (m.group(1) if m else '').split(',') if p.strip() and 'nothing' not in p]
crit = '\n'.join(f'{i+1}. {a}' for i, a in enumerate(acc))
body = f"""{persona.read_text()}

# Task {tid}: {st['title']} (story {sid})
## Status: not_started
## Context
Repo: jarvis-companion (FastAPI gateway + Mac ctypes drivers + Windows WS agent + iOS WKWebView shell). READ FIRST, it is the rationale and names every existing helper to reuse: docs/superpowers/specs/2026-10-04-remote-screen-control-design.md (Components + Platform constraints + Security sections). Story source: `.orchestrator/state/prd.json` story {sid}. Read docs/HANDOFF.md for the repo conventions. Read every existing file the design doc names for your story BEFORE writing (reuse their functions; do not duplicate ctypes loading or auth helpers).
If your cwd is a worktree (path under ~/.claude/orchestrator/worktrees/), the venv lives in the main checkout: run every python/pytest command as `PYTHONPATH=$PWD {root}/.venv/bin/python -m ...` from the worktree root. `.orchestrator/` is shared with the main checkout.
## Requirements — acceptance criteria (each independently checkable)
{crit}
## Constraints
- Only touch files listed under Owns. No new dependencies beyond those the story names. No new abstractions (no base classes, factories, config layers); shortest working diff; a `ponytail:` comment where you cut a known corner.
- Never git commit/push. Never read the Secrets vault or print credentials/tokens into code, logs, or your report (tests use fixture tokens from tests/conftest.py). Do not touch `.orchestrator/` except your report.
- Security must stay as designed: cookie HttpOnly/Secure/SameSite=Strict/Path=/screen; /screen/* never accepts the shared token; input rate limit; no typed-text logging; loopback bind unchanged.
- Fail loudly: on quota/429/limit errors or the same error twice, stop and write the report with the exact error text.
## Owns:
{chr(10).join(owns)}
## Budget: {mins}
## When done (MANDATORY)
1. Run EVERY runnable check named in the acceptance criteria and paste real output (full pytest summary line; exact status codes). Also run `PYTHONPATH=$PWD {root}/.venv/bin/python -m pytest tests windows/tests -q` and paste the summary line.
2. Write `.orchestrator/reports/{tid}.md` with `## Status: done|failed`, files changed (git status --short), commands run with real output snippets, per-criterion ✅/❌ checklist, blockers. Final message = one line.
"""
(root/'.orchestrator/specs'/f'{tid}.md').write_text(body)
print(tid)
