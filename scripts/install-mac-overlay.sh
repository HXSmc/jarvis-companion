#!/usr/bin/env bash
# install-mac-overlay.sh — build the Jarvis overlay app + voice service and load their LaunchAgents.
# --dry-run prints every mutating step as "would: <cmd>" and exits 0, so it also runs off macOS (CI check).
set -euo pipefail

usage() {
  cat <<'EOF'
install-mac-overlay.sh — build and install the Jarvis macOS voice overlay.

Usage: scripts/install-mac-overlay.sh [--dry-run] [--help]
  --dry-run  print every mutating step as "would: <cmd>", list missing tools, exit 0
  --help     show this help

Steps: tool checks (swift, brew, ffmpeg, espeak-ng, claude) -> brew install ffmpeg/espeak-ng
-> .venv with the service deps -> Kokoro TTS model download (sha256-verified)
-> swift build of mac/overlay
-> bundle + sign $HOME/Applications/JarvisOverlay.app with the stable local identity
   "JarvisOverlay Local Signing" (scripts/mac-overlay-sign.sh), so TCC grants survive reinstalls
(the service writes its own dedicated overlay token on first start; no token step here)
-> render launchd/ templates into $HOME/Library/LaunchAgents and bootstrap them
-> refuse a taken OVERLAY_PORT -> print the TCC permissions to grant.
EOF
}

DRY_RUN=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
APP="$HOME/Applications/JarvisOverlay.app"
TTS_DIR="${HWMODE_TTS_DIR:-$HOME/.local/share/kokoro}"
LOG_DIR="$HOME/Library/Logs/JarvisOverlay"
PORT="${OVERLAY_PORT:-8791}"
KOKORO_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

# run CMD...: execute, or print "would: CMD..." in dry-run.
run() {
  if (( DRY_RUN )); then echo "would: $*"; else "$@"; fi
}

# (a) platform + tool checks
OS="$(uname -s)"
if [ "$OS" != "Darwin" ]; then
  if (( DRY_RUN )); then
    echo "not macOS: dry-run only"
  else
    echo "install-mac-overlay: not macOS (uname -s = $OS), refusing to run" >&2
    exit 1
  fi
fi

MISSING=""
for TOOL in swift brew ffmpeg espeak-ng claude; do
  if ! command -v "$TOOL" >/dev/null 2>&1; then
    MISSING="$MISSING $TOOL"
    echo "missing tool: $TOOL"
  fi
done
if [ -n "$MISSING" ] && (( ! DRY_RUN )); then
  # swift/claude cannot be installed by this script (Xcode / claude.ai); brew fixes the rest below.
  case "$MISSING" in
    *swift*|*claude*)
      echo "install-mac-overlay: missing tools the script cannot install:$MISSING (Xcode for swift; claude from anthropic)" >&2
      exit 1
      ;;
  esac
  command -v brew >/dev/null 2>&1 || {
    echo "install-mac-overlay: brew missing, cannot install:$MISSING" >&2
    exit 1
  }
fi

