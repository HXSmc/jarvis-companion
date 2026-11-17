#!/usr/bin/env bash
# install-blender-object.sh — put the blender-object skill and its sandbox wrapper on the mini PC
# (jfollow10d). Run as your own user; no sudo, no sudoers or ~/.local/bin change.
#
#   install --vault <dir> [--dry-run]   <dir>: absolute path of the Obsidian vault (holds .obsidian/)
#   uninstall [--dry-run]               removes exactly the files below (and their dirs when empty)
#
# Installs:
#   $HOME/.claude/skills/blender-object/SKILL.md          (0644, the vault path written in)
#   $HOME/.local/share/jarvis-blender/bin/blender-job.sh  (0755)
#   $HOME/.local/share/jarvis-blender/bin/blender_job_runner.py (0644)
#   $HOME/.local/share/jarvis-blender/jobs/               (0700)
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
SKILL_DIR="$HOME/.claude/skills/blender-object"
SKILL="$SKILL_DIR/SKILL.md"
ROOT="$HOME/.local/share/jarvis-blender"
BIN="$ROOT/bin"
JOBS="$ROOT/jobs"

usage() { echo "usage: $0 install --vault <dir> [--dry-run] | uninstall [--dry-run]" >&2; exit 2; }

ACTION=${1:-}
[ -n "$ACTION" ] || usage
shift
VAULT=""
DRY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --vault) [ "$#" -ge 2 ] || usage; VAULT=$2; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) usage ;;
  esac
done

run() { if [ "$DRY" = 1 ]; then echo "would: $*"; else "$@"; fi; }

case "$ACTION" in
  install)
    case "$VAULT" in /*) ;; *) echo "--vault must be an absolute path" >&2; exit 1 ;; esac
    case "$VAULT" in *$'\n'*|*'`'*|*'$'*) echo "--vault has characters a skill file can't hold" >&2; exit 1 ;; esac
    [ -d "$VAULT/.obsidian" ] || { echo "--vault is not an Obsidian vault (no .obsidian/)" >&2; exit 1; }
    VAULT=$(realpath -e -- "$VAULT")
    text=$(<"$REPO/skills/blender-object/SKILL.md")
    run mkdir -p "$SKILL_DIR" "$BIN" "$JOBS"
    if [ "$DRY" = 1 ]; then
      echo "would: write $SKILL (0644, vault path filled in)"
    else
      printf '%s\n' "${text//__JARVIS_VAULT__/"$VAULT"}" > "$SKILL"  # quoted: a & in the path stays a &
      chmod 0644 "$SKILL"
    fi
    run install -m 0755 "$REPO/scripts/blender-job.sh" "$BIN/blender-job.sh"
    run install -m 0644 "$REPO/scripts/blender_job_runner.py" "$BIN/blender_job_runner.py"
    run chmod 0700 "$JOBS"
    [ "$DRY" = 1 ] || echo "installed: $SKILL, $BIN/blender-job.sh, $BIN/blender_job_runner.py, $JOBS"
    ;;
  uninstall)
    [ -z "$VAULT" ] || usage
    run rm -f -- "$SKILL" "$BIN/blender-job.sh" "$BIN/blender_job_runner.py"
    for d in "$SKILL_DIR" "$BIN" "$JOBS" "$ROOT"; do
      if [ "$DRY" = 1 ]; then echo "would: rmdir $d (if empty)"; else rmdir -- "$d" 2>/dev/null || true; fi
    done
    [ "$DRY" = 1 ] || echo "removed the blender-object skill and its wrapper"
    ;;
  *) usage ;;
esac
