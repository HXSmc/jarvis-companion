#!/bin/bash
# Deploy path (settled 2026-10-16, see docs/HANDOFF.md "Box deploy path"):
# Mac main is the source of truth. The box's ~/jarvis-gateway clone never
# fetches on its own — this script PUSHES Mac main onto the box's box-deploy
# branch, which is checked out with `receive.denyCurrentBranch=updateInstead`
# (set once on the box) so the push also updates the worktree files. Never
# force-push: a non-fast-forward means someone edited on the box directly,
# which should not happen outside this script.
#
# Scope: only ~/jarvis-gateway on the box. Never touches ~/minipc-router,
# router.db, minipc-router.service, ~/work/*, or the jarvis-companion
# worktrees (central-ui, stream, rustdesk) — those are owned by other
# sessions per .orchestrator/reports/gateway-move-recon.md section 6.
set -euo pipefail

BOX_HOST="<box-host>"
BOX_DIR="/home/<user>/jarvis-gateway"
UNIT="jarvis-gateway.service"

echo "[1/4] push Mac main -> box:box-deploy (fast-forward only)"
git push box main:box-deploy

echo "[2/4] box: confirm clean tree, run pytest"
ssh "$BOX_HOST" "cd $BOX_DIR && git status --short && git log --oneline -1 && .venv/bin/python -m pytest -q"

echo "[3/4] box: restart $UNIT"
ssh "$BOX_HOST" "sudo -n systemctl restart $UNIT"

echo "[4/4] box: confirm active"
ssh "$BOX_HOST" "systemctl is-active $UNIT && sudo -n systemctl status $UNIT --no-pager | head -8"

echo "done."
