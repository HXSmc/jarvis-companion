"""j3d-anim criteria 17-19 (the 3D object card page): a GLB with clips plays them in the page
(AnimationMixer), the Play/Pause button and Space on the stage toggle it, reduced motion starts
paused, and the camera frames the box the whole motion sweeps. All GLBs are synthetic (struct,
like the jfollow10d cube); pages load in headless Chromium over 127.0.0.1 from a scratch copy
of web/ with the app's CSP meta dropped, exactly as the jfollow10d shadow test does."""
from __future__ import annotations

import functools
import json
import re
import shutil
import struct
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

from test_overlay_jfollow10d import _chrome, _cube_glb  # noqa: E402


def _cube_parts():
    # Same 1 m cube resting on y=0 as _cube_glb(): per-face normals, winding-correct indices.
    pos, nor, idx = [], [], []
    for axis in range(3):
        for sign in (1, -1):
            n = [0.0, 0.0, 0.0]
            n[axis] = float(sign)
            u, v = [a for a in range(3) if a != axis]
            base = len(pos)
            for du, dv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                p = [0.0, 0.0, 0.0]
                p[axis], p[u], p[v] = 0.5 * sign, 0.5 * du, 0.5 * dv
                pos.append((p[0], p[1] + 0.5, p[2]))
                nor.append(tuple(n))
            e1 = [pos[base + 1][i] - pos[base][i] for i in range(3)]
            e2 = [pos[base + 2][i] - pos[base][i] for i in range(3)]
            cross = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
            out = sum(cross[i] * n[i] for i in range(3)) > 0
            if out:
                idx += [base, base + 1, base + 2, base, base + 2, base + 3]
            else:
                idx += [base, base + 2, base + 1, base, base + 3, base + 2]
    pb = b"".join(struct.pack("<3f", *q) for q in pos)
    nb = b"".join(struct.pack("<3f", *q) for q in nor)
    ib = b"".join(struct.pack("<H", i) for i in idx)
    return pb, nb, ib


def _anim_glb(moves, color=(1.0, 0.0, 0.0, 1.0)):
    """One node per entry of moves; each entry is a list of (time, (x, y, z)) translation keys
    (LINEAR), or None for a still node. Every moving node gets its own 1-node animation clip
    in the BIN chunk (input times SCALAR float, output VEC3 translation)."""
    pb, nb, ib = _cube_parts()
    views = [
        {"buffer": 0, "byteOffset": 0, "byteLength": len(pb)},
        {"buffer": 0, "byteOffset": len(pb), "byteLength": len(nb)},
        {"buffer": 0, "byteOffset": len(pb) + len(nb), "byteLength": len(ib)},
    ]
    accessors = [
        {"bufferView": 0, "componentType": 5126, "count": 24, "type": "VEC3", "min": [-0.5, 0, -0.5], "max": [0.5, 1, 0.5]},
        {"bufferView": 1, "componentType": 5126, "count": 24, "type": "VEC3"},
        {"bufferView": 2, "componentType": 5123, "count": 36, "type": "SCALAR"},
    ]
    binb = pb + nb + ib
    nodes, animations = [], []
    for keys in moves:
        node = len(nodes)
        nodes.append({"mesh": 0})
        if not keys:
            continue
        times = b"".join(struct.pack("<f", t) for t, _ in keys)
        vals = b"".join(struct.pack("<3f", *v) for _, v in keys)
        views.append({"buffer": 0, "byteOffset": len(binb), "byteLength": len(times)})
        in_acc = len(accessors)
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126, "count": len(keys), "type": "SCALAR",
                          "min": [keys[0][0]], "max": [keys[-1][0]]})
        views.append({"buffer": 0, "byteOffset": len(binb) + len(times), "byteLength": len(vals)})
        out_acc = len(accessors)
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126, "count": len(keys), "type": "VEC3"})
        binb += times + vals
        animations.append({
            "channels": [{"sampler": 0, "target": {"node": node, "path": "translation"}}],
            "samplers": [{"input": in_acc, "output": out_acc, "interpolation": "LINEAR"}],
        })
    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1}, "indices": 2, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": list(color), "metallicFactor": 0}}],
        "animations": animations,
        "buffers": [{"byteLength": len(binb)}],
        "bufferViews": views,
        "accessors": accessors,
    }
    j = json.dumps(doc).encode()
    j += b" " * (-len(j) % 4)
    body = struct.pack("<I4s", len(j), b"JSON") + j + struct.pack("<I4s", len(binb), b"BIN\0") + binb
    return b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body


