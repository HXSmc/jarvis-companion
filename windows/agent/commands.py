"""Allowlisted Windows-side command runner.

Only names in ALLOWED may ever execute. Anything else is refused — never
dispatched, never shelled out — and the caller replies `{ok: false, data: "refused"}`.
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import os
import platform
import re
import subprocess
import time

import psutil

logger = logging.getLogger(__name__)

ALLOWED = (
    "specs", "uptime", "disk", "processes", "wake", "sleep", "list_dir",
    "screen_frame", "screen_input", "open",
)

IS_WINDOWS = platform.system() == "Windows"
LIST_DIR_LIMIT = 200
PROCESS_LIMIT = 25
POWERSHELL_TIMEOUT_S = 15

# list_dir scope (LOW-3): these three trees and nothing else — the whole-home
# scope let a junction/symlink resolve anywhere the user could read.
LIST_DIR_ROOTS = tuple(
    os.path.join(os.path.expanduser("~"), name)
    for name in ("Desktop", "Documents", "Downloads")
)

# Screen control (SC3). w/q clamps mirror the gateway's query-param ranges —
# the agent is trusted but dumb, defense in depth.
SCREEN_W_MIN, SCREEN_W_MAX, SCREEN_W_DEFAULT = 320, 1600, 1170
SCREEN_Q_MIN, SCREEN_Q_MAX, SCREEN_Q_DEFAULT = 20, 90, 55

OPEN_NAME_RE = re.compile(r"^[\w .+-]{1,64}$")

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
ABS_SCALE = 0xFFFF  # SendInput absolute coords are 0..65535 across the display
WHEEL_NOTCH = 120  # one Windows wheel notch

VK = {
    "return": 0x0D, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "tab": 0x09, "space": 0x20,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 25)},
}
VK_MODS = {"cmd": 0x5B, "ctrl": 0x11, "alt": 0x12, "shift": 0x10}  # cmd = Win key


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        # c_long not c_ulong: layout is identical (DWORD-sized) but wheel
        # deltas are negative scrolling up and must read back signed.
        ("mouseData", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _INPUTUnion(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", ctypes.c_ulong), ("u", _INPUTUnion))


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


def _list_dir(payload) -> dict:
    raw = payload if isinstance(payload, str) and payload.strip() else LIST_DIR_ROOTS[0]
    # realpath collapses symlinks/junctions, normcase neutralises Windows case
    # and slash differences — startswith tricks don't survive either.
    target = os.path.normcase(os.path.realpath(os.path.expanduser(raw)))
    for root in LIST_DIR_ROOTS:
        root = os.path.normcase(os.path.realpath(root))
        try:
            if os.path.commonpath([root, target]) == root:
                break
        except ValueError:  # e.g. different Windows drives (C: vs D:)
            continue
    else:
        raise ValueError("path outside the allowed Desktop/Documents/Downloads roots is not allowed")
    if not os.path.isdir(target):
        raise ValueError(f"not a directory: {target}")
    return {"path": target, "entries": sorted(os.listdir(target))[:LIST_DIR_LIMIT]}


# --- screen control (SC3) ---------------------------------------------------

def _clamp(value, lo, hi, default) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _screen_frame(args) -> dict:
    # Lazy import: Pillow is a win32-only dependency, the module must import
    # on the Mac dev box where windows tests run.
    from PIL import ImageGrab

    args = args if isinstance(args, dict) else {}
    w = _clamp(args.get("w"), SCREEN_W_MIN, SCREEN_W_MAX, SCREEN_W_DEFAULT)
    q = _clamp(args.get("q"), SCREEN_Q_MIN, SCREEN_Q_MAX, SCREEN_Q_DEFAULT)
    img = ImageGrab.grab()
    img.thumbnail((w, 10**9))  # width-bounded, aspect kept, never upscales
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    return {"jpeg": base64.b64encode(buf.getvalue()).decode(), "w": img.width, "h": img.height}


def _mouse(flags, dx=0, dy=0, data=0) -> INPUT:
    return INPUT(INPUT_MOUSE, _INPUTUnion(mi=MOUSEINPUT(dx, dy, data, flags, 0, 0)))


def _vk_press(vk, down) -> INPUT:
    return INPUT(
        INPUT_KEYBOARD,
        _INPUTUnion(ki=KEYBDINPUT(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0, 0)),
    )


def _unicode_pair(unit) -> list[INPUT]:
    return [
        INPUT(INPUT_KEYBOARD, _INPUTUnion(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, 0))),
        INPUT(
            INPUT_KEYBOARD,
            _INPUTUnion(ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)),
        ),
    ]


def _send_inputs(structs) -> int:
    """One SendInput call with the whole batch — the single seam tests mock."""
    array = (INPUT * len(structs))(*structs)
    return ctypes.windll.user32.SendInput(len(structs), array, ctypes.sizeof(INPUT))


def _screen_input(args) -> str:
    structs: list[INPUT] = []
    for ev in (args if isinstance(args, dict) else {}).get("events", []):
        kind = ev.get("type")
        if kind in ("move", "tap", "dblclick", "right", "drag"):
            structs.append(
                _mouse(
                    MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                    int(ev["x"] * ABS_SCALE),
                    int(ev["y"] * ABS_SCALE),
                )
            )
            if kind == "tap":
                structs += [_mouse(MOUSEEVENTF_LEFTDOWN), _mouse(MOUSEEVENTF_LEFTUP)]
            elif kind == "dblclick":
                structs += 2 * [_mouse(MOUSEEVENTF_LEFTDOWN), _mouse(MOUSEEVENTF_LEFTUP)]
            elif kind == "right":
                structs += [_mouse(MOUSEEVENTF_RIGHTDOWN), _mouse(MOUSEEVENTF_RIGHTUP)]
            elif kind == "drag":
                # ponytail: v1 drag = press-move-release at the event point —
                # the event envelope carries no path to interpolate.
                structs += [_mouse(MOUSEEVENTF_LEFTDOWN), _mouse(MOUSEEVENTF_LEFTUP)]
        elif kind == "scroll":
            structs.append(_mouse(MOUSEEVENTF_WHEEL, data=int(ev["dy"] * WHEEL_NOTCH)))
        elif kind == "text":
            raw = (ev.get("text") or "").encode("utf-16-le")
            for i in range(0, len(raw), 2):  # one down/up pair per UTF-16 unit
                structs += _unicode_pair(raw[i] | raw[i + 1] << 8)
        elif kind == "key":
            mod_vks = [VK_MODS[m] for m in ev.get("mods", [])]
            for mod in mod_vks:  # mods wrap the main key press, then release
                structs.append(_vk_press(mod, down=True))
            structs += [_vk_press(VK[ev["key"]], down=True), _vk_press(VK[ev["key"]], down=False)]
            for mod in reversed(mod_vks):
                structs.append(_vk_press(mod, down=False))
    _send_inputs(structs)
    return f"sent {len(structs)} input(s)"


def _open(args) -> str:
    name = args.get("name") if isinstance(args, dict) else None
    if not isinstance(name, str) or not OPEN_NAME_RE.fullmatch(name):
        raise ValueError("invalid app name")
    try:
        os.startfile(name)  # no shell fallback — a shell `start` round-trip is a needless injection surface (D3)
    except OSError:
        # error:-prefixed failure so the gateway surfaces it (run_command wraps this as ok=false)
        raise RuntimeError(f"cannot open {name}") from None
    return f"opened {name}"


_HANDLERS = {
    "specs": _specs,
    "uptime": _uptime,
    "disk": _disk,
    "processes": _processes,
    "wake": _wake,
    "sleep": _sleep,
    "list_dir": _list_dir,
    "screen_frame": _screen_frame,
    "screen_input": _screen_input,
    "open": _open,
}
