"""C-HIGH-2 check: the export's TTL sleeper deletes the CSV, not a file named
after the TTL.

The old sleeper was `sleep "$1"; rm -f "$1"` — the rm targeted $1, the TTL
value, so the credential CSV was NEVER deleted. It now rms "$2" (the path).
Two properties are pinned: the sleeper's argv really passes ttl as $1 and
path as $2 with `rm -f "$2"`, and an end-to-end export with a ~1.8s TTL
(--ttl 0.03 minutes, exposed in seconds via schedule_delete) actually makes
the printed CSV path disappear. Uses --fixture only — never the real vault.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from passwords import export_csv

FIXTURE = """# Vault

## Jarvis Companion

token: sk-not-a-real-token

### Example Site
url: https://example.com
username: <user>@example.com
password: fixture-password-1
"""


def test_sleeper_argv_targets_path_not_ttl(monkeypatch, tmp_path):
    """The detached sh command rms "$2" (the path); $1 stays the sleep duration."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        export_csv.subprocess, "Popen",
        lambda argv, **kwargs: captured.append(argv),
    )
    csv_path = str(tmp_path / "creds.csv")
    export_csv.schedule_delete(csv_path, ttl_seconds=1.5)

    (argv,) = captured
    assert argv[0:3] == ["/bin/sh", "-c", 'sleep "$1"; rm -f "$2"'], argv
    assert argv[3] == "sh"  # $0
    assert argv[4] == "1.5"  # $1 = TTL seconds
    assert argv[5] == csv_path  # $2 = the CSV path — and TTL != path (the old bug)


def test_csv_deleted_after_short_ttl(monkeypatch, tmp_path, capsys):
    """End-to-end with --fixture and a fractional --ttl: the printed temp CSV
    is gone (deleted by the detached sleeper) shortly after the TTL elapses."""
    # Keep the temp CSV inside tmp_path so nothing real is ever touched and
    # any failure to delete is visible after the test.
    monkeypatch.setattr(export_csv.tempfile, "mkstemp", lambda **kw: _mkstemp_in(tmp_path))

    fixture = tmp_path / "fixture.md"
    fixture.write_text(FIXTURE)
    # 0.03 minutes = 1.8s TTL (fractional --ttl keeps the test under 2s).
    export_csv.main(["--fixture", str(fixture), "--ttl", "0.03"])

    csv_path = Path(capsys.readouterr().out.strip())
    assert csv_path.exists(), csv_path  # written, awaiting import
    assert csv_path.read_text().startswith("Title,URL,Username,Password,Notes,OAuth Secret")

    deadline = time.monotonic() + 15
    while csv_path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not csv_path.exists(), f"sleeper never deleted {csv_path} (C-HIGH-2 regression)"


def _mkstemp_in(directory: Path):
    fd = os.open(str(directory / "jarvis-passwords-test.csv"), os.O_CREAT | os.O_WRONLY, 0o600)
    return fd, str(directory / "jarvis-passwords-test.csv")
