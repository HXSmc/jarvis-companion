"""get_token(name=...) check: named token lines inside `## Jarvis Companion`.

Fixture vault files only (fake values) — the real Secrets vault is never read.
Covers the default shared `token:` name, the `ios_token:` name (S2 role-bound
WS auth), and the missing/empty-line failures.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import config

SHARED_TOKEN = "jarvis_test_token_123"
IOS_TOKEN = "jarvis_ios_test_token_456"

VAULT_FIXTURE = f"""\
# Secrets

## Some Other Project
token: wrong-section-token
ios_token: wrong-section-ios-token

## Jarvis Companion
token: {SHARED_TOKEN}
ios_token: {IOS_TOKEN}
"""


def _vault(tmp_path: Path, text: str) -> str:
    vault = tmp_path / "Projects.md"
    vault.write_text(text, encoding="utf-8")
    return str(vault)


def test_default_name_reads_shared_token(tmp_path):
    assert config.get_token(path=_vault(tmp_path, VAULT_FIXTURE)) == SHARED_TOKEN


def test_ios_token_name_reads_the_ios_line(tmp_path):
    path = _vault(tmp_path, VAULT_FIXTURE)
    assert config.get_token(path=path, name="ios_token") == IOS_TOKEN
    # The two names read DIFFERENT lines of the same section.
    assert config.get_token(path=path) != config.get_token(path=path, name="ios_token")


def test_named_line_missing_returns_none(tmp_path):
    vault = _vault(
        tmp_path,
        "## Jarvis Companion\ntoken: " + SHARED_TOKEN + "\n",
    )
    assert config.get_token(path=vault, name="ios_token") is None


def test_empty_named_line_returns_none(tmp_path):
    vault = _vault(tmp_path, "## Jarvis Companion\nios_token:   \n")
    assert config.get_token(path=vault, name="ios_token") is None


def test_section_or_file_missing_returns_none(tmp_path):
    assert config.get_token(path=_vault(tmp_path, "## Elsewhere\nios_token: x\n"),
                            name="ios_token") is None
    assert config.get_token(path=str(tmp_path / "nope.md"), name="ios_token") is None


def test_other_sections_token_lines_are_ignored(tmp_path):
    # A `token:`/`ios_token:` line OUTSIDE the section must never match.
    assert config.get_token(path=_vault(
        tmp_path,
        f"ios_token: outsider\ntoken: outsider\n\n## Jarvis Companion\ntoken: {SHARED_TOKEN}\nios_token: {IOS_TOKEN}\n",
    ), name="ios_token") == IOS_TOKEN


def test_duplicate_token_lines_are_first_wins(tmp_path):
    # T0.1 semantics: the FIRST `token:` line in the section wins; a later
    # duplicate (e.g. from a hand-edited vault) must not overwrite it.
    assert config.get_token(path=_vault(
        tmp_path,
        f"## Jarvis Companion\ntoken: {SHARED_TOKEN}\ntoken: later-duplicate\n",
    )) == SHARED_TOKEN


def test_ios_token_and_token_do_not_shadow_each_other(tmp_path):
    # Distinct key names are distinct entries: a duplicate `token:` line
    # first-wins on `token` alone and leaves the `ios_token` line untouched
    # (and vice versa).
    path = _vault(
        tmp_path,
        f"## Jarvis Companion\ntoken: {SHARED_TOKEN}\ntoken: dup\nios_token: {IOS_TOKEN}\n",
    )
    assert config.get_token(path=path) == SHARED_TOKEN
    assert config.get_token(path=path, name="ios_token") == IOS_TOKEN
