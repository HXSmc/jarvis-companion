# Jarvis Windows agent  -  one-click installer (PowerShell 5.1+, no admin needed).
# Run from the unzipped bundle root (the dir containing pyproject.toml):
#
#   powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
#
# Idempotent: re-running refreshes the code (pip install -e .) and restarts
# the tray agent. Config files already present are never overwritten.
$ErrorActionPreference = "Stop"

$JarvisDir = Join-Path $env:LOCALAPPDATA "jarvis"
$VenvDir = Join-Path $JarvisDir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
$ConfigDir = Join-Path $env:APPDATA "jarvis"
$TokenFile = Join-Path $ConfigDir "token"
$GatewayUrlFile = Join-Path $ConfigDir "gateway_url"
$StartupLnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Jarvis Agent.lnk"

# --- 1. Python 3.11+ -------------------------------------------------------
$py = $null
try { $py = (Get-Command py -ErrorAction Stop).Source } catch { }
if (-not $py) {
    Write-Host "py launcher not found  -  installing Python 3.12 for this user (no admin)..."
    winget install -e --id Python.Python.3.12 --scope user
    # refresh PATH for this session, then re-check
    $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("PATH", "User")
    try { $py = (Get-Command py -ErrorAction Stop).Source } catch { }
}
if (-not $py) {
    throw "Python still not available after winget install. Open a NEW PowerShell window and re-run this script."
}
$pyVersion = & $py -3 --version 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "py -3 failed: $pyVersion"
}
Write-Host "Using $pyVersion"

# --- 2. venv ---------------------------------------------------------------
if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating venv at $VenvDir ..."
    & $py -3 -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}

# --- 3. install/refresh the agent code (editable, from this bundle dir) -----
$BundleRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $BundleRoot "pyproject.toml"))) {
    throw "pyproject.toml not found next to windows\  -  run this script from the unzipped bundle (see dist/INSTALL.md)."
}
Write-Host "Installing agent code (pip install -e .) ..."
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet -e $BundleRoot
if ($LASTEXITCODE -ne 0) { throw "pip install -e . failed" }

# --- 4. config files (prompt only when missing; never echoed) --------------
New-Item -ItemType Directory -Force $ConfigDir | Out-Null
if (Test-Path $GatewayUrlFile) {
    Write-Host "Gateway URL file already present - keeping it."
} else {
    $url = Read-Host "Gateway URL (the Mac's tailscale-serve address, e.g. https://mac-name.<tailnet>.<tld>)"
    if (-not $url) { throw "gateway URL is required (agent cannot start without it)" }
    Set-Content -Path $GatewayUrlFile -Value $url
    icacls $GatewayUrlFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
}
if (Test-Path $TokenFile) {
    Write-Host "Token file already present - keeping it."
} else {
    $sec = Read-Host "Gateway bearer token" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if (-not $token) { throw "token is required (agent cannot start without it)" }
    Set-Content -Path $TokenFile -Value $token
    icacls $TokenFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
}

# --- 5. Startup shortcut ----------------------------------------------------
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($StartupLnk)
$lnk.TargetPath = $VenvPythonw
$lnk.Arguments = "-m windows.agent.tray"
$lnk.WorkingDirectory = $JarvisDir
$lnk.Description = "Jarvis companion tray agent"
$lnk.Save()

# --- 6. (re)start the tray agent now ----------------------------------------
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*windows.agent.tray*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Process $VenvPythonw -ArgumentList "-m", "windows.agent.tray" -WorkingDirectory $JarvisDir

# --- 7. summary (no secrets) -------------------------------------------------
Write-Host ""
Write-Host "Jarvis agent installed and started (tray icon: green = online, grey = offline)."
Write-Host "Config: $GatewayUrlFile + $TokenFile"
Write-Host "Autostart: $StartupLnk (starts at login; re-run this script any time to update)."
