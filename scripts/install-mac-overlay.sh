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
-> .venv with the service deps -> Kokoro TTS model download -> swift build of mac/overlay
-> bundle + ad-hoc sign $HOME/Applications/JarvisOverlay.app -> write the gateway token file
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
TOKEN_DIR="$HOME/Library/Application Support/JarvisOverlay"
TOKEN_FILE="$TOKEN_DIR/token"
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
  run python3 -m venv "$REPO/.venv"
fi
run "$PY" -m pip install fastapi uvicorn httpx numpy openwakeword kokoro-onnx

# (d) Kokoro TTS model files (skip when already downloaded; never redistributed by this repo)
run mkdir -p "$TTS_DIR"
for MODEL_FILE in kokoro-v1.0.int8.onnx voices-v1.0.bin; do
  if [ -f "$TTS_DIR/$MODEL_FILE" ]; then
    echo "kokoro model present: $TTS_DIR/$MODEL_FILE"
  else
    run curl -fL --retry 3 -o "$TTS_DIR/$MODEL_FILE" "$KOKORO_BASE/$MODEL_FILE"
  fi
done

# (e) overlay Swift package, release build
run swift build -c release --package-path "$REPO/mac/overlay"

# (f) app bundle + ad-hoc signature
run mkdir -p "$APP/Contents/MacOS"
run cp "$REPO/mac/overlay/.build/release/JarvisOverlay" "$APP/Contents/MacOS/JarvisOverlay"
if (( DRY_RUN )); then
  echo "would: write $APP/Contents/Info.plist (com.jarvis.overlay, LSUIElement, mic + speech usage strings)"
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
run codesign --force --sign - "$APP"

# (g) gateway token file (value never printed; 0600 via umask 077)
run mkdir -p "$TOKEN_DIR"
if (( DRY_RUN )); then
  echo "would: write $TOKEN_FILE from common.config.get_token (token value never printed)"
else
  if ! (
    umask 077 && cd "$REPO" &&
    "$PY" -c 'from common import config; import sys; t = config.get_token(); sys.exit(1) if not t else open(sys.argv[1], "w").write(t)' "$TOKEN_FILE"
  ); then
    echo "install-mac-overlay: no gateway token found (config.get_token returned empty)" >&2
    exit 1
  fi
fi

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

# (j) permissions to grant once, with deep links to the right System Settings pane
cat <<'EOF'

TCC permissions to grant once (System Settings):
  Microphone:         x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone
  Speech Recognition: x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition
  Accessibility:      x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility
  Screen Recording:   x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture
  Claude login: run `claude` once in Terminal if it says Not logged in.
EOF
