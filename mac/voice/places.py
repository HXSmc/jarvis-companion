"""Saved places ("home", "gym") in ONE private file: <OVERLAY_VAULT>/Secrets/jarvis-places.json.

Only this path in that folder is ever opened (O_NOFOLLOW, regular file, <= 64 KB, realpath under
<vault>/Secrets). Format: {"places": {name: {"label", "query", "lat"?, "lon"?}}}, <= 20 places,
names [a-z0-9' ]{1,24}. A damaged or missing file is no places (one warning, never the path). A
user-owned file with group/other bits is chmod 0600'd once (Syncthing copies may be 0644); a file
another user owns is refused. Writes go through a same-dir temp file + os.replace, mode 0600.
Places, queries and coordinates never reach a log line, a debug row or a model prompt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

FILE_NAME = "jarvis-places.json"
MAX_BYTES = 64 * 1024
MAX_PLACES = 20
NAME_RE = re.compile(r"[a-z0-9' ]{1,24}")
MAX_TEXT = 120


class Unset(Exception):
    """OVERLAY_VAULT is not set: there is no places file."""


class Unreadable(Exception):
    """The places file belongs to another user."""


def places_file() -> Path:
    vault = os.environ.get("OVERLAY_VAULT", "").strip()
    if not vault:
        raise Unset()
    return Path(vault).expanduser() / "Secrets" / FILE_NAME


def _clean_entry(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    label = str(raw.get("label") or "")[:MAX_TEXT]
    query = str(raw.get("query") or "")[:MAX_TEXT]
    out = {"label": label, "query": query}
    lat, lon = raw.get("lat"), raw.get("lon")
    if (isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and not isinstance(lat, bool)
            and not isinstance(lon, bool) and -90 <= lat <= 90 and -180 <= lon <= 180):
        out["lat"], out["lon"] = float(lat), float(lon)
    elif not query:
        return None
    return out


def _parse(data: bytes, raw: bool = False) -> dict[str, dict]:
    doc = json.loads(data.decode("utf-8"))
    places = doc.get("places") if isinstance(doc, dict) else None
    if not isinstance(places, dict) or len(places) > MAX_PLACES:
        raise ValueError("bad places")
    if raw:  # write-back: every other entry is kept exactly as it was
        return places
    out = {}
    for name, entry in places.items():
        clean = _clean_entry(entry)
        if isinstance(name, str) and NAME_RE.fullmatch(name) and clean:
            out[name] = clean
    return out


def _damaged(raw: bool) -> dict:
    if raw:
        raise Unreadable()
    return {}


def load(raw: bool = False) -> dict[str, dict]:
    """The saved places ({} when missing or damaged). Raises Unset / Unreadable for the two lines.
    raw=True: the entries as stored (for a write-back that leaves the others untouched); a file that
    is there but refused or damaged raises Unreadable, so a write never replaces the owner's places."""
    path = places_file()
    secrets = path.parent
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError:  # a symlink (ELOOP) or no access
        logger.warning("places file refused")
        return _damaged(raw)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            logger.warning("places file refused")
            return _damaged(raw)
        if os.path.realpath(path) != os.path.join(os.path.realpath(secrets), FILE_NAME):
            logger.warning("places file refused")
            return _damaged(raw)
        if st.st_uid != os.getuid():
            raise Unreadable()
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o600)  # Hub question 3: a user-owned 0644 copy is fixed once
            logger.warning("places file mode fixed")
        data = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        return _damaged(raw)
    try:
        return _parse(data, raw)
    except (ValueError, UnicodeDecodeError):
        logger.warning("places file damaged")
        return _damaged(raw)


def names() -> tuple[str, ...]:
    try:
        return tuple(load())
    except (Unset, Unreadable):
        return ()


def save_all(places: dict[str, dict]) -> None:
    """Write the whole map back atomically (same-dir temp file, os.replace), mode 0600."""
    path = places_file()
    if len(places) > MAX_PLACES:
        raise ValueError("too many places")
    if path.is_symlink():
        raise OSError("places file refused")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps({"places": places}, indent=2, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("places file too big")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".jarvis-places-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


def put(name: str, entry: dict) -> None:
    """Add or replace one place; every other entry is written back unchanged."""
    if not NAME_RE.fullmatch(name):
        raise ValueError("bad name")
    places = load(raw=True)
    places[name] = entry
    save_all(places)


def remove(name: str) -> bool:
    places = load(raw=True)
    if name not in places:
        return False
    del places[name]
    save_all(places)
    return True
