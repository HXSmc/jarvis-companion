"""LeanSession.ask serializes per session: interleaved write/read pairs would
interleave their stream-json lines and corrupt every reply. No process runs:
Popen is faked; one ask = one written line + one result line.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mac.voice import lean


class FakeProc:
    """stdin.write queues one result line per write (marker = first text char)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []  # ("w"|"r", marker), in real order
        self.pending: list[str] = []
        self.lock = threading.Lock()
        self.stdin = self   # ask() writes to proc.stdin
        self.stdout = self  # ask() iterates proc.stdout line by line

    def write(self, line: str) -> None:
        marker = json.loads(line)["message"]["content"][-1]["text"][0]
        with self.lock:
            self.events.append(("w", marker))
            self.pending.append(marker)

    def flush(self) -> None:
        pass

    def __iter__(self):
        while True:
            with self.lock:
                if self.pending:
                    marker = self.pending.pop(0)
                    self.events.append(("r", marker))
                    yield json.dumps({"type": "result", "result": f"ok-{marker}"}) + "\n"
                    continue
            time.sleep(0.001)


def test_ask_is_serialized_per_session(monkeypatch):
    fake = FakeProc()
    monkeypatch.setattr(lean.subprocess, "Popen", lambda *a, **k: fake)
    session = lean.LeanSession()
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}

    def run(marker: str) -> None:
        barrier.wait()
        results[marker] = session.ask(marker * 3).raw

    threads = [threading.Thread(target=run, args=(m,), daemon=True) for m in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert results == {"A": "ok-A", "B": "ok-B"}  # each ask got its own reply
    # strictly write->read->write->read: no interleaving of the two turns
    assert fake.events in (
        [("w", "A"), ("r", "A"), ("w", "B"), ("r", "B")],
        [("w", "B"), ("r", "B"), ("w", "A"), ("r", "A")],
    )
