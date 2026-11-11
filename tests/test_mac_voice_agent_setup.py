"""agent_setup: mail/calendar MCP seeding for the overlay agent tier.

No real ~/.claude.json is ever touched: HOME and OVERLAY_SUPPORT_DIR are
monkeypatched to tmp_path in every main() test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mac.voice import agent_setup


def test_select_servers_keeps_mail_and_calendar_only():
    cfg = {"mcpServers": {"mailbox": {"command": "x"}, "Calendar": {"command": "y"},
                          "github": {"command": "z"}, "mailboxx-not-mail": "not-a-dict"}}
    assert agent_setup.select_servers(cfg, "/srv/u") == {
        "mailbox": {"command": "x"}, "Calendar": {"command": "y"}}


def test_select_servers_merges_project_level_top_level_wins():
    cfg = {"mcpServers": {"mailbox": {"top": True}},
           "projects": {"/srv/u": {"mcpServers": {"mailbox": {"project": True},
                                                   "calendar-mcp": {"project": True}}}}}
    merged = agent_setup.select_servers(cfg, "/srv/u")
    assert merged == {"mailbox": {"top": True}, "calendar-mcp": {"project": True}}


def test_read_only_tools_filters_reads_from_writes():
    servers = {"mailbox": {}}
    names = ["mcp__mailbox__search_messages", "mcp__mailbox__list_labels",  # list… contains "label"!
             "mcp__mailbox__get_message", "mcp__mailbox__send_message",
             "mcp__mailbox__draft_reply", "mcp__mailbox__delete_message",
             "mcp__other__search_things", "mcp__mailbox"]  # unknown server + malformed
    assert agent_setup.read_only_tools(names, servers) == \
        ["mcp__mailbox__get_message", "mcp__mailbox__search_messages"]


def test_read_only_tools_sorted_and_deduped():
    names = ["mcp__mailbox__fetch_x", "mcp__mailbox__fetch_x", "mcp__cal__query_y"]
    assert agent_setup.read_only_tools(names, {"mailbox": {}, "cal": {}}) == \
        ["mcp__cal__query_y", "mcp__mailbox__fetch_x"]


def test_tool_names_from_init_skips_bad_lines():
    lines = ['not json', '{"type":"other"}', '[]', 'noise',
             '{"type":"system","subtype":"init","tools":["mcp__mailbox__search_messages","WebSearch"]}',
             '{"type":"system","subtype":"init","tools":["too_late"]}']
    assert agent_setup.tool_names_from_init(lines) == ["mcp__mailbox__search_messages", "WebSearch"]
    assert agent_setup.tool_names_from_init(["nope", "{}"]) == []


def test_write_private_modes(tmp_path):
    out = tmp_path / "nested" / "agent-mcp.json"
    agent_setup.write_private(out, {"a": 1})
    assert (out.stat().st_mode & 0o777) == 0o600
    assert (out.parent.stat().st_mode & 0o777) == 0o700
    assert json.loads(out.read_text()) == {"a": 1}


def _claude_json(home: Path, servers: dict) -> None:
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}))


def _main_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    support = tmp_path / "support"
    home.mkdir()
    support.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OVERLAY_SUPPORT_DIR", str(support))
    return home, support


def test_main_keeps_existing_agent_mcp(tmp_path, monkeypatch, capsys):
    home, support = _main_env(tmp_path, monkeypatch)
    _claude_json(home, {"mailbox": {"command": "x"}})
    mine = {"mcpServers": {"my-own": {"command": "y"}}}
    (support / "agent-mcp.json").write_text(json.dumps(mine))
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run(INIT))
    assert agent_setup.main([]) == 0
    assert "kept your existing agent-mcp.json" in capsys.readouterr().out
    assert json.loads((support / "agent-mcp.json").read_text()) == mine


def test_main_no_servers_writes_nothing(tmp_path, monkeypatch, capsys):
    home, support = _main_env(tmp_path, monkeypatch)
    _claude_json(home, {"github": {"command": "z"}})
    assert agent_setup.main([]) == 0
    assert ("no mail or calendar MCP servers found; the agent gets git, "
            "GitHub and files only") in capsys.readouterr().out
    assert not (support / "agent-mcp.json").exists()
    assert not (support / "agent-tools.json").exists()


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    home, support = _main_env(tmp_path, monkeypatch)
    _claude_json(home, {"mailbox": {"command": "x"}})
    assert agent_setup.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would: copy mail/calendar MCP servers" in out
    assert "would: list their tools with claude and write agent-tools.json" in out
    assert not (support / "agent-mcp.json").exists()


def _fake_run(stdout: str | Exception):
    def fake(*_a, **_k):
        if isinstance(stdout, Exception):
            raise stdout
        return SimpleNamespace(stdout=stdout)
    return fake


INIT = ('{"type":"system","subtype":"init","tools":'
        '["mcp__mailbox__search_messages","mcp__mailbox__send_message","WebSearch"]}\n')


def test_main_writes_both_files_and_hides_canary(tmp_path, monkeypatch, capsys):
    home, support = _main_env(tmp_path, monkeypatch)
    _claude_json(home, {"mailbox": {"command": "x", "env": {"TOKEN": "CANARY-XYZ"}}})
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run(INIT))
    assert agent_setup.main([]) == 0
    captured = capsys.readouterr()
    assert "CANARY-XYZ" not in captured.out + captured.err
    assert "1 server(s), 1 read-only tool(s), 1 ask first, 0 hidden" in captured.out
    mcp = json.loads((support / "agent-mcp.json").read_text())
    assert mcp == {"mcpServers": {"mailbox": {"command": "x", "env": {"TOKEN": "CANARY-XYZ"}}}}
    tools = json.loads((support / "agent-tools.json").read_text())
    assert tools == {"read_only": ["mcp__mailbox__search_messages"],
                     "confirm": ["mcp__mailbox__send_message"], "hidden": []}
    for name in ("agent-mcp.json", "agent-tools.json"):
        assert (support / name).stat().st_mode & 0o777 == 0o600


def test_main_dry_run_with_existing_mcp_calls_no_claude(tmp_path, monkeypatch, capsys):
    home, support = _main_env(tmp_path, monkeypatch)
    (support / "agent-mcp.json").write_text(json.dumps({"mcpServers": {"my-own": {"command": "y"}}}))
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run(AssertionError("claude ran")))
    assert agent_setup.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would: refresh agent-tools.json from your existing agent-mcp.json" in out
    assert not (support / "agent-tools.json").exists()


MAIL_TOOLS = ["search_emails", "get_email", "list_accounts", "list_labels", "read_thread",
              "get_attachment", "find_contacts", "fetch_profile", "query_inbox", "send_email",
              "create_draft", "reply_email", "forward_email", "delete_email", "remove_account",
              "add_account", "modify_labels", "trash_email", "archive_email"]
CAL_TOOLS = ["list_events", "get_event", "create_event", "update_event", "respond_to_event",
             "delete_event"]


def _names(server: str, tools: list[str]) -> list[str]:
    return [f"mcp__{server}__{t}" for t in tools]


def test_tools_split_and_refreshed(tmp_path, monkeypatch, capsys):
    servers = {"multi-gmail": {}, "calendar": {}}
    names = _names("multi-gmail", MAIL_TOOLS) + _names("calendar", CAL_TOOLS)
    split = agent_setup.split_tools(names, servers)
    assert split["read_only"] == sorted(
        _names("multi-gmail", ["search_emails", "get_email", "list_accounts", "read_thread",
                               "get_attachment", "find_contacts", "fetch_profile", "query_inbox"])
        + _names("calendar", ["list_events", "get_event"]))
    assert split["confirm"] == sorted(
        _names("multi-gmail", ["send_email", "create_draft", "reply_email", "forward_email"])
        + _names("calendar", ["create_event", "update_event", "respond_to_event"]))
    # list_labels lands here too: "label" is a WRITE_HINT, so criterion 2's read_only excludes it.
    assert split["hidden"] == sorted(
        _names("multi-gmail", ["remove_account", "delete_email", "modify_labels", "add_account",
                               "trash_email", "archive_email", "list_labels"])
        + _names("calendar", ["delete_event"]))
    home, support = _main_env(tmp_path, monkeypatch)
    mcp = support / "agent-mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"multi-gmail": {"command": "x"},
                                              "calendar": {"command": "y"}}}))
    mcp.chmod(0o600)
    before = mcp.read_bytes()
    init = '{"type":"system","subtype":"init","tools":' + json.dumps(names) + '}\n'
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run(init))
    assert agent_setup.main([]) == 0
    out = capsys.readouterr().out
    assert "kept your existing agent-mcp.json" in out
    assert "refreshed agent-tools.json: 10 read-only, 7 ask first, 8 hidden" in out
    assert mcp.read_bytes() == before
    assert json.loads((support / "agent-tools.json").read_text()) == split
    for name in ("agent-mcp.json", "agent-tools.json"):
        assert (support / name).stat().st_mode & 0o777 == 0o600


def test_main_subprocess_failure_writes_empty_allowlist(tmp_path, monkeypatch, capsys):
    home, support = _main_env(tmp_path, monkeypatch)
    _claude_json(home, {"mailbox": {"command": "x"}})
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run(OSError("boom")))
    assert agent_setup.main([]) == 0
    assert "could not list tools; every mail/calendar tool will ask you first" in capsys.readouterr().out
    assert json.loads((support / "agent-tools.json").read_text()) == {"read_only": []}
    assert (support / "agent-tools.json").stat().st_mode & 0o777 == 0o600


def test_refresh_failure_keeps_the_last_tool_lists(tmp_path, monkeypatch, capsys):
    """A failed listing must not replace good lists with an empty one (every tool would ask again)."""
    home, support = _main_env(tmp_path, monkeypatch)
    (support / "agent-mcp.json").write_text(json.dumps({"mcpServers": {"mailbox": {"command": "x"}}}))
    good = {"read_only": ["mcp__mailbox__search_messages"], "confirm": [], "hidden": ["mcp__mailbox__delete"]}
    (support / "agent-tools.json").write_text(json.dumps(good))
    monkeypatch.setattr(agent_setup.subprocess, "run", _fake_run(OSError("boom")))
    assert agent_setup.main([]) == 0
    assert "agent-tools.json left as it was" in capsys.readouterr().out
    assert json.loads((support / "agent-tools.json").read_text()) == good
