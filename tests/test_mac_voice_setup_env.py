"""jfollow4 round 3: the overlay service reads the installer's setup keys from the repo .env.

Live run: the installer wrote OVERLAY_VAULT / OVERLAY_COMPANION_URL to .env, but launchd gives the
service only PATH and nothing in its import chain loaded .env, so the vault and sessions stayed off.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _import_service(env_file: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("OVERLAY_VAULT", "OVERLAY_COMPANION_URL", "YOUTUBE_API_KEY", "HOST")}
    env["OVERLAY_ENV_FILE"] = str(env_file)
    prog = ("import os, mac.voice.service\n"
            "for k in ('OVERLAY_VAULT', 'OVERLAY_COMPANION_URL', 'YOUTUBE_API_KEY', 'HOST'):\n"
            "    print(k + '=' + os.environ.get(k, '<unset>'))\n")
    out = subprocess.run([sys.executable, "-c", prog], cwd=ROOT, env=env, capture_output=True,
                         text=True, timeout=60, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_service_start_loads_only_the_setup_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("YOUTUBE_API_KEY=abc\nHOST=0.0.0.0\n# comment\n"
                        f"OVERLAY_VAULT={tmp_path / 'My Vault'}\n"
                        "OVERLAY_COMPANION_URL='http://127.0.0.1:8788'\n")
    got = _import_service(env_file)
    assert got["OVERLAY_VAULT"] == str(tmp_path / "My Vault")
    assert got["OVERLAY_COMPANION_URL"] == "http://127.0.0.1:8788"
    # other keys stay out: the agent's claude child inherits os.environ, and HOST picks the bind
    assert got["YOUTUBE_API_KEY"] == "<unset>"
    assert got["HOST"] == "<unset>"


def test_real_env_wins_and_a_missing_file_is_fine(tmp_path, monkeypatch):
    from mac.voice import service

    env_file = tmp_path / ".env"
    env_file.write_text("OVERLAY_VAULT=/from/file\nOVERLAY_COMPANION_URL=\n")
    monkeypatch.setenv("OVERLAY_VAULT", "/from/env")
    monkeypatch.delenv("OVERLAY_COMPANION_URL", raising=False)
    assert service.load_setup_env(env_file) == []  # env wins; an empty value sets nothing
    assert os.environ["OVERLAY_VAULT"] == "/from/env"
    assert "OVERLAY_COMPANION_URL" not in os.environ
    assert service.load_setup_env(tmp_path / "missing") == []


def test_installer_writes_the_env_keys_before_it_starts_the_service():
    out = subprocess.run(["bash", str(ROOT / "scripts/install-mac-overlay.sh"), "--dry-run"],
                         capture_output=True, text=True, timeout=120, check=True).stdout.splitlines()

    def first(needle: str) -> int:
        return next(i for i, line in enumerate(out) if needle in line)

    start = first("launchctl bootstrap")
    assert first("set OVERLAY_VAULT in .env") < start
    assert first("create companion-token") < start
    assert first("set OVERLAY_COMPANION_URL") < start
