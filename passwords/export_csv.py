"""Export Secrets-vault credentials to an Apple Passwords CSV (T4.3).

Reads vault-format markdown at runtime (same convention as common/config.py:
`## Section` headings with `key: value` lines; each password entry is a
`### Name` block) and emits the Apple Passwords CSV columns
`Title,URL,Username,Password,Notes,OAuth Secret`.

--dry-run prints the CSV to stdout (pair with --fixture for testing);
otherwise the CSV goes to a 0600 temp file, which --ttl minutes later is
deleted by a detached cleanup process. Credential values are never printed
or logged — the CSV content itself and the temp file path are the only output.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)

DEFAULT_VAULT_PATH = "~/Desktop/ObsidianVault/Secrets/Projects.md"

CSV_HEADER = ["Title", "URL", "Username", "Password", "Notes", "OAuth Secret"]

# vault `key:` line (lowercased) -> CSV column
FIELDS = {
    "url": "URL",
    "username": "Username",
    "password": "Password",
    "notes": "Notes",
    "otpauth": "OAuth Secret",
}


def parse_vault(text: str) -> list[dict[str, str]]:
    """Parse `## Section` / `### Entry` markdown into CSV row dicts.

    Bare `key: value` lines outside a `###` block (e.g. `token: ...`) are not
    password entries and are skipped, as are entries without a `password:` line.
    """
    rows: list[dict[str, str]] = []
    entry: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            entry = None
        elif line.startswith("### "):
            entry = {"Title": line[4:].strip()}
            rows.append(entry)
        elif entry is not None and line:
            key, sep, value = line.partition(":")
            column = FIELDS.get(key.strip().lower()) if sep else None
            if column:
                entry[column] = value.strip()

    complete = [row for row in rows if row.get("Password")]
    skipped = len(rows) - len(complete)
    if skipped:
        logger.info("parse_vault: skipped %d entry/entries without a password line", skipped)
    return complete


def load_vault_text(path: str | None) -> str:
    """Read the fixture file, or the real vault when path is None."""
    vault = os.path.expanduser(path) if path is not None else os.path.expanduser(DEFAULT_VAULT_PATH)
    try:
        with open(vault, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        logger.error("export_csv: vault file not readable: %s", vault)
        sys.exit(1)


def write_csv(rows: list[dict[str, str]], out) -> None:
    writer = csv.writer(out)
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow([row.get(column, "") for column in CSV_HEADER])


def schedule_delete(path: str, ttl_minutes: int) -> None:
    """Delete the temp file after the TTL via a detached process that
    survives this script exiting (owner needs the file alive to import it)."""
    subprocess.Popen(
        ["/bin/sh", "-c", 'sleep "$1"; rm -f "$1"', "sh", str(ttl_minutes * 60), path],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("export_csv: %s auto-deletes in %d minute(s)", path, ttl_minutes)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Export Secrets-vault credentials to an Apple Passwords CSV."
    )
    parser.add_argument(
        "--fixture", metavar="FILE",
        help="read this vault-format file instead of the real vault",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the CSV to stdout instead of writing a temp file",
    )
    parser.add_argument(
        "--ttl", type=int, metavar="MINUTES",
        help="auto-delete the temp file after this many minutes",
    )
    args = parser.parse_args(argv)

    text = load_vault_text(args.fixture)
    rows = parse_vault(text)
    if not rows:
        logger.error(
            "export_csv: no password entries found in %s",
            args.fixture or DEFAULT_VAULT_PATH,
        )
        sys.exit(1)

    if args.dry_run:
        write_csv(rows, sys.stdout)
        return

    fd, path = tempfile.mkstemp(prefix="jarvis-passwords-", suffix=".csv")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        write_csv(rows, f)
    logger.info("export_csv: %d entries written", len(rows))
    print(path)
    if args.ttl is not None:
        schedule_delete(path, args.ttl)


if __name__ == "__main__":
    main()
