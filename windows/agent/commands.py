"""Allowlisted Windows-side command runner.

Only names in ALLOWED may ever execute. Anything else is refused — never
dispatched, never shelled out — and the caller replies `{ok: false, data: "refused"}`.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time

import psutil

logger = logging.getLogger(__name__)

ALLOWED = ("specs", "uptime", "disk", "processes", "wake", "sleep", "list_dir")

IS_WINDOWS = platform.system() == "Windows"
LIST_DIR_LIMIT = 200
PROCESS_LIMIT = 25
POWERSHELL_TIMEOUT_S = 15


def run_command(name: str | None, payload=None) -> tuple[bool, object]:
    """Dispatch an allowlisted command. Returns the (ok, data) for the reply envelope."""
    if name not in ALLOWED:
        logger.warning("refusing non-allowlisted command: %r", name)
        return False, "refused"
    try:
        return True, _HANDLERS[name](payload)
    except Exception as exc:  # a failing command must not kill the agent
        logger.exception("command %s failed", name)
        return False, f"error: {exc}"


def _powershell(command: str) -> str:
    """Run a PowerShell snippet on Windows; raises on non-zero exit."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=POWERSHELL_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise RuntimeError(f"powershell exit {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def _specs(_payload) -> dict:
    mem = psutil.virtual_memory()
    info = {
        "os": platform.platform(),
        "machine": platform.machine(),
        "cpu": platform.processor() or f"{psutil.cpu_count(logical=False)} core(s)",
        "cpu_cores": psutil.cpu_count(logical=False),
        "cpu_threads": psutil.cpu_count(logical=True),
        "ram_total_gb": round(mem.total / 1024**3, 1),
        "ram_used_percent": mem.percent,
    }
    if IS_WINDOWS:
        # psutil can't reach motherboard/machine model — PowerShell CIM fallback.
        try:
            info["motherboard"] = " ".join(
                _powershell("(Get-CimInstance Win32_BaseBoard).Manufacturer; (Get-CimInstance Win32_BaseBoard).Product").split()
            )
            info["model"] = _powershell("(Get-CimInstance Win32_ComputerSystem).Model")
        except (OSError, RuntimeError, subprocess.SubprocessError):
            logger.warning("PowerShell CIM fallback failed; returning psutil-only specs")
    return info


def _uptime(_payload) -> dict:
    return {"uptime_sec": int(time.time() - psutil.boot_time())}


def _disk(_payload) -> dict:
    usage = psutil.disk_usage(os.path.abspath(os.sep))
    return {
        "total_gb": round(usage.total / 1024**3, 1),
        "used_gb": round(usage.used / 1024**3, 1),
        "free_gb": round(usage.free / 1024**3, 1),
        "used_percent": usage.percent,
    }


def _processes(payload) -> dict:
    limit = PROCESS_LIMIT
    if isinstance(payload, dict) and isinstance(payload.get("limit"), int):
        limit = max(1, payload["limit"])
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        rss = p.info["memory_info"].rss if p.info.get("memory_info") else 0
        procs.append(
            {"pid": p.info["pid"], "name": p.info.get("name") or "?", "rss_mb": round(rss / 1024**2, 1)}
        )
    procs.sort(key=lambda x: x["rss_mb"], reverse=True)
    return {"count": len(procs), "top": procs[:limit]}


def _send_wake_key() -> None:
    """Nudge the display awake on Windows with a harmless Ctrl press."""
    import ctypes

    ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
    ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)  # Ctrl up


def _suspend() -> None:
    subprocess.run(
        ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        check=True,
        timeout=30,
    )


def _wake(_payload) -> str:
    if not IS_WINDOWS:
        return "wake: Windows only, not executed on this machine"
    _send_wake_key()
    return "wake sent"


def _sleep(_payload) -> str:
    if not IS_WINDOWS:
        return "sleep: Windows only, not executed on this machine"
    _suspend()
    return "sleep requested"


def _home() -> str:
    return os.path.expanduser("~")


def _list_dir(payload) -> dict:
    home = _home()
    raw = payload if isinstance(payload, str) and payload.strip() else home
    target = os.path.abspath(os.path.expanduser(raw))
    if target != home and not target.startswith(home + os.sep):
        raise ValueError("path outside the user home tree is not allowed")
    if not os.path.isdir(target):
        raise ValueError(f"not a directory: {target}")
    return {"path": target, "entries": sorted(os.listdir(target))[:LIST_DIR_LIMIT]}


_HANDLERS = {
    "specs": _specs,
    "uptime": _uptime,
    "disk": _disk,
    "processes": _processes,
    "wake": _wake,
    "sleep": _sleep,
    "list_dir": _list_dir,
}
