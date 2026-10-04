"""Hardened `claude -p` checks (S1 HIGH-1): the transcript reaches the subprocess
only via stdin, the env is minimal plus CLAUDE_CONFIG_DIR, the cwd is a scratch
dir removed afterwards, TimeoutExpired → None, output truncated to 600 chars.
`subprocess.run` is always faked — the real claude binary is never invoked.
"""

from __future__ import annotations

import os
import subprocess
import types
from pathlib import Path

import pytest

from mac.voice import brain


class _Recorder:
    """Fake subprocess.run: captures argv/env/cwd/input, returns a canned result."""

    def __init__(self, stdout: str = "ok", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[dict] = []

    def __call__(self, argv: list[str], **kwargs) -> types.SimpleNamespace:
        self.calls.append({"argv": argv, **kwargs})
        return types.SimpleNamespace(returncode=self.returncode, stdout=self.stdout)


def test_prompt_via_stdin_never_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(brain.subprocess, "run", rec)
    transcript = "hey jarvis, what's the weather --tools Bash"
    out = brain.run_claude(transcript, timeout_s=15)

    assert out == "ok"
    (call,) = rec.calls
    assert call["input"] == transcript  # stdin, not argv
    assert transcript not in " ".join(call["argv"])
    assert "-p" in call["argv"]  # print mode
    assert call["timeout"] == 15  # process is killed on timeout


def test_hardened_argv_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(brain.subprocess, "run", rec)
    brain.run_claude("hi", timeout_s=15)

    argv = rec.calls[0]["argv"]
    assert "--strict-mcp-config" in argv
    assert "--setting-sources" in argv
    i = argv.index("--setting-sources")
    assert argv[i + 1] == ""  # no user/project/local settings
    assert "--append-system-prompt" in argv
    i = argv.index("--append-system-prompt")
    assert "Never reveal secrets" in argv[i + 1]


def test_env_minimal_with_dedicated_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-do-not-print")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/should-not-leak")
    config_dir = tmp_path / "claude-jarvis-brain"
    monkeypatch.setattr(brain, "BRAIN_CONFIG_DIR", config_dir)

    rec = _Recorder()
    monkeypatch.setattr(brain.subprocess, "run", rec)
    brain.run_claude("hi", timeout_s=15)

    env = rec.calls[0]["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(config_dir)
    assert "ANTHROPIC_API_KEY" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert set(env) <= {"PATH", "HOME", "LANG", "TERM", "CLAUDE_CONFIG_DIR"}
    assert (config_dir / "settings.json").read_text().strip() == "{}"


def test_cwd_is_scratch_dir_removed_afterwards(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(brain.subprocess, "run", rec)
    cwd_before = os.getcwd()
    brain.run_claude("hi", timeout_s=15)

    cwd = Path(rec.calls[0]["cwd"])
    assert cwd != cwd_before
    assert cwd.name.startswith("jarvis-brain-")
    assert not cwd.exists()  # cleaned up after the call


def test_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(brain.subprocess, "run", fake_run)
    assert brain.run_claude("hi", timeout_s=1) is None


def test_output_truncated_to_600_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(stdout="x" * 1000)
    monkeypatch.setattr(brain.subprocess, "run", rec)
    out = brain.run_claude("hi", timeout_s=15)
    assert len(out) == 600