# (b) brew-installable tools
INSTALL=()
command -v ffmpeg >/dev/null 2>&1 || INSTALL+=(ffmpeg)
command -v espeak-ng >/dev/null 2>&1 || INSTALL+=(espeak-ng)
if (( ${#INSTALL[@]} > 0 )); then
  run brew install "${INSTALL[@]}"
fi
if (( ! DRY_RUN )); then
  for TOOL in swift brew ffmpeg espeak-ng claude; do
    command -v "$TOOL" >/dev/null 2>&1 || {
      echo "install-mac-overlay: still missing after install step: $TOOL" >&2
      exit 1
    }
  done
fi

# (c) venv + service deps
if [ -x "$PY" ]; then
  echo "venv present: $REPO/.venv"
else
  # kokoro-onnx 0.6.1 has no wheels for Python 3.14+: use the newest 3.10-3.13 found.
  BASE_PY=""
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 13) else 1)'; then
      BASE_PY="$cand"; break
    fi
  done
  if [ -z "$BASE_PY" ]; then
    echo "need Python 3.10-3.13 (kokoro-onnx has no 3.14 build): brew install python@3.13" >&2
    exit 1
  fi
  run "$BASE_PY" -m venv "$REPO/.venv"
fi
run "$PY" -m pip install fastapi==0.143.2 uvicorn==0.54.0 httpx==0.28.1 numpy==2.5.4 openwakeword==0.6.0 kokoro-onnx==0.6.1

# (d) Kokoro TTS model files (never redistributed by this repo; sha256-verified,
# freshly downloaded ones and files already on disk alike).
# case, not an associative array: macOS ships bash 3.2.
model_sha() {
  case "$1" in
    kokoro-v1.0.int8.onnx) echo 6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb ;;
    voices-v1.0.bin) echo bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d ;;
  esac
}
verify_model() {  # $1 = model file path; mismatch -> delete the file, abort the install
  if (( DRY_RUN )); then
    echo "would: verify sha256 $(basename "$1")"
    return 0
  fi
  local GOT
  GOT="$(shasum -a 256 "$1" | cut -d' ' -f1)"
  if [ "$GOT" != "$(model_sha "$(basename "$1")")" ]; then
    echo "install-mac-overlay: sha256 mismatch for $1 (got $GOT), deleting and aborting" >&2
    rm -f "${1:?}"
    exit 1
  fi
  echo "sha256 ok: $(basename "$1")"
}
run mkdir -p "$TTS_DIR"
for MODEL_FILE in kokoro-v1.0.int8.onnx voices-v1.0.bin; do
  if [ ! -f "$TTS_DIR/$MODEL_FILE" ]; then
    run curl -fL --retry 3 -o "$TTS_DIR/$MODEL_FILE" "$KOKORO_BASE/$MODEL_FILE"
  else
    echo "kokoro model present: $TTS_DIR/$MODEL_FILE"
  fi
  verify_model "$TTS_DIR/$MODEL_FILE"
done

# (e) overlay Swift package, release build
run swift build -c release --package-path "$REPO/mac/overlay"

# (f) app bundle + stable signature (ad hoc would reset TCC grants on every reinstall)
run mkdir -p "$APP/Contents/MacOS"
run cp "$REPO/mac/overlay/.build/release/JarvisOverlay" "$APP/Contents/MacOS/JarvisOverlay"
run mkdir -p "$APP/Contents/Resources"
run iconutil -c icns "$REPO/mac/overlay/Resources/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
if (( DRY_RUN )); then
  echo "would: write $APP/Contents/Info.plist (com.jarvis.overlay, AppIcon, LSUIElement, mic + speech usage strings)"
else
  cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleIdentifier</key>
	<string>com.jarvis.overlay</string>
	<key>CFBundleName</key>
	<string>JarvisOverlay</string>
	<key>CFBundleExecutable</key>
	<string>JarvisOverlay</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>0.1.0</string>
	<key>CFBundleVersion</key>
	<string>1</string>
	<key>LSMinimumSystemVersion</key>
	<string>14.0</string>
	<key>LSUIElement</key>
	<true/>
	<key>NSMicrophoneUsageDescription</key>
	<string>Jarvis listens for its wake word so you can talk to it hands-free.</string>
	<key>NSSpeechRecognitionUsageDescription</key>
	<string>Jarvis transcribes what you say so it can carry out your request.</string>
</dict>
</plist>
PLIST
fi
run bash "$REPO/scripts/mac-overlay-sign.sh" "$APP"
run touch "$APP"  # Finder and the Dock re-read the bundle icon after the new AppIcon.icns

# (g) token: none written here — the service mints its own dedicated overlay
# token (0600) on first start; the installer never touches token files.

# (h) refuse a port held by another process (our own service may already hold it: it is reloaded below)
if (( DRY_RUN )); then
  echo "would: refuse to continue if port $PORT is taken by another process (lsof -nP -iTCP:$PORT -sTCP:LISTEN)"
