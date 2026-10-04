# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller ONEFILE spec for the Jarvis Windows tray agent.

ONEFILE build (single dist/jarvis-agent.exe, no COLLECT) bundling the
windows/agent package with tray.py as the entrypoint. Build happens ON the
Windows PC — PyInstaller cannot cross-compile (PLAN.md T3.2):

    pip install pyinstaller
    pyinstaller windows\\jarvis-agent.spec
"""

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

a = Analysis(
    [os.path.join(SPECPATH, "agent", "tray.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=["pystray._win32"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="jarvis-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
