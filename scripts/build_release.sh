#!/usr/bin/env bash
# P1 — build the one-click release bundle into dist/ (DESIGN.md "Delivery / install paths").
# Produces:
#   dist/jarvis-windows-agent.zip  — windows/ + common/ + pyproject.toml + README.md
#                                    (unzip on the PC, run windows/install.ps1)
#   dist/JarvisCompanion-unsigned.ipa — copied from ~/Downloads if present (phone sideload)
#   dist/INSTALL.md                — PC / phone / gateway-prerequisite instructions
set -euo pipefail

cd "$(dirname "$0")/.."
DIST=dist
IPA="$HOME/Downloads/JarvisCompanion-unsigned.ipa"

mkdir -p "$DIST"

# Windows agent bundle (source install; the .exe build on the PC is legacy).
rm -f "$DIST/jarvis-windows-agent.zip"
zip -r -q "$DIST/jarvis-windows-agent.zip" \
    windows common pyproject.toml README.md \
    -x "*__pycache__*" "*.pyc" "*.pyo" ".DS_Store" "windows/tests/*"

# Phone IPA, when it has been exported.
if [[ -f "$IPA" ]]; then
    cp "$IPA" "$DIST/"
    echo "IPA copied: $(basename "$IPA")"
else
    echo "NOTE: $IPA not found — phone bundle skipped (export it from Xcode first)."
fi

cat > "$DIST/INSTALL.md" <<'EOF'
# Jarvis companion — install bundle

## Prerequisites (Mac gateway — do once)
1. `python3 -m venv .venv && .venv/bin/pip install -e .` in this repo
2. `launchctl load launchd/com.jarvis.companion.gateway.plist`
3. `scripts/serve.sh` — prints the gateway's tailnet URL (`https://<mac>.<tailnet>.<tld>:8790`);
   that URL is what the PC agent and the iPhone app use
4. Verify: `curl -s -o /dev/null -w '%{http_code}\n' "$URL/health" -H "Authorization: Bearer <token>"`
   (token from the Secrets vault `## Jarvis Companion` section) → 200

## Windows PC
1. Mac → PC (PC must be on): `tailscale file cp dist/jarvis-windows-agent.zip <user>:`
2. On the PC (PowerShell) — the Taildrop file lands in Downloads:
   ```powershell
   Expand-Archive "$env:USERPROFILE\Downloads\jarvis-windows-agent.zip" -DestinationPath "$env:LOCALAPPDATA\jarvis\src"
   cd "$env:LOCALAPPDATA\jarvis\src"
   powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
   ```
   The installer sets up Python (if missing), the venv, prompts for the gateway
   URL + token (token input is masked), adds a Startup shortcut and starts the
   tray agent. Re-running it updates the code and restarts. See windows/README.md.

## iPhone
- Unsigned IPA: `dist/JarvisCompanion-unsigned.ipa` (if present). Do the FIRST
  install over USB — the cable is needed for pairing regardless, and wireless
  iloader transfers died ~12% before (`device socket io failed`).
- Open iloader.app (in /Applications, v2.3.3) → IPA Installation → select the
  IPA; if all 3 free-account app slots are used, remove one first. Apple
  ID/2FA prompt on first install is expected.
- Gateway URL + token go into the app's settings (same values as the PC).
EOF

echo "--- dist/ ---"
ls -lh "$DIST"
