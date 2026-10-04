"""Shared config: gateway port constant and bearer-token loading from the Secrets vault."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

PORT = 8788

DEFAULT_VAULT_PATH = "~/Desktop/ObsidianVault/Secrets/Projects.md"
SECTION_HEADING = "## Jarvis Companion"


def get_token(path: str | None = None, name: str = "token") -> str | None:
    """Read a bearer token from the vault markdown file at runtime.

    Finds the `## Jarvis Companion` section and returns the value of its
    `<name>: <value>` line (`token:` by default — the shared token; `ios_token:`
    for the phone-only token). Returns None on failure, logging which part was
    missing (file, section, or token line) — never the value itself.
    """
    vault = os.path.expanduser(path) if path is not None else os.path.expanduser(DEFAULT_VAULT_PATH)
    try:
        with open(vault, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        logger.error("get_token: vault file not readable: %s", vault)
        return None

    prefix = f"{name}:"
    in_section = False
    section_seen = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped == SECTION_HEADING
            section_seen = section_seen or in_section
            continue
        if in_section and stripped.startswith(prefix):
            value = stripped[len(prefix):].strip()
            if value:
                return value
            logger.error(
                "get_token: `%s:` line is empty in the %s section of %s",
                name, SECTION_HEADING, vault,
            )
            return None
    if section_seen:
        logger.error("get_token: no `%s:` line in the %s section of %s", name, SECTION_HEADING, vault)
    else:
        logger.error("get_token: %s section not found in %s", SECTION_HEADING, vault)
    return None
