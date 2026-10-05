#!/bin/bash
# launch2.sh <tid> <model> <minutes> [inplace] -- runs a glm-code spoke, blocks until exit. GLM_MODEL is what glm-code reads.
cd /Users/<user>/Desktop/personal/jarvis-companion || exit 1
TID="$1"; MODEL="$2"; MINS="$3"; INPLACE="$4"
[ "$INPLACE" = "inplace" ] && export GLM_NO_WORKTREE=1
W=1; [ "$MODEL" = "glm-5.3" ] && W=2
echo "$(date +%s) $W" >> ~/.claude/orchestrator/state/quota.log
GLM_TASK_ID="$TID" GLM_MODEL="$MODEL" CLAUDE_CODE_MAX_CONTEXT_TOKENS=100000 timeout "${MINS}m" glm-code -p "$(cat .orchestrator/specs/$TID.md)" --permission-mode acceptEdits --dangerously-skip-permissions > ".orchestrator/logs/$TID.log" 2>&1
echo "spoke $TID exit=$? report=$([ -f .orchestrator/reports/$TID.md ] && echo yes || echo MISSING)"
