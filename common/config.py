"""Shared config: gateway port constant and bearer-token loading from the Secrets vault."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def load_env_file(path: str | os.PathLike | None = None) -> int:
    """Load a .env file's KEY=VALUE lines into os.environ (real env wins).

    No python-dotenv dependency: the gateway needs a handful of flat keys,
    so this tiny parser (blank/# lines skipped, one pair of matching quotes
    stripped) is enough. Returns the number of keys loaded; 0 if no file.
    """
    env_path = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    try:
        lines = env_path.read_text().splitlines()
    except OSError:
        return 0
    loaded = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
        loaded += 1
    return loaded


# Runs before any gateway module reads its env vars (they all import this
# module first); a missing .env is fine — unset vars then fail loudly at use.
load_env_file()

PORT = 8788

DEFAULT_VAULT_PATH = "~/Desktop/ObsidianVault/Secrets/Projects.md"
SECTION_HEADING = "## Jarvis Companion"


def read_section(path: str, heading: str = SECTION_HEADING) -> dict[str, str] | None:
    """Read a `## <heading>` section's `key: value` lines from vault markdown.

    The one parser for the vault's T0.1 markdown shape (used by get_token and
    the sign-in flow). `heading` may be given with or without the `## ` prefix;
    only `## ` headings bound sections. Lines inside that are empty or `#`-
    prefixed are skipped. Duplicate `key:` lines within a section are
    FIRST-wins (the first occurrence sets the value, later ones are ignored) —
    the semantics get_token shipped with in T0.1. Returns None when the file
    is unreadable (logged); {} when the section is absent.
    """
    wanted = heading.lstrip("#").strip()
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        logger.error("read_section: vault file not readable: %s", path)
        return None

    section: dict[str, str] = {}
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped[3:].strip() == wanted
            continue
        if in_section and stripped and not stripped.startswith("#") and ":" in stripped:
            key, _, value = stripped.partition(":")
            section.setdefault(key.strip(), value.strip())  # first-wins
    return section


def get_token(path: str | None = None, name: str = "token") -> str | None:
    """Read a bearer token from the vault markdown file at runtime.

    Finds the `## Jarvis Companion` section and returns the value of its
    `<name>: <value>` line (`token:` by default — the shared token; `ios_token:`
    for the phone-only token). Returns None on failure, logging which part was
    missing (file or token line) — never the value itself.
    """
    vault = os.path.expanduser(path) if path is not None else os.path.expanduser(DEFAULT_VAULT_PATH)
    section = read_section(vault)
    if section is None:  # unreadable file — already logged by read_section
        return None
    value = section.get(name)
    if value is None:
        logger.error("get_token: no `%s:` line in the %s section of %s", name, SECTION_HEADING, vault)
        return None
    if not value:
        logger.error("get_token: `%s:` line is empty in the %s section of %s", name, SECTION_HEADING, vault)
        return None
    return value
