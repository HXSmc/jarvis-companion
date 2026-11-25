#!/usr/bin/env bash
# blender-job.sh <jobdir> — run Blender headless on <jobdir>/object.py inside a root-made systemd
# sandbox (jfollow10d). The only way the blender-object skill may run Blender.
#
# <jobdir> must be $JARVIS_BLENDER_JOBS/<slug-ish name> (default $HOME/.local/share/jarvis-blender/jobs),
# a real dir owned by you, holding object.py (<= 256 KB) and no old outputs. Nothing from the job
# reaches sudo or systemd-run except that checked path. The sandbox: no network, an empty $HOME
# except the job dir (read-write) and the Blender + runner dirs (read-only), read-only system, private
# /tmp, no new privileges, no capabilities, 4 GB, 64 tasks, 120 s. PrivateUsers=yes hides every other
# process of the same uid (no /proc/<pid>/root way into the real home); an empty read-only /run cuts the
# host's unix sockets (docker, tailscale, snapd, system D-Bus).
# An animated job also leaves frames/ (f_0001.png..f_NNNN.png). After the GLB and PNG checks pass,
# a second root-made sandbox unit (the same props, but the job dir read-only and one fresh writable
# mp4out dir in place of the Blender and runner binds, 60 s) runs the fixed /usr/bin/ffmpeg on them
# into object.mp4. The outputs are checked again after it. Frames never outlive the run, and an mp4 failure
# never fails the job.
# Prints one JSON line: {"ok":true,"glb_bytes":N,"png_bytes":N,"ms":N} (exit 0), with "mp4_bytes":N
# or "mp4":"<reason>" (bad_frames|no_ffmpeg|ffmpeg_failed|timeout|mp4_too_big|bad_mp4) appended when
# frames existed, or {"ok":false,"reason":"<code>"} (exit 2 = refused before Blender ran, 1 = failed).
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
for f in object.glb preview.png object.mp4 frames mp4out; do
  { [ -e "$JOB/$f" ] || [ -L "$JOB/$f" ]; } && refuse stale_output
done

# Scratch the job and the encoder leave: gone on every exit from here on (neither exists yet: the stale
# check above), so a kill mid-run or any failure never leaves them for the retry to trip on. A link or a
# file goes as itself (never through it); a dir is made writable and emptied without following links.
drop_dir() {
  if [ -L "$1" ] || { [ -e "$1" ] && [ ! -d "$1" ]; }; then
    rm -f -- "$1"
  elif [ -d "$1" ]; then
    find "$1" -type d -exec chmod u+rwx {} + 2>/dev/null
    find "$1" -mindepth 1 -delete 2>/dev/null
    rmdir -- "$1" 2>/dev/null
  fi
}
drop_frames() { drop_dir "$JOB/frames"; drop_dir "$JOB/mp4out"; }
trap drop_frames EXIT

