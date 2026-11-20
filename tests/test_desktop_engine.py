"""Desktop engine row: the pure // engine-logic block inside desktop.html, run
under node (same style as test_screen_cursor.py). The block is iOS-parity logic
ported from EnginePickerRow.swift; it must stay free of DOM and page globals."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "gateway" / "static" / "desktop.html"


def logic_block() -> str:
    m = re.search(r"// engine-logic:start[^\n]*\n(.*?)\n// engine-logic:end", PAGE.read_text(), re.S)
    assert m, "engine-logic block not found in desktop.html"
    return m.group(1)


def test_logic_block_is_pure():
    block = logic_block()
    for dep in ("document", "window", "localStorage", "store", "api(", "$("):
        assert dep not in block, f"engine-logic block touches page state: {dep}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_engine_choice_fields_hubfirst_and_labels():
    up = "[{id:'hub',label:'Hub',kind:'hub',available:true}," \
         "{id:'glm',label:'GLM',available:false,reason:'endpoint offline'}," \
         "{id:'agy',label:'Antigravity',available:true}]"
    ok = "[{id:'hub',label:'Hub',kind:'hub',available:true}," \
         "{id:'glm',label:'GLM',available:true},{id:'agy',label:'Antigravity',available:true}]"
    prelude = f"var up = {up}; var ok = {ok};\n"
    checks = [
        "sendChoice('hub', ok)",                       # Hub: no engine key at all
        "sendChoice('glm', ok)",                       # available glm
        "sendChoice('glm', up)",                       # down glm with a reason
        "sendChoice('x', up)",                         # unknown id
        "engineFields('glm', ok, false)",              # plain pick
        "engineFields('glm', ok, true)",               # pick + new session
        "engineFields('hub', ok, true)",               # Hub + new session only
        "engineFields('hub', ok, false)",              # Hub, nothing set
        "engineFields('glm', up, true)",               # a down engine blocks the send
        "withHubFirst([{id:'glm'},{id:'agy'}]).map(o => o.id)",          # inserts Hub
        "withHubFirst([{id:'glm'},{id:'hub'},{id:'agy'}]).map(o => o.id)",  # moves Hub to front
        "withHubFirst(null).map(o => o.id)",           # empty list: Hub alone
        "['', 'claude', 'hub', 'glm', 'agy', 'zz'].map(e => sessionLabel(e, ok))",
        "sessionLabel('zz', [{id:'zz', label:'Zeta Engine'}])",          # list label wins
    ]
    script = (
        "const vm = require('vm');\n"
        "const ctx = vm.createContext({});\n"
        f"vm.runInContext({json.dumps(prelude + logic_block())}, ctx);\n"
        + "\n".join(
            f"console.log(JSON.stringify(vm.runInContext({json.dumps(c)}, ctx)));"
            for c in checks
        )
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    lines = out.stdout.strip().splitlines()
    assert len(lines) == len(checks), out.stderr
    (hub, glm, glm_down, unknown, f_plain, f_new, f_hub_new, f_plain_hub,
     f_blocked, ins, move, empty, labels, zeta) = (json.loads(x) for x in lines)
    assert hub == {}
    assert glm == {"engine": "glm"}
    assert glm_down == {"blocked": "GLM unavailable: endpoint offline. Pick another engine."}
    assert unknown == {"blocked": "x unavailable. Pick another engine."}
    assert f_plain == {"engine": "glm"}
    assert f_new == {"engine": "glm", "new_session": True}
    assert f_hub_new == {"new_session": True}
    assert f_plain_hub == {}
    assert f_blocked == glm_down  # same blocked note, toggle or not
    assert ins == ["hub", "glm", "agy"]
    assert move == ["hub", "glm", "agy"]
    assert empty == ["hub"]
    assert labels == ["Hub", "Hub", "Hub", "GLM", "Antigravity", "zz"]
    assert zeta == "Zeta Engine"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_local_only_labels_quickask_upstream():
    lst = "[{id:'hub',label:'Hub',kind:'hub',available:true}," \
          "{id:'local-qwen',label:'Qwen 27B · Local only',kind:'local',local_only:true,available:true}," \
          "{id:'local-down',label:'Qwen 27B · Local only',kind:'local',local_only:true,available:false,reason:'endpoint offline'}," \
          "{id:'glm',label:'GLM',available:true}," \
          "{id:'glm-down',label:'GLM',available:false,reason:'off'}]"
    prelude = f"var lst = {lst};\n"
    checks = [
        "optionLabel(lst[1])",     # local available: label already says Local only
        "optionLabel({id:'q', label:'Qwen 27B', local_only:true, available:true})",  # suffix added
        "optionLabel(lst[2])",     # local unavailable with a reason
        "optionLabel(lst[3])",     # non-local available unchanged
        "optionLabel(lst[4])",     # non-local unavailable with a reason
        "optionLabel({id:'g', label:'GLM', available:false})",                    # unavailable, no reason
        "sendChoice('local-down', lst)",   # a down local pick still blocks the send
        "sendChoice('local-qwen', lst)",   # an up local pick sends
        "usesQuickAsk('? hi', 'local-qwen', lst)",  # local pick: ask() even for "?"
        "usesQuickAsk('? hi', 'hub', lst)",
        "usesQuickAsk('hello', 'local-qwen', lst)",  # no "?": never quickAsk
        "usesQuickAsk('? hi', 'glm', lst)",
        "usesQuickAsk('', 'hub', lst)",
        "usesQuickAsk('? hi', 'local-qwen', [])",  # saved local pick missing from the list: fail closed
        "usesQuickAsk('? hi', 'zz', lst)",  # any unlisted pick: fail closed
        "upstreamNote('gaming-pc')",
        "upstreamNote('mini-pc')",
        "upstreamNote('unknown')",
        "upstreamNote(undefined)",
    ]
    script = (
        "const vm = require('vm');\n"
        "const ctx = vm.createContext({});\n"
        f"vm.runInContext({json.dumps(prelude + logic_block())}, ctx);\n"
        + "\n".join(
            f"console.log(JSON.stringify(vm.runInContext({json.dumps(c)}, ctx)));"
            for c in checks
        )
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    lines = out.stdout.strip().splitlines()
    assert len(lines) == len(checks), out.stderr
    (loc_ok, loc_suffix, loc_down, glm_ok, glm_down, glm_noreason,
     blocked, local_send, qa_local, qa_hub, qa_plain, qa_glm, qa_empty,
     qa_unlisted_local, qa_unlisted, up_gaming, up_mini, up_unknown, up_undef) = (json.loads(x) for x in lines)
    assert loc_ok == "Qwen 27B · Local only"
    assert loc_suffix == "Qwen 27B · Local only"
    assert loc_down == "Qwen 27B (unavailable)"  # closed control stays short; reason in tooltip
    assert glm_ok == "GLM"
    assert glm_down == "GLM (unavailable)"
    assert glm_noreason == "GLM (unavailable)"
    assert blocked == {"blocked": "Qwen 27B · Local only unavailable: endpoint offline. Pick another engine."}
    assert local_send == {"engine": "local-qwen"}
    assert qa_local is False
    assert qa_hub is True
    assert qa_plain is False
    assert qa_glm is True
    assert qa_empty is False
    assert qa_unlisted_local is False
    assert qa_unlisted is False
    assert up_gaming == {"label": "Gaming PC"}
    assert up_mini == {"label": "Mini PC", "hint": "Slower: running on the mini PC"}
    assert up_unknown is None
    assert up_undef is None
