#!/usr/bin/env bash
# Deliver the built Jarvis Windows agent .exe to the PC "<user>" over Taildrop
# (DESIGN.md "Delivery / install paths"). The build happens ON the Windows PC
# (PyInstaller cannot cross-compile — PLAN.md T3.2); this copies the .exe once
# one exists locally and prints the install command to paste on the PC.
set -euo pipefail

EXE="${1:-dist/jarvis-agent.exe}"

if [[ ! -f "$EXE" ]]; then
    echo "error: $EXE not found." >&2
    echo "Build it on the Windows PC first: pip install pyinstaller && pyinstaller windows\\jarvis-agent.spec" >&2
    exit 1
fi

command -v tailscale >/dev/null 2>&1 || { echo "error: tailscale CLI not found in PATH" >&2; exit 1; }

echo "Sending $(basename "$EXE") to <user> ..."
tailscale file cp "$EXE" <user>:

cat <<'EOF'

Paste this on the Windows PC (PowerShell) to install and start the agent:

  New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\Jarvis" | Out-Null; Move-Item -Force "$env:USERPROFILE\Downloads\jarvis-agent.exe" "$env:LOCALAPPDATA\Jarvis\jarvis-agent.exe"; $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\Jarvis Agent.lnk'); $lnk.TargetPath = "$env:LOCALAPPDATA\Jarvis\jarvis-agent.exe"; $lnk.Save(); Start-Process "$env:LOCALAPPDATA\Jarvis\jarvis-agent.exe"

(Moves the Taildrop file from Downloads to %LOCALAPPDATA%\Jarvis, adds a Startup
shortcut so it relaunches on login, then starts it.)
EOF