elif command -v lsof >/dev/null 2>&1; then
  OUR_PID="$(launchctl list 2>/dev/null | awk '$3 == "com.jarvis.overlay.service" {print $1}')"
  for LISTEN_PID in $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true); do
    if [ "$LISTEN_PID" != "$OUR_PID" ]; then
      echo "install-mac-overlay: port $PORT is already in use by another process (set OVERLAY_PORT to move):" >&2
      lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
      exit 1
    fi
  done
fi

# (h2) agent MCP config: mail/calendar servers for the agent tier (local 0600 files, never committed)
AGENT_SETUP=(python3 -m mac.voice.agent_setup)
if [ -x "$PY" ]; then AGENT_SETUP[0]="$PY"; fi
if (( DRY_RUN )); then AGENT_SETUP+=("--dry-run"); fi
echo "agent MCP setup:"
(cd "$REPO" && "${AGENT_SETUP[@]}") \
  || echo "install-mac-overlay: agent MCP setup failed (the agent tier starts without mail/calendar MCP); rerun later"

# (i) LaunchAgents: render the templates, then reload each agent
run mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
for PLIST in com.jarvis.overlay.plist com.jarvis.overlay.service.plist; do
  DST="$HOME/Library/LaunchAgents/$PLIST"
  if (( DRY_RUN )); then
    echo "would: sed __HOME__ __REPO__ __PYTHON__ __APP__ launchd/$PLIST > $DST"
    echo "would: launchctl bootout gui/\$(id -u) $DST"
    echo "would: launchctl bootstrap gui/\$(id -u) $DST"
  else
    sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$REPO|g" -e "s|__PYTHON__|$PY|g" -e "s|__APP__|$APP|g" \
      "$REPO/launchd/$PLIST" > "$DST"
    launchctl bootout "gui/$(id -u)" "$DST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$DST"
  fi
done

# (i2) setup checks (one line each): the editor CLI on the service's PATH, the notes vault
SVC_PLIST="$HOME/Library/LaunchAgents/com.jarvis.overlay.service.plist"
[ -f "$SVC_PLIST" ] || SVC_PLIST="$REPO/launchd/com.jarvis.overlay.service.plist"
SVC_PATH="$(sed -n '/<key>PATH<\/key>/{n;s|.*<string>\(.*\)</string>.*|\1|p;}' "$SVC_PLIST" | sed "s|__HOME__|$HOME|g")"
if PATH="$SVC_PATH" command -v code >/dev/null 2>&1; then
  echo "VS Code CLI: found on the service PATH (the editor scrolls to changes)"
else
  found=""
  for c in \
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" \
    "$HOME/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" \
    "/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code-insiders" \
    "/Applications/VSCodium.app/Contents/Resources/app/bin/codium"; do
    if [ -x "$c" ] && [ -f "$c" ]; then
      echo "VS Code CLI: found (bundled in the VS Code app)"
      found=1
      break
    fi
  done
  [ -n "$found" ] || echo "VS Code CLI: missing on the service PATH; in VS Code run Cmd+Shift+P > \"Shell Command: Install 'code' command in PATH\""
fi
if [ -n "${OVERLAY_VAULT:-}" ] || grep -qs '^OVERLAY_VAULT=' "$REPO/.env"; then
  echo "Notes vault: OVERLAY_VAULT is set"
else
  echo "Notes vault: OVERLAY_VAULT is not set; add OVERLAY_VAULT=<your vault folder> to $REPO/.env, then rerun this installer"
fi

# (j) permissions to grant once, with deep links to the right System Settings pane
cat <<'EOF'

TCC permissions to grant once (System Settings):
  Microphone:         x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone
  Speech Recognition: x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition
  Accessibility:      x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility
  Screen Recording:   x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture
  Claude login: run `claude` once in Terminal if it says Not logged in.
EOF
