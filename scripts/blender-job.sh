#!/usr/bin/env bash
# blender-job.sh <jobdir> — run Blender headless on <jobdir>/object.py inside a root-made systemd
# sandbox (jfollow10d). The only way the blender-object skill may run Blender.
#
# <jobdir> must be $JARVIS_BLENDER_JOBS/<slug-ish name> (default $HOME/.local/share/jarvis-blender/jobs),
# a real dir owned by you, holding object.py (<= 256 KB) and no old outputs. Nothing from the job
# reaches sudo or systemd-run except that checked path. The sandbox: no network, an empty $HOME
# except the job dir (read-write) and the Blender + runner dirs (read-only), read-only system, private
# /tmp, no new privileges, no capabilities, 4 GB, 64 tasks, 120 s.
# Prints one JSON line: {"ok":true,"glb_bytes":N,"png_bytes":N,"ms":N} (exit 0) or
# {"ok":false,"reason":"<code>"} (exit 2 = refused before Blender ran, 1 = Blender or output failed).
set -u

refuse() { printf '{"ok":false,"reason":"%s"}\n' "$1"; exit 2; }
failed() { printf '{"ok":false,"reason":"%s"}\n' "$1"; exit 1; }

[ "$#" -eq 1 ] || refuse bad_args
JOB=$1
[ "$(id -u)" != "0" ] || refuse root

BLENDER_DIR=${JARVIS_BLENDER_DIR:-$HOME/.local/opt/blender-5.2.3}
JOBS_ROOT=${JARVIS_BLENDER_JOBS:-$HOME/.local/share/jarvis-blender/jobs}
RUNNER_DIR=$(dirname "$(realpath "$0")")
HOME_REAL=$(realpath -e -- "$HOME" 2>/dev/null) || refuse bad_home
BD=$(realpath -e -- "$BLENDER_DIR" 2>/dev/null) || refuse blender_dir
JR=$(realpath -e -- "$JOBS_ROOT" 2>/dev/null) || refuse jobs_root
case "$BD" in "$HOME_REAL"/?*) ;; *) refuse blender_dir ;; esac
case "$JR" in "$HOME_REAL"/?*) ;; *) refuse jobs_root ;; esac
case "$BD/" in "$JR"/*) refuse nested_dirs ;; esac
case "$JR/" in "$BD"/*) refuse nested_dirs ;; esac
[ -f "$BD/blender" ] && [ ! -L "$BD/blender" ] && [ -x "$BD/blender" ] || refuse no_blender

BASE=${JOB##*/}
[[ "$BASE" =~ ^[a-z0-9][a-z0-9-]{0,47}$ ]] || refuse bad_jobdir
[ "$JOB" = "$JOBS_ROOT/$BASE" ] || refuse bad_jobdir
[ ! -L "$JOB" ] || refuse symlink
[ -d "$JOB" ] || refuse no_jobdir
[ "$(realpath -e -- "$JOB" 2>/dev/null)" = "$JOB" ] || refuse bad_jobdir
[ "$(stat -c %u -- "$JOB")" = "$(id -u)" ] || refuse not_owner
SCRIPT="$JOB/object.py"
[ ! -L "$SCRIPT" ] || refuse symlink
[ -f "$SCRIPT" ] || refuse no_script
[ "$(stat -c %s -- "$SCRIPT")" -le 262144 ] || refuse script_too_big
for f in object.glb preview.png; do
  { [ -e "$JOB/$f" ] || [ -L "$JOB/$f" ]; } && refuse stale_output
done

USER_NAME=$(id -un)
START=$(date +%s%3N)
OUT=$(sudo -n systemd-run --uid="$USER_NAME" --gid="$USER_NAME" --wait --pipe --collect \
  -p PrivateNetwork=yes -p ProtectHome=tmpfs -p ProtectSystem=strict \
  -p BindPaths="$JOB" -p BindReadOnlyPaths="$BD" -p BindReadOnlyPaths="$RUNNER_DIR" \
  -p PrivateTmp=yes -p NoNewPrivileges=yes -p MemoryMax=4G -p TasksMax=64 -p RuntimeMaxSec=120 \
  -p CapabilityBoundingSet= -p RestrictAddressFamilies=AF_UNIX \
  "$BD/blender" -b --factory-startup --python-exit-code 1 --python "$RUNNER_DIR/blender_job_runner.py" -- "$JOB" \
  </dev/null 2>&1)
RC=$?
MS=$(( $(date +%s%3N) - START ))

# The sandbox has ended. The job could have left a link named blender.log: remove it, never write through it.
LOG="$JOB/blender.log"
rm -f -- "$LOG"
( set -C; printf '%s\n' "$OUT" | tail -c 65536 > "$LOG" ) 2>/dev/null

if [ "$RC" -ne 0 ]; then
  case "$OUT" in
    sudo:*|*$'\n'sudo:*) failed sudo_failed ;;
    *"Finished with result: timeout"*) failed timeout ;;
    *"Finished with result: oom-kill"*) failed oom ;;
    *) failed blender_error ;;
  esac
fi

# Outputs: real files of ours with one link each, under the caps, with the right magic bytes.
check() {  # check <file> <missing-reason> <cap> <too-big-reason>
  local f=$1
  [ ! -L "$f" ] || failed symlink
  [ -f "$f" ] || failed "$2"
  [ "$(stat -c %h -- "$f")" = "1" ] || failed symlink
  [ "$(stat -c %u -- "$f")" = "$(id -u)" ] || failed not_owner
  [ "$(stat -c %s -- "$f")" -le "$3" ] || failed "$4"
}
GLB="$JOB/object.glb"
PNG="$JOB/preview.png"
check "$GLB" no_glb 52428800 glb_too_big
[ "$(head -c 4 -- "$GLB")" = "glTF" ] || failed bad_glb
[ "$(od -An -tu4 -j4 -N4 -- "$GLB" | tr -d ' ')" = "2" ] || failed bad_glb
check "$PNG" no_png 5242880 png_too_big
[ "$(od -An -tx1 -N8 -- "$PNG" | tr -d ' \n')" = "89504e470d0a1a0a" ] || failed bad_png

printf '{"ok":true,"glb_bytes":%s,"png_bytes":%s,"ms":%s}\n' "$(stat -c %s -- "$GLB")" "$(stat -c %s -- "$PNG")" "$MS"
