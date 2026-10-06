"""build_jarvis.py checks: action wire format, token containment, sign argv.

No network, no real `shortcuts` call: the script module is loaded in-process,
the token comes from a tmp_path dummy file, and signing is stubbed at
`subprocess.run` (argv asserted). Plists are parsed back with plistlib.
"""

from __future__ import annotations

import importlib.util
import plistlib
import subprocess
from pathlib import Path

import pytest

DUMMY_TOKEN = "dummy-token-abc123"

SCRIPT = Path(__file__).resolve().parents[1] / "shortcuts" / "build_jarvis.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_jarvis", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


@pytest.fixture
def token_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "token"
    path.write_text(DUMMY_TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv("JARVIS_TOKEN_FILE", str(path))
    return path


def _build(tmp_path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Build into tmp_path with signing stubbed out; return the parsed plist."""
    mod = _load_module()

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/shortcuts")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod.main(["--out", str(tmp_path / "build")])
    return plistlib.loads((tmp_path / "build" / "Jarvis.shortcut").read_bytes())


def _walk_strings(node):
    """Yield every string leaf in a parsed plist tree."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_strings(item)


def test_action_sequence_and_name(token_file, tmp_path, monkeypatch) -> None:
    """Exactly the 4 note-proven identifiers, in order; name "Jarvis"."""
    wf = _build(tmp_path, monkeypatch)
    identifiers = [
        a["WFWorkflowActionIdentifier"] for a in wf["WFWorkflowActions"]
    ]
    assert identifiers == [
        "is.workflow.actions.dictatetext",
        "is.workflow.actions.downloadurl",
        "is.workflow.actions.getvalueforkey",
        "is.workflow.actions.speaktext",
    ]
    assert wf["WFWorkflowName"] == "Jarvis"


def test_http_method_post_and_url(token_file, tmp_path, monkeypatch) -> None:
    """downloadurl: POST against the gateway URL (default /siri/hey)."""
    wf = _build(tmp_path, monkeypatch)
    params = wf["WFWorkflowActions"][1]["WFWorkflowActionParameters"]
    assert params["WFHTTPMethod"] == "POST"
    assert params["WFURL"].endswith("/siri/hey")
    assert params["WFHTTPBodyType"] == "JSON"
    assert params["ShowHeaders"] is True


def test_json_body_references_dictation_output(token_file, tmp_path, monkeypatch) -> None:
    """The JSON body's `text` value is the Dictated Text action output."""
    wf = _build(tmp_path, monkeypatch)
    dictate_uuid = wf["WFWorkflowActions"][0]["WFWorkflowActionParameters"]["UUID"]
    body = wf["WFWorkflowActions"][1]["WFWorkflowActionParameters"]["WFJSONValues"]
    (item,) = body["Value"]["WFDictionaryFieldValueItems"]
    assert item["WFKey"]["Value"]["string"] == "text"
    value = item["WFValue"]
    assert value["WFSerializationType"] == "WFTextTokenString"
    assert value["Value"]["string"] == "￼"
    (attachment,) = value["Value"]["attachmentsByRange"].values()
    assert attachment["Type"] == "ActionOutput"
    assert attachment["OutputName"] == "Dictated Text"
    assert attachment["OutputUUID"] == dictate_uuid


def test_authorization_header_bearer(token_file, tmp_path, monkeypatch) -> None:
    """Headers envelope carries Authorization = "Bearer <token>"."""
    wf = _build(tmp_path, monkeypatch)
    headers = wf["WFWorkflowActions"][1]["WFWorkflowActionParameters"]["WFHTTPHeaders"]
    assert headers["WFSerializationType"] == "WFDictionaryFieldValue"
    (item,) = headers["Value"]["WFDictionaryFieldValueItems"]
    assert item["WFItemType"] == 0
    assert item["WFKey"]["Value"]["string"] == "Authorization"
    assert item["WFValue"]["Value"]["string"] == f"Bearer {DUMMY_TOKEN}"


def test_token_only_in_header_value(token_file, tmp_path, monkeypatch, capsys) -> None:
    """The token appears exactly once in the whole plist (the header value)
    and never in stdout."""
    wf = _build(tmp_path, monkeypatch)
    occurrences = [s for s in _walk_strings(wf) if DUMMY_TOKEN in s]
    assert occurrences == [f"Bearer {DUMMY_TOKEN}"]
    for line in capsys.readouterr().out.splitlines():
        assert DUMMY_TOKEN not in line


def test_sign_argv_absolute_paths(token_file, tmp_path, monkeypatch) -> None:
    """`shortcuts sign --mode anyone` with exact argv and absolute paths."""
    mod = _load_module()
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/shortcuts")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod.main(["--out", str(tmp_path / "build")])

    built = (tmp_path / "build" / "Jarvis.shortcut").resolve()
    signed = (tmp_path / "build" / "Jarvis-signed.shortcut").resolve()
    assert calls == [
        ["shortcuts", "sign", "--mode", "anyone", "--input", str(built), "--output", str(signed)]
    ]


def test_sign_skipped_without_shortcuts_cli(token_file, tmp_path, monkeypatch, capsys) -> None:
    """No `shortcuts` binary → build still succeeds, stdout has only the built path."""
    mod = _load_module()

    def spy(argv, **kwargs):
        raise AssertionError("subprocess.run must not be called when signing is skipped")

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod.subprocess, "run", spy)
    mod.main(["--out", str(tmp_path / "build")])

    out = capsys.readouterr().out.splitlines()
    assert out == [str(tmp_path / "build" / "Jarvis.shortcut")]


def test_missing_token_file_fails(tmp_path, monkeypatch) -> None:
    """No JARVIS_TOKEN_FILE → non-zero exit, clear message, no plist written."""
    mod = _load_module()
    monkeypatch.delenv("JARVIS_TOKEN_FILE", raising=False)
    with pytest.raises(SystemExit) as exc:
        mod.main(["--out", str(tmp_path / "build")])
    assert exc.value.code != 0
    assert not (tmp_path / "build" / "Jarvis.shortcut").exists()
