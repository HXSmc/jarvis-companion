#!/usr/bin/env bash
# Deliver the Jarvis Windows agent bundle to the PC over Taildrop
# (DESIGN.md "Delivery / install paths"). Default is the one-click source zip
# from scripts/build_release.sh (windows/install.ps1 sets everything up on the
# PC); the legacy self-built .exe still works when passed explicitly.
set -euo pipefail

ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

WINPC="${JARVIS_WINPC_TAILDROP:?JARVIS_WINPC_TAILDROP is not set (copy .env.example to .env)}"
ZIP="${1:-dist/jarvis-windows-agent.zip}"

if [[ ! -f "$ZIP" ]]; then
    echo "error: $ZIP not found." >&2
    echo "Build it on the Mac first: bash scripts/build_release.sh" >&2
    echo "(legacy .exe path: build on the PC with pip install pyinstaller && pyinstaller windows\\\\jarvis-agent.spec, then pass dist/jarvis-agent.exe)" >&2
    exit 1
fi

command -v tailscale >/dev/null 2>&1 || { echo "error: tailscale CLI not found in PATH" >&2; exit 1; }

echo "Sending $(basename "$ZIP") to $WINPC ..."
tailscale file cp "$ZIP" "$WINPC":

if [[ "$ZIP" == *.zip ]]; then
    cat <<'EOF'

Paste this on the Windows PC (PowerShell) to install and start the agent:

  Expand-Archive "$env:USERPROFILE\Downloads\jarvis-windows-agent.zip" -DestinationPath "$env:LOCALAPPDATA\jarvis\src"; cd "$env:LOCALAPPDATA\jarvis\src"; powershell -ExecutionPolicy Bypass -File .\windows\install.ps1

(Installer: ensures Python, creates the venv, prompts for the gateway URL +
token (masked), adds a Startup shortcut, starts the tray agent. Re-running
updates the code and restarts. See windows/README.md.)
EOF
else
    cat <<'EOF'

Paste this on the Windows PC (PowerShell) to install and start the agent:

  New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\Jarvis" | Out-Null; Move-Item -Force "$env:USERPROFILE\Downloads\jarvis-agent.exe" "$env:LOCALAPPDATA\Jarvis\jarvis-agent.exe"; $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\Jarvis Agent.lnk'); $lnk.TargetPath = "$env:LOCALAPPDATA\Jarvis\jarvis-agent.exe"; $lnk.Save(); Start-Process "$env:LOCALAPPDATA\Jarvis\jarvis-agent.exe"

(Moves the Taildrop file from Downloads to %LOCALAPPDATA%\Jarvis, adds a Startup
shortcut so it relaunches on login, then starts it.)

ONE-TIME SETUP on the PC — the agent needs exactly two values, or it exits at
startup with "no gateway URL available" / "no bearer token available":

  1. Gateway URL (where to reach the Mac gateway over Tailscale — NOT the PC's
     own 127.0.0.1). Create this file containing the Mac's serve URL on its
     first line, e.g. https://mac-name.<tailnet>.<tld> :

       New-Item -ItemType Directory -Force "$env:APPDATA\jarvis" | Out-Null; Set-Content -Path "$env:APPDATA\jarvis\gateway_url" -Value "https://<MAC-SERVE-URL>"

     (or set a JARVIS_GATEWAY_URL env var to the same value)

  2. Bearer token (the vault's shared `token:` value). Copy it into:

       Set-Content -Path "$env:APPDATA\jarvis\token" -Value "<TOKEN-VALUE>"

     (or set a JARVIS_TOKEN env var to the same value)
EOF
fi