def _run_page(work: Path, glb: bytes, flags=(), shot: Path | None = None, dump: bool = False) -> str:
    """Serve a scratch copy of web/ (CSP meta dropped, three.core.min.js aliased) with the given
    object.glb, load model.html in headless Chromium, return the dumped DOM."""
    chrome = _chrome()
    if chrome is None:
        pytest.skip("no Chromium on this box (set CHROME_PATH)")
    site = work / "web"
    shutil.copytree(ROOT / "mac" / "overlay" / "Resources" / "web", site)
    build = site / "vendor" / "three" / "build"
    shutil.copy(build / "three.core.min.js", build / "three.core.js")
    page = site / "model.html"
    page.write_text("\n".join(x for x in page.read_text().splitlines() if "Content-Security-Policy" not in x))
    (site / "data.json").write_text(json.dumps({"slug": "test-cube", "view": "model"}))
    (site / "object.glb").write_bytes(glb)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    args = [
        chrome, "--no-sandbox", "--headless=new", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
        "--virtual-time-budget=4000", "--window-size=360,300", f"--user-data-dir={work / 'profile'}",
    ]
    if shot is not None:
        args.append(f"--screenshot={shot}")
    if dump:
        args.append("--dump-dom")
    args += list(flags)
    args.append(f"http://127.0.0.1:{server.server_port}/model.html")
    try:
        r = subprocess.run(args, capture_output=True, timeout=90, check=False)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"Chromium never reached the end of its virtual-time budget: {e}")
    finally:
        server.shutdown()
        server.server_close()
    return r.stdout.decode("utf-8", "replace")


def _play_button(dom: str) -> tuple[str, str]:
    m = re.search(r'<button id="play"([^>]*)>([^<]*)</button>', dom)
    assert m, "no #play button in the dumped DOM"
    return m.group(1), m.group(2)


def test_page_plays_clip(tmp_path):
    two_clips = _anim_glb([[(0, (0, 0, 0)), (1, (0, 1, 0))], [(0, (0, 0, -2)), (1, (0, 1.5, -2))]])
    dom = _run_page(tmp_path, two_clips, dump=True)
    assert 'data-anim="playing"' in dom, dom[:600]
    assert 'data-clips="2"' in dom, dom[:600]
    attrs, text = _play_button(dom)
    assert "hidden" not in attrs
    assert text == "Pause"
    assert 'aria-pressed="true"' in attrs

    still = _run_page(tmp_path / "reduced", two_clips, flags=["--force-prefers-reduced-motion"], dump=True)
    assert 'data-anim="paused"' in still, still[:600]
    attrs, text = _play_button(still)
    assert text == "Play"
    assert 'aria-pressed="false"' in attrs

    static = _run_page(tmp_path / "static", _cube_glb(), dump=True)
    assert 'data-anim="none"' in static, static[:600]
    assert 'data-clips="0"' in static, static[:600]
    assert "hidden" in _play_button(static)[0]


def test_page_motion_framed(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    results = {}
    for name, keys in (("down", [(0, (0, 3, 0)), (1, (0, 1, 0))]),  # t=0 high, falls 2 m in 1 s
                       ("up", [(0, (0, 1, 0)), (1, (0, 3, 0))])):   # the reverse
        shot = tmp_path / f"{name}.png"
        _run_page(tmp_path / name, _anim_glb([keys]), flags=["--force-prefers-reduced-motion"], shot=shot)
        im = Image.open(shot).convert("RGB")
        red = []
        for y in range(230):  # the stage area: the panel below it is y >= 230
            for x in range(im.width):
                r, g, b = im.getpixel((x, y))
                if r > 120 and r > 2 * g and r > 2 * b:
                    red.append((x, y))
        assert len(red) > 300, (name, len(red))
        xs = [x for x, _ in red]
        ys = [y for _, y in red]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        assert bbox[0] > 0 and bbox[1] > 0 and bbox[2] < im.width - 1 and bbox[3] < 229, (name, bbox)
        results[name] = (sum(ys) / len(ys), bbox)
    print(f"centroids+bboxes: {results}")
    assert results["down"][0] < 0.45 * 230, results  # starts high: centroid in the top 45%
    assert results["up"][0] > 0.45 * 230, results    # starts low: centroid in the bottom 55%


def test_play_button_style():
    css = (ROOT / "mac" / "overlay" / "Resources" / "web" / "model.css").read_text()
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    heights = [(s, b) for s, b in rules if "min-height: 44px" in b]
    assert len(heights) == 1, heights
    assert "#play" in heights[0][0] and "#reset" in heights[0][0]
    focus = [(s, b) for s, b in rules if ":focus-visible" in s and "outline" in b]
    assert any("#play" in s and "#reset" in s for s, _ in focus), focus
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "#play" in reduced and "transition: none" in reduced
