#!/usr/bin/env python3
"""mkreview.py <story-id> <build-tid> -> review+verify spec for a fresh glm-5.3 spoke run IN PLACE after spoke-wt apply; prints tid"""
import json, sys, time, random, pathlib, re
sid, btid = sys.argv[1], sys.argv[2]
root = pathlib.Path('/Users/<user>/Desktop/personal/jarvis-companion')
prd = json.load(open(root/'.orchestrator/state/prd.json'))
st = next(s for s in prd['stories'] if s['id'] == sid)
tid = f"t{int(time.time())}{random.randint(0,32767)}-review"
persona = (pathlib.Path.home()/'.claude/orchestrator/agents/spokes/glm-implementer.md').read_text()
acc = st['acceptance']
m = re.search(r'Owns:\s*(.*?)(?:\.\s|$)', acc[0])
owns = [re.sub(r'\s*\(.*?\)', '', p).strip() for p in (m.group(1) if m else '').split(',') if p.strip() and 'nothing' not in p]
crit = '\n'.join(f'{i+1}. {a}' for i, a in enumerate(acc))
body = f"""{persona}

# Task {tid}: REVIEW + VERIFY story {sid} ({st['title']}) built by spoke {btid}
## Status: not_started
## Context
You are a fresh reviewer with no memory of the build. Read: `.orchestrator/specs/{btid}.md` (the contract), `.orchestrator/reports/{btid}.md` (the builder's claims), docs/superpowers/specs/2026-10-04-remote-screen-control-design.md, and the ACTUAL diff: `git status --short` and `git diff HEAD -- {' '.join(owns)}` (new files: read them fully). The builder's ✅ is a claim, not evidence.
## Requirements
1. Re-run EVERY runnable check in the criteria below yourself with fresh output; paste it. Use `.venv/bin/python` from this checkout.
2. Read the whole diff. Hunt: criteria drift, silent scope creep, files touched outside Owns (`git status --short` vs the Owns list), secrets/tokens in code or reports, network binds other than 127.0.0.1, cookie flags missing, the shared token reaching /screen/*, typed text reaching logs, new dependencies, dead/duplicated code, the variadic CGEventCreateScrollWheelEvent, shell=True or `cmd /c start`.
3. Answer explicitly: "is there a meaningfully simpler approach?" (one paragraph).
4. Verdict line, exactly one of: `VERDICT: ACCEPT` / `VERDICT: FIX` (quote exact failing output + file:line for each finding) / `VERDICT: REJECT` (why).
## Acceptance criteria under review
{crit}
## Constraints
READ-ONLY: do not edit any product file. No git commit/push. Do not touch `.orchestrator/` except your report. Never print credentials.
## Owns:
(none — read-only)
## Budget: 15
## When done (MANDATORY)
Write `.orchestrator/reports/{tid}.md` with `## Status: done`, the fresh outputs, findings with file:line, the simpler-approach answer, and the VERDICT line. Final message = the verdict line.
"""
(root/'.orchestrator/specs'/f'{tid}.md').write_text(body)
print(tid)
