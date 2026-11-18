"""Simulated tap end to end: scripts/jarvis-card posts a `mac` ask and polls it
with `answer`, the phone tap comes in on the iOS token, and every request runs
in-process against the gateway app (urlopen routed to it, token faked by
test_central's autouse fixture). Run with -s to see the CLI lines."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from test_central import IOS, TOKEN, _call, stub_router  # noqa: F401  (autouse fixture)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setenv("JARVIS_GATEWAY_DIR", str(ROOT))
    loader = importlib.machinery.SourceFileLoader("_jarvis_card_cli", str(ROOT / "scripts" / "jarvis-card"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    def urlopen(req, timeout=None):
        path = req.full_url[len(mod.GATEWAY):]
        resp = _call(req.get_method(), path, headers=dict(req.header_items()), content=req.data)
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status_code, "error", {}, io.BytesIO(resp.content))
        return io.BytesIO(resp.content)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def run(*args):
        """argv -> (printed line, exit code); SystemExit with a str is an error line."""
        out = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        code = 0
        try:
            mod.main(["jarvis-card", *args, "--session", "s-done"])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                out.write(e.code)
        line = out.getvalue().strip()
        print(f"$ jarvis-card {' '.join(args)}\n{line}\n(exit {code})", file=sys.__stderr__)
        return line, code
    return run


def _need_id(line):
    return line.split("id ", 1)[1].split(")", 1)[0]


def _phone_tap(need_id, answer, note=""):
    resp = _call("POST", "/central/cards/s-done/answer", headers=IOS,
                 json={"msg_id": f"m-{answer.replace(' ', '-')}", "need_id": need_id, "answer": answer, "note": note})
    assert resp.status_code == 200, resp.text


def test_simulated_tap_end_to_end(cli):
    outputs = []
    line, code = cli("ask", "mac", "Mac is needed for tests, free it")
    outputs.append(line)
    assert code == 0 and "jarvis-card answer" in line
    first = _need_id(line)

    line, code = cli("answer", first)
    outputs.append(line)
    assert (line, code) == ("waiting", 2)

    _phone_tap(first, "Free")
    line, code = cli("answer", first)
    outputs.append(line)
    assert (line, code) == ("free", 0)

    line, _ = cli("ask", "mac", "Mac is needed for tests, free it")
    second = _need_id(line)
    _phone_tap(second, "Message", note="stop\x1b[2J please wait")
    line, code = cli("answer", second)
    outputs.append(line)
    assert (line, code) == ("message: stop [2J please wait", 0)
    assert "\x1b" not in line

    cli("clear")
    line, code = cli("answer", second)
    outputs.append(line)
    assert (line, code) == ("gone", 3)

    assert not any(TOKEN in o for o in outputs)


def test_answer_reads_other_kinds(cli):
    line, _ = cli("ask", "choice", "Mac is needed", "--option", "Free", "--option", "Not yet")
    need_id = _need_id(line)
    _phone_tap(need_id, "Not yet")
    assert cli("answer", need_id) == ("answered: Not yet", 0)
    line, _ = cli("ask", "approve", "Plan ready")
    need_id = _need_id(line)
    _call("POST", "/central/sessions/s-done/message", headers=IOS, json={"msg_id": "m-r", "text": "go on"})
    assert cli("answer", need_id) == ("reply: go on", 0)
    assert cli("answer", "zz")[1] == 1  # gateway 422 -> error line, exit 1
