"""Seed the overlay agent tier's MCP config from the owner's ~/.claude.json.

The agent tier is a `claude -p` process with `--setting-sources "" --strict-mcp-config`,
so it sees NO MCP servers unless we pass `--mcp-config <file>`. This module copies the
owner's mail/calendar MCP entries into two local 0600 files (never committed):
agent-mcp.json (entries verbatim) and agent-tools.json (tool names the agent may call
without asking). It only ever prints fixed lines: never server names, entry contents,
env values or tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

MCP_NAME = re.compile(r"mail|calendar", re.I)

# Casefolded tool-name prefixes that only read; anything containing a WRITE_HINT verb asks first.
READ_PREFIX = ("search", "list", "get", "read", "find", "fetch", "query", "check")
WRITE_HINT = ("send", "reply", "forward", "draft", "delete", "remove", "trash", "archive",
              "label", "move", "modify", "update", "create", "write", "set", "add", "mark",
              "accept", "decline", "respond")


def select_servers(claude_cfg: dict, home: str) -> dict:
    """Mail/calendar MCP entries from ~/.claude.json: project-level merged in, top-level wins."""
    merged = dict(claude_cfg.get("projects", {}).get(home, {}).get("mcpServers", {}))
    merged.update(claude_cfg.get("mcpServers", {}))
    return {name: entry for name, entry in merged.items()
            if isinstance(entry, dict) and MCP_NAME.search(name)}


def read_only_tools(tool_names: list[str], servers: dict) -> list[str]:
    """`mcp__<server>__<tool>` names whose tool reads (never writes), sorted and deduped."""
    keep: set[str] = set()
    for name in tool_names:
        parts = name.split("__")
        if len(parts) != 3 or parts[0] != "mcp" or parts[1] not in servers:
            continue
        tool = parts[2].casefold()
        if tool.startswith(READ_PREFIX) and not any(hint in tool for hint in WRITE_HINT):
            keep.add(name)
    return sorted(keep)


def tool_names_from_init(lines: Iterable[str]) -> list[str]:
    """The `tools` list of the first stream-json system/init event; bad lines skipped."""
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
            tools = ev.get("tools")
            return [t for t in tools if isinstance(t, str)] if isinstance(tools, list) else []
    return []


def write_private(path: Path, data: dict) -> None:
    """Write JSON owner-only: 0700 dir, 0600 tmp + rename, 0600 final."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="seed the overlay agent tier's MCP config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    support = Path(os.environ.get("OVERLAY_SUPPORT_DIR")
                   or Path.home() / "Library/Application Support/JarvisOverlay")
    mcp_path = support / "agent-mcp.json"
    if mcp_path.exists() and not args.force:
        print("agent MCP: kept your existing agent-mcp.json")  # never clobber owner edits
        return 0
    if args.dry_run:  # never even read ~/.claude.json in a dry run (CI, other machines)
        print("would: copy mail/calendar MCP servers from ~/.claude.json into agent-mcp.json (0600)")
        print("would: list their tools with claude and write agent-tools.json")
        return 0
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    servers = select_servers(cfg, str(Path.home()))
    if not servers:
        print("agent MCP: no mail or calendar MCP servers found; the agent gets git, GitHub and files only")
        return 0
    write_private(mcp_path, {"mcpServers": servers})
    try:
        stdin_line = json.dumps({"type": "user", "message": {"role": "user",
                                "content": [{"type": "text", "text": "Reply with OK."}]}}) + "\n"
        # Empty scratch cwd: no project CLAUDE.md / .mcp.json / hooks get picked up (same as lean.py).
        proc = subprocess.run(
            ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--model", "haiku", "--tools", "", "--setting-sources", "",
             "--strict-mcp-config", "--mcp-config", str(mcp_path),
             "--permission-mode", "default", "--no-session-persistence"],
            input=stdin_line, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            cwd=tempfile.mkdtemp(prefix="jov-agent-"),
            env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"},
            timeout=90)
        tool_names = tool_names_from_init(proc.stdout.splitlines())
    except (OSError, subprocess.SubprocessError):
        write_private(support / "agent-tools.json", {"read_only": []})
        print("agent MCP: could not list tools; every mail/calendar tool will ask you first")
        return 0
    read_only = read_only_tools(tool_names, servers)
    write_private(support / "agent-tools.json", {"read_only": read_only})
    print(f"agent MCP: {len(servers)} server(s), {len(read_only)} read-only tool(s); "
          "every other mail/calendar tool asks you first")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
