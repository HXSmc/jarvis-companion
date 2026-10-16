#!/usr/bin/env python3
"""Build (and sign) the importable "Jarvis" Siri Shortcut — S1.

Writes a 4-action plist — Dictate Text → POST JSON to the gateway (Bearer
header) → Get Dictionary Value → Speak Text — mirroring the live-signed
sample in Brain/Research/macos-ios-26-siri-shortcuts-plist-creation.md
(every key below is Ground-Truthed there). Output is a binary plist:
`shortcuts sign` rejects raw XML plists even when `plutil -lint` passes.

Token: read at BUILD time from the 0600 file named by env JARVIS_TOKEN_FILE.
It is never hardcoded, printed, or written anywhere except the Authorization
header value inside the generated .shortcut.

Then signs locally via `shortcuts sign --mode people-who-know-me` (absolute
paths — the note's gotcha: sign unreliably resolves relative ones); `anyone`
would instead upload the shortcut, token included, to Apple for notarization.
If the `shortcuts` CLI is missing, signing is skipped with one message and
exit stays 0.

stdout carries only the output path(s); everything else goes to stderr.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# Split deploy (2026-10-16): the phone's iOS websocket now lives on the box,
# not the Mac — a Siri "show my Mac screen" POSTed to the Mac's own /siri/hey
# would call launch_screen() there, find no "ios" role connected (it's on
# the box), and silently do nothing (pushed:false). Point the Shortcut at
# the box; the Mac gateway stays up as a fallback but is no longer where the
# phone's socket lives. Rebuilding this constant is NOT enough on its own —
# the already-installed "Jarvis" Shortcut on the phone needs re-running this
# script (with the real token) and a manual re-import (one "Add Shortcut"
# tap); see docs/HANDOFF.md.
DEFAULT_URL = "https://minipc.<tailnet>.<tld>:8790/siri/hey"

# The magic-variable placeholder: literal U+FFFC in the string, never an XML
# entity (hex-confirmed against real exported samples in the research note).
OBJECT_REPLACEMENT = "￼"


def _token_string(text: str) -> dict:
    """Static text field value (WFTextTokenString, no attachments)."""
    return {"Value": {"string": text}, "WFSerializationType": "WFTextTokenString"}


def _attachment(output_name: str, output_uuid: str) -> dict:
    """ActionOutput reference to a producer action's UUID."""
    return {
        "OutputName": output_name,
        "OutputUUID": output_uuid,
        "Type": "ActionOutput",
    }


def _token_attachment(output_name: str, output_uuid: str) -> dict:
    """Single magic-variable reference, no literal text (WFTextTokenAttachment)."""
    return {
        "Value": _attachment(output_name, output_uuid),
        "WFSerializationType": "WFTextTokenAttachment",
    }


def _token_string_with_var(output_name: str, output_uuid: str) -> dict:
    """Literal U+FFFC standing for one magic variable (WFTextTokenString)."""
    return {
        "Value": {
            "string": OBJECT_REPLACEMENT,
            "attachmentsByRange": {"{0, 1}": _attachment(output_name, output_uuid)},
        },
        "WFSerializationType": "WFTextTokenString",
    }


def _dict_field(items: list[tuple[str, dict]]) -> dict:
    """WFDictionaryFieldValue envelope: array of {WFItemType, WFKey, WFValue}."""
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {"WFItemType": 0, "WFKey": _token_string(key), "WFValue": value}
                for key, value in items
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def build_workflow(url: str, token: str) -> dict:
    """The full 4-action workflow dict, shapes exactly per the research note."""
    dictate_uuid = str(uuid.uuid4())
    download_uuid = str(uuid.uuid4())
    getvalue_uuid = str(uuid.uuid4())

    actions = [
        # 1. Dictate Text — zero parameters beyond UUID; output "Dictated Text".
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.dictatetext",
            "WFWorkflowActionParameters": {"UUID": dictate_uuid},
        },
        # 2. Get Contents of URL — POST JSON {"text": <Dictated Text>} with the
        #    auth header.
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
            "WFWorkflowActionParameters": {
                "UUID": download_uuid,
                "WFURL": url,
                "WFHTTPMethod": "POST",
                "ShowHeaders": True,
                "WFHTTPHeaders": _dict_field(
                    [("Authorization", _token_string(f"Bearer {token}"))]
                ),
                "WFHTTPBodyType": "JSON",
                "WFJSONValues": _dict_field(
                    [("text", _token_string_with_var("Dictated Text", dictate_uuid))]
                ),
            },
        },
        # 3. Get Dictionary Value — gateway/siri.py returns {"reply": ...}.
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.getvalueforkey",
            "WFWorkflowActionParameters": {
                "UUID": getvalue_uuid,
                "WFInput": _token_attachment("Contents of URL", download_uuid),
                "WFDictionaryKey": "reply",
                "WFGetDictionaryValueType": "Value",
            },
        },
        # 4. Speak Text — speak the Dictionary Value.
        {
            "WFWorkflowActionIdentifier": "is.workflow.actions.speaktext",
            "WFWorkflowActionParameters": {
                "UUID": str(uuid.uuid4()),
                "WFText": _token_string_with_var("Dictionary Value", getvalue_uuid),
            },
        },
    ]

    return {
        "WFWorkflowActions": actions,
        "WFWorkflowName": "Jarvis",
        "WFWorkflowClientVersion": "4711",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": 61440,
            "WFWorkflowIconStartColor": 431817727,
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": [],
    }


def _read_token() -> str:
    """Token from the 0600 file named by JARVIS_TOKEN_FILE; exit 1 if absent."""
    path = os.environ.get("JARVIS_TOKEN_FILE")
    if not path:
        sys.stderr.write("error: JARVIS_TOKEN_FILE is not set\n")
        raise SystemExit(1)
    token_file = Path(path)
    if not token_file.is_file():
        sys.stderr.write(f"error: token file not found: {path}\n")
        raise SystemExit(1)
    return token_file.read_text(encoding="utf-8").strip()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL, help="gateway /siri/hey URL")
    parser.add_argument(
        "--out",
        default=None,
        help="output directory (default: shortcuts/build/ next to this script)",
    )
    args = parser.parse_args(argv)

    token = _read_token()
    out_dir = Path(args.out).resolve() if args.out else Path(__file__).resolve().parent / "build"
    out_dir.mkdir(parents=True, exist_ok=True)

    shortcut_path = out_dir / "Jarvis.shortcut"
    with open(shortcut_path, "wb") as f:
        plistlib.dump(build_workflow(args.url, token), f, fmt=plistlib.FMT_BINARY)
    print(shortcut_path)

    if shutil.which("shortcuts") is None:
        sys.stderr.write("shortcuts CLI not found — skipping sign step\n")
        return

    signed_path = out_dir / "Jarvis-signed.shortcut"
    # Absolute paths: sign unreliably resolves relative ones (research note).
    result = subprocess.run(
        [
            "shortcuts",
            "sign",
            "--mode",
            "people-who-know-me",
            "--input",
            str(shortcut_path.resolve()),
            "--output",
            str(signed_path.resolve()),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    print(signed_path)


if __name__ == "__main__":
    main()
