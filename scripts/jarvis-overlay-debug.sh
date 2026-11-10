#!/usr/bin/env bash
# jarvis-overlay-debug.sh — open the Jarvis voice debug page (wake -> words -> answer, per turn).
# The overlay token rides in the URL fragment: browsers never send a fragment to the
# server, and the page drops it from the address bar on load. Never printed here.
set -euo pipefail
PORT="${OVERLAY_PORT:-8791}"
TOKEN_FILE="${OVERLAY_TOKEN_FILE:-$HOME/Library/Application Support/JarvisOverlay/token}"
[ -r "$TOKEN_FILE" ] || { echo "no overlay token at $TOKEN_FILE: is the overlay service running?" >&2; exit 1; }
open "http://127.0.0.1:$PORT/overlay/debug#token=$(tr -d '[:space:]' < "$TOKEN_FILE")"