USER_NAME=$(id -un)
START=$(date +%s%3N)
OUT=$(sudo -n systemd-run --uid="$USER_NAME" --gid="$USER_NAME" --wait --pipe --collect \
  -p PrivateNetwork=yes -p ProtectHome=tmpfs -p ProtectSystem=strict \
  -p BindPaths="$JOB" -p BindReadOnlyPaths="$BD" -p BindReadOnlyPaths="$RUNNER_DIR" \
  -p PrivateTmp=yes -p NoNewPrivileges=yes -p MemoryMax=4G -p TasksMax=64 -p RuntimeMaxSec=120 \
  -p CapabilityBoundingSet= -p RestrictAddressFamilies=AF_UNIX \
  -p PrivateUsers=yes -p TemporaryFileSystem=/run:ro \
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
check_outputs() {
  check "$GLB" no_glb 52428800 glb_too_big
  [ "$(head -c 4 -- "$GLB")" = "glTF" ] || failed bad_glb
  [ "$(od -An -tu4 -j4 -N4 -- "$GLB" | tr -d ' ')" = "2" ] || failed bad_glb
  check "$PNG" no_png 5242880 png_too_big
  [ "$(od -An -tx1 -N8 -- "$PNG" | tr -d ' \n')" = "89504e470d0a1a0a" ] || failed bad_png
}
check_outputs

# The mp4 step: only when the job left frames, and its failure never fails the job ("ok":true stays).
FFMPEG=/usr/bin/ffmpeg  # fixed: not looked up on PATH, no env override
MP4=""
MP4_BYTES=""
if [ -e "$JOB/frames" ] || [ -L "$JOB/frames" ]; then
  # frames must be a real dir of ours holding only f_0001.png..f_NNNN.png (2..240): the entry count
  # equals N and every checked name exists, so together nothing else can be in there.
  if [ -L "$JOB/frames" ] || [ ! -d "$JOB/frames" ] \
     || [ "$(stat -c %u -- "$JOB/frames")" != "$(id -u)" ]; then
    MP4="bad_frames"
  else
    N=$(( $(find "$JOB/frames" -mindepth 1 -maxdepth 1 -printf x 2>/dev/null | wc -c) ))
    if [ "$N" -lt 2 ] || [ "$N" -gt 240 ]; then
      MP4="bad_frames"
    fi
  fi
  if [ -z "$MP4" ]; then
    for i in $(seq 1 "$N"); do
      FR="$JOB/frames/$(printf 'f_%04d.png' "$i")"
      if [ -L "$FR" ] || [ ! -f "$FR" ] || [ "$(stat -c %h -- "$FR")" != "1" ] \
         || [ "$(stat -c %u -- "$FR")" != "$(id -u)" ] || [ "$(stat -c %s -- "$FR")" -gt 2097152 ] \
         || [ "$(od -An -tx1 -N8 -- "$FR" | tr -d ' \n')" != "89504e470d0a1a0a" ]; then
        MP4="bad_frames"
        break
      fi
    done
  fi
  if [ -z "$MP4" ] && { [ -e "$JOB/object.mp4" ] || [ -L "$JOB/object.mp4" ]; }; then
    MP4="bad_mp4"  # the job left an object.mp4 (a link ffmpeg would write through): never encode onto it
  elif [ -z "$MP4" ] && [ ! -x "$FFMPEG" ]; then
    MP4="no_ffmpeg"
  elif [ -z "$MP4" ] && ! mkdir -m 0700 -- "$JOB/mp4out" 2>/dev/null; then
    MP4="bad_mp4"
  elif [ -z "$MP4" ]; then
    # The encoder decodes job-written PNG bytes, so it sees the job dir read-only and writes only into
    # the fresh, empty mp4out; the mp4 is checked there, then moved into place.
    MP4OUT="$JOB/mp4out/object.mp4"
    OUT2=$(sudo -n systemd-run --uid="$USER_NAME" --gid="$USER_NAME" --wait --pipe --collect \
      -p PrivateNetwork=yes -p ProtectHome=tmpfs -p ProtectSystem=strict \
      -p BindReadOnlyPaths="$JOB" -p BindPaths="$JOB/mp4out" \
      -p PrivateTmp=yes -p NoNewPrivileges=yes -p MemoryMax=4G -p TasksMax=64 -p RuntimeMaxSec=60 \
      -p CapabilityBoundingSet= -p RestrictAddressFamilies=AF_UNIX \
      -p PrivateUsers=yes -p TemporaryFileSystem=/run:ro \
      "$FFMPEG" -nostdin -hide_banner -loglevel error -protocol_whitelist file \
      -f image2 -framerate 24 -start_number 1 -i "$JOB/frames/f_%04d.png" -frames:v "$N" \
      -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -movflags +faststart -an -n \
      "$MP4OUT" \
      </dev/null 2>&1)
    RC2=$?
    if [ "$RC2" -ne 0 ]; then
      case "$OUT2" in
        *"Finished with result: timeout"*) MP4="timeout" ;;
        *) MP4="ffmpeg_failed" ;;
      esac
    elif [ -L "$MP4OUT" ] || [ ! -f "$MP4OUT" ] \
         || [ "$(stat -c %h -- "$MP4OUT")" != "1" ] \
         || [ "$(stat -c %u -- "$MP4OUT")" != "$(id -u)" ]; then
      MP4="bad_mp4"
    elif [ "$(stat -c %s -- "$MP4OUT")" -gt 20971520 ]; then
      MP4="mp4_too_big"
    elif [ "$(od -An -tc -j4 -N4 -- "$MP4OUT" | tr -d ' \n')" != "ftyp" ]; then
      MP4="bad_mp4"
    elif mv -T -- "$MP4OUT" "$JOB/object.mp4"; then
      MP4_BYTES=$(stat -c %s -- "$JOB/object.mp4")
    else
      MP4="bad_mp4"
    fi
  fi
  if [ -n "$MP4" ]; then  # a failed encode that left a file: remove it (rm -f on a link removes the link)
    rm -f -- "$JOB/object.mp4"
  fi
fi
check_outputs  # again: the encoder ran after the first check; the result line vouches for both files

if [ -n "$MP4_BYTES" ]; then
  printf '{"ok":true,"glb_bytes":%s,"png_bytes":%s,"ms":%s,"mp4_bytes":%s}\n' \
    "$(stat -c %s -- "$GLB")" "$(stat -c %s -- "$PNG")" "$MS" "$MP4_BYTES"
elif [ -n "$MP4" ]; then
  printf '{"ok":true,"glb_bytes":%s,"png_bytes":%s,"ms":%s,"mp4":"%s"}\n' \
    "$(stat -c %s -- "$GLB")" "$(stat -c %s -- "$PNG")" "$MS" "$MP4"
else
  printf '{"ok":true,"glb_bytes":%s,"png_bytes":%s,"ms":%s}\n' \
    "$(stat -c %s -- "$GLB")" "$(stat -c %s -- "$PNG")" "$MS"
fi
