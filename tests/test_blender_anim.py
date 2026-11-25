"""j3d-h2: the animated runner (physics bake + keyframes), the wrapper's ffmpeg unit and the
real-Blender round trip. Linux only. The fake-bpy tests pin the runner's calls; the wrapper tests
use W's box fixture with their own stub sudo that runs the real /usr/bin/ffmpeg for the second
unit; the real-Blender test runs the true pipeline in a tmp HOME with the D3 ffmpeg argv."""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import runpy
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))
from test_blender_job import box, job, run, calls, _run_runner, _Translate  # noqa: E402

RUNNER = ROOT / "scripts" / "blender_job_runner.py"
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="mini PC wrapper: Linux only")

REAL_HOME = os.path.realpath(os.path.expanduser("~"))
REAL_BLENDER = os.path.join(REAL_HOME, ".local", "bin", "blender")
HAS_FFMPEG = os.path.exists("/usr/bin/ffmpeg")

# A's own sudo stub: the Blender unit writes outputs (and frames for the fs_* and failure modes);
# the ffmpeg unit runs the real /usr/bin/ffmpeg with the checked argv, or fails on ffmpeg_fail.
A_STUB_SUDO = r'''#!/usr/bin/env python3
import json, os, struct, sys, zlib
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("STUB_MODE", "ok")
argv = sys.argv[1:]
job = argv[-1]

def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

def frame_png():
    raw = b"".join(b"\x00" + bytes((200, 40, 40)) * 8 for _ in range(8))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

if "/usr/bin/ffmpeg" in argv:  # the second unit: the real encoder, argv fixed after the executable
    if mode == "ffmpeg_fail":
        sys.exit(1)
    os.execv("/usr/bin/ffmpeg", ["/usr/bin/ffmpeg"] + argv[argv.index("/usr/bin/ffmpeg") + 1:])

glb = b"glTF" + struct.pack("<II", 2, 20) + b"\0" * 8
png = b"\x89PNG\r\n\x1a\n" + b"\0" * 24
if mode in ("ok", "ffmpeg_fail", "timeout", "oom-kill", "badmagic", "nopng",
            "fs_link", "fs_hard", "fs_gap", "fs_extra", "fs_many", "fs_big", "fs_badmagic", "fs_dirlink",
            "mp4link"):
    fr = os.path.join(job, "frames")
    os.makedirs(fr, exist_ok=True)
    for i in range(1, 25):
        open(os.path.join(fr, "f_%04d.png" % i), "wb").write(frame_png())
if mode == "fs_link":  # a frame that is a link to the canary
    os.unlink(os.path.join(job, "frames", "f_0001.png"))
    os.symlink(os.environ["STUB_SECRET"], os.path.join(job, "frames", "f_0001.png"))
elif mode == "fs_hard":  # a frame that is a second link to the canary
    os.unlink(os.path.join(job, "frames", "f_0001.png"))
    os.link(os.environ["STUB_SECRET"], os.path.join(job, "frames", "f_0001.png"))
elif mode == "fs_gap":  # a hole in the sequence
    os.unlink(os.path.join(job, "frames", "f_0002.png"))
elif mode == "fs_extra":  # an unexpected entry next to the frames
    open(os.path.join(job, "frames", "x.txt"), "wb").write(b"x" * 16)
elif mode == "fs_many":  # more than 240 frames
    for i in range(25, 242):
        open(os.path.join(job, "frames", "f_%04d.png" % i), "wb").write(frame_png())
elif mode == "fs_big":  # a frame over the 2 MB cap, PNG magic intact
    with open(os.path.join(job, "frames", "f_0002.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * (3 * 1024 * 1024))
elif mode == "fs_badmagic":  # a frame that is not a PNG
    open(os.path.join(job, "frames", "f_0002.png"), "wb").write(b"JUNKJUNKJUNK")
elif mode == "mp4link":  # the job leaves object.mp4 as a link to the canary
    os.symlink(os.environ["STUB_SECRET"], os.path.join(job, "object.mp4"))
elif mode == "fs_dirlink":  # the frames dir itself is a link to a dir holding a link to the canary
    os.rename(os.path.join(job, "frames"), job + "-frames-target")
    os.link(os.environ["STUB_SECRET"], os.path.join(job + "-frames-target", "canary-copy"))
    os.symlink(job + "-frames-target", os.path.join(job, "frames"))
if mode == "badmagic":
    open(os.path.join(job, "object.glb"), "wb").write(b"PK\x03\x04" + glb[4:])
else:
    open(os.path.join(job, "object.glb"), "wb").write(glb)
if mode != "nopng":
    open(os.path.join(job, "preview.png"), "wb").write(png)
if mode in ("timeout", "oom-kill"):
    print("Running as unit: run-u1.service\nFinished with result: " + mode); sys.exit(1)
print("Finished with result: success")
'''


@pytest.fixture
def abox(box):
    (box["stubs"] / "sudo").write_text(A_STUB_SUDO)
    (box["stubs"] / "sudo").chmod(0o755)
    return box


BOX = [(-1, -1, -1), (1, -1, -1), (1, 1, 1), (-1, 1, 1)]


def _mesh_obj(**extra):
    fields = dict(type="MESH", bound_box=BOX, matrix_world=_Translate((0, 0, 0)),
                  animation_data=None, data=None, rigid_body=None)
    fields.update(extra)
    return SimpleNamespace(**fields)


def test_runner_animated_with_fake_bpy(tmp_path):
    # keyframed: one object with animation_data.action, frame_end 400 -> clamped to 240 frames
    d = tmp_path / "anim"
    d.mkdir()
    (d / "object.py").write_text("pass\n")
    bpy = mock.MagicMock()
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 400
    scene.rigidbody_world = None
    scene.objects = [_mesh_obj(animation_data=SimpleNamespace(action=object()))]
    rendered = []
    bpy.ops.render.render.side_effect = lambda write_still=False: rendered.append(
        (bpy.context.scene.render.filepath, write_still, scene.frame_set.call_args))
    assert _run_runner(d, bpy) == 0
    assert scene.frame_end == scene.frame_start + 239
    assert scene.render.fps == 24 and scene.render.fps_base == 1.0
    assert bpy.ops.export_scene.gltf.call_args.kwargs == dict(
        filepath=str(d / "object.glb"), export_format="GLB", export_draco_mesh_compression_enable=False,
        export_apply=True, export_cameras=False, export_lights=False, export_yup=True,
        export_animations=True, export_frame_range=True, export_force_sampling=True,
        export_animation_mode="ACTIONS")
    assert rendered[0][:2] == (str(d / "preview.png"), True)
    assert [r[0] for r in rendered[1:]] == [str(d / "frames" / f"f_{i:04d}.png") for i in range(1, 241)]
    assert all(r[1] is True for r in rendered[1:])
    assert [r[2] for r in rendered[1:]] == [mock.call(i) for i in range(1, 241)]  # each frame file shows its frame
    # 33 ACTIVE bodies: refused before any render or export
    d = tmp_path / "toomany"
    d.mkdir()
    (d / "object.py").write_text("pass\n")
    bpy = mock.MagicMock()
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 48
    scene.rigidbody_world = SimpleNamespace(point_cache=SimpleNamespace())
    scene.objects = [_mesh_obj(rigid_body=SimpleNamespace(type="ACTIVE")) for _ in range(33)]
    assert _run_runner(d, bpy) not in (0, None)
    assert "max 32" in (d / "error.txt").read_text()
    assert not bpy.ops.export_scene.gltf.called and not bpy.ops.render.render.called
    # frame_start != 1: refused
    d = tmp_path / "late-start"
    d.mkdir()
    (d / "object.py").write_text("pass\n")
    bpy = mock.MagicMock()
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 100, 148
    scene.rigidbody_world = None
    scene.objects = [_mesh_obj(animation_data=SimpleNamespace(action=object()))]
    assert _run_runner(d, bpy) not in (0, None)
    assert "frame_start must be 1" in (d / "error.txt").read_text()
    assert not bpy.ops.export_scene.gltf.called


@pytest.mark.skipif(not HAS_FFMPEG, reason="/usr/bin/ffmpeg absent")
def test_wrapper_ffmpeg_line(abox):
    d = job(abox)
    code, out, _err = run(abox, str(d), STUB_MODE="ok")
    assert code == 0, out
    res = json.loads(out)
    assert res["ok"] is True and res["glb_bytes"] == 20 and res["png_bytes"] == 32 and res["ms"] >= 0
    assert res["mp4_bytes"] > 0
    data = (d / "object.mp4").read_bytes()
    assert data[4:8] == b"ftyp" and len(data) > 1024
    assert not (d / "frames").exists()
    two = calls(abox)
    assert len(two) == 2, two
    user = getpass.getuser()
    b, r = str(abox["blender"]), str(abox["bin"])
    want1 = ["-n", "systemd-run", f"--uid={user}", f"--gid={user}", "--wait", "--pipe", "--collect",
             "-p", "PrivateNetwork=yes", "-p", "ProtectHome=tmpfs", "-p", "ProtectSystem=strict",
             "-p", f"BindPaths={d}", "-p", f"BindReadOnlyPaths={b}", "-p", f"BindReadOnlyPaths={r}",
             "-p", "PrivateTmp=yes", "-p", "NoNewPrivileges=yes", "-p", "MemoryMax=4G", "-p", "TasksMax=64",
             "-p", "RuntimeMaxSec=120", "-p", "CapabilityBoundingSet=", "-p", "RestrictAddressFamilies=AF_UNIX",
             "-p", "PrivateUsers=yes", "-p", "TemporaryFileSystem=/run:ro",
             f"{b}/blender", "-b", "--factory-startup", "--python-exit-code", "1", "--python",
             f"{r}/blender_job_runner.py", "--", str(d)]
    want2 = ["-n", "systemd-run", f"--uid={user}", f"--gid={user}", "--wait", "--pipe", "--collect",
             "-p", "PrivateNetwork=yes", "-p", "ProtectHome=tmpfs", "-p", "ProtectSystem=strict",
             "-p", f"BindPaths={d}",
             "-p", "PrivateTmp=yes", "-p", "NoNewPrivileges=yes", "-p", "MemoryMax=4G", "-p", "TasksMax=64",
             "-p", "RuntimeMaxSec=60", "-p", "CapabilityBoundingSet=", "-p", "RestrictAddressFamilies=AF_UNIX",
             "-p", "PrivateUsers=yes", "-p", "TemporaryFileSystem=/run:ro",
             "/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file",
             "-f", "image2", "-framerate", "24", "-start_number", "1", "-i", f"{d}/frames/f_%04d.png",
             "-frames:v", "24", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", "-n", f"{d}/object.mp4"]
    assert two[0] == want1, two[0]
    assert two[1] == want2, two[1]


def test_wrapper_frames_refused(abox):
    sha = hashlib.sha256(abox["secret"].read_bytes()).hexdigest()
    cases = ["fs_link", "fs_hard", "fs_gap", "fs_extra", "fs_many", "fs_big", "fs_badmagic", "fs_dirlink"]
    for i, mode in enumerate(cases):
        d = job(abox, f"fr-{i}")
        before = len(calls(abox))
        code, out, _err = run(abox, str(d), STUB_MODE=mode)
        res = json.loads(out)
        assert code == 0 and res["ok"] is True and res["mp4"] == "bad_frames", (mode, out)
        assert (d / "object.glb").exists() and not (d / "object.mp4").exists()
        assert len(calls(abox)) == before + 1, (mode, calls(abox)[before:])  # the ffmpeg unit never ran
        assert "SECRET" not in out and "canary" not in out
        assert not (d / "frames").exists(), mode
    # the job left object.mp4 as a link: no encode, the link goes, the canary is untouched
    d = job(abox, "mp4link")
    before = len(calls(abox))
    code, out, _err = run(abox, str(d), STUB_MODE="mp4link")
    assert code == 0 and json.loads(out)["mp4"] == "bad_mp4", out
    assert len(calls(abox)) == before + 1 and not (d / "object.mp4").is_symlink() and not (d / "frames").exists()
    assert hashlib.sha256(abox["secret"].read_bytes()).hexdigest() == sha


@pytest.mark.skipif(not HAS_FFMPEG, reason="/usr/bin/ffmpeg absent")
def test_wrapper_mp4_optional(abox):
    # no_ffmpeg: a copy of the wrapper whose fixed encoder path points at a missing file
    d = job(abox, "noffmpeg")
    copy = abox["bin"] / "blender-job-noffmpeg.sh"
    copy.write_text((abox["bin"] / "blender-job.sh").read_text()
                    .replace("/usr/bin/ffmpeg", str(abox["tmp"] / "no-ffmpeg")))
    copy.chmod(0o755)
    r = subprocess.run(["bash", str(copy), str(d)], env={**abox["env"], "STUB_MODE": "ok"},
                       capture_output=True, text=True, timeout=60)
    res = json.loads(r.stdout)
    assert r.returncode == 0 and res["ok"] is True and res["mp4"] == "no_ffmpeg", r.stdout
    assert len(calls(abox)) == 1 and not (d / "object.mp4").exists() and not (d / "frames").exists()
    copy.unlink()
    # ffmpeg_failed: the encode unit exits 1, the object itself still succeeded
    d = job(abox, "ffail")
    before = len(calls(abox))  # the log is cumulative across the runs above
    code, out, _err = run(abox, str(d), STUB_MODE="ffmpeg_fail")
    res = json.loads(out)
    assert code == 0 and res["ok"] is True and res["mp4"] == "ffmpeg_failed", out
    assert not (d / "object.mp4").exists() and not (d / "frames").exists()
    assert len(calls(abox)) == before + 2
    # a planted object.mp4 is stale output: refused before anything runs
    d = job(abox, "planted")
    (d / "object.mp4").write_bytes(b"ftyp junk")
    code, out, _err = run(abox, str(d), STUB_MODE="ok")
    assert code == 2 and json.loads(out) == {"ok": False, "reason": "stale_output"}
    # old failure modes with frames: the reason stays and frames never outlive the run
    for mode, reason in (("timeout", "timeout"), ("badmagic", "bad_glb"), ("nopng", "no_png")):
        d = job(abox, f"old-{mode}")
        code, out, _err = run(abox, str(d), STUB_MODE=mode)
        assert code == 1 and json.loads(out) == {"ok": False, "reason": reason}, (mode, out)
        assert not (d / "frames").exists(), mode
        for p in ("object.glb", "preview.png"):  # the skill's step 4: delete the outputs, run again
            (d / p).unlink(missing_ok=True)
        code, out, _err = run(abox, str(d), STUB_MODE=mode)
        assert json.loads(out) == {"ok": False, "reason": reason}, (mode, out)
    # a static job: exactly the four keys, one sudo call
    d = job(abox, "static")
    before = len(calls(abox))
    code, out, _err = run(abox, str(d), STUB_MODE="static")
    res = json.loads(out)
    assert code == 0 and list(res) == ["ok", "glb_bytes", "png_bytes", "ms"], out
    assert len(calls(abox)) == before + 1


BALL_PY = '''import bpy
import bmesh
import math

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 48

def material(name, rgb):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.5
    return m

def mesh_object(name, build):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    build(bm)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    scene.collection.objects.link(ob)
    return ob

floor = mesh_object("floor", lambda bm: bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.0))
floor.data.materials.append(material("grey", (0.5, 0.5, 0.5)))
ball = mesh_object("ball", lambda bm: bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=12, radius=0.15))
ball.data.materials.append(material("red", (0.8, 0.05, 0.05)))
for f in ball.data.polygons:
    f.use_smooth = True
for frame in range(1, 49):
    t = (frame - 1) / 47  # 0..1, one bounce: last pose = first pose
    ball.location = (0.0, 0.0, 0.15 + 0.8 * math.sin(math.pi * t))
    ball.keyframe_insert("location", frame=frame)
'''

BOXES_PY = '''import bpy
import bmesh

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 72

def box(name, size, loc, rgb):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=size, verts=bm.verts)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    scene.collection.objects.link(ob)
    ob.location = loc
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    ob.data.materials.append(m)
    return ob

with bpy.context.temp_override(scene=scene):
    assert bpy.ops.rigidbody.world_add() == {"FINISHED"}
parts = [(box("table", (1.2, 0.8, 0.1), (0.0, 0.0, 0.7), (0.45, 0.3, 0.2)), "PASSIVE")]
for i, x in enumerate((-0.3, 0.0, 0.3)):
    parts.append((box(f"box_{i + 1}", (0.2, 0.2, 0.2), (x, 0.05 * i, 1.4 + 0.35 * i), (0.2, 0.4, 0.8)), "ACTIVE"))
for ob, kind in parts:
    with bpy.context.temp_override(object=ob, active_object=ob, selected_objects=[ob]):
        assert bpy.ops.rigidbody.object_add(type=kind) == {"FINISHED"}
'''

CUBE_PY = '''import bpy
import bmesh

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
me = bpy.data.meshes.new("cube")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=0.3)
bmesh.ops.bevel(bm, geom=bm.verts[:] + bm.edges[:], offset=0.02, segments=3, affect="EDGES")
bm.to_mesh(me)
bm.free()
ob = bpy.data.objects.new("cube", me)
bpy.context.scene.collection.objects.link(ob)
'''


def _glb_json_bin(path):
    data = path.read_bytes()
    assert data[:4] == b"glTF" and data[16:20] == b"JSON"
    jlen = struct.unpack("<I", data[12:16])[0]
    doc = json.loads(data[20:20 + jlen])
    off = 20 + jlen + (-jlen % 4)
    blen = struct.unpack("<I", data[off:off + 4])[0]
    assert data[off + 4:off + 8] == b"BIN\x00"
    return doc, data[off + 8:off + 8 + blen]


def _vec3s(jdoc, bins, acc):
    bv = jdoc["bufferViews"][acc["bufferView"]]
    off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    return [struct.unpack_from("<3f", bins, off + 12 * i) for i in range(acc["count"])]


@pytest.mark.skipif(not (os.path.exists(REAL_BLENDER) and HAS_FFMPEG), reason="real Blender or ffmpeg absent")
def test_real_blender_jobs(tmp_path):
    from mac.voice import models3d
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    out = {}
    for name, script in (("ball", BALL_PY), ("boxes", BOXES_PY), ("cube", CUBE_PY)):
        d = tmp_path / name
        d.mkdir()
        (d / "object.py").write_text(script)
        r = subprocess.run([REAL_BLENDER, "-b", "--factory-startup", "--python-exit-code", "1",
                            "--python", str(RUNNER), "--", str(d)],
                           env=env, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, (name, r.stdout[-2000:], r.stderr[-2000:])
        glb, png, frames = d / "object.glb", d / "preview.png", d / "frames"
        assert glb.exists() and png.exists(), name
        with open(glb, "rb") as f:  # the same check the Mac side runs on every upload
            models3d._check_glb(f.fileno(), glb.stat().st_size)
        jdoc, bins = _glb_json_bin(glb)
        clips = jdoc.get("animations") or []
        if name == "cube":
            assert not frames.exists() and "animations" not in jdoc, name
            print(f"cube: clips=0 glb={glb.stat().st_size} png={png.stat().st_size}")
            continue
        n = len(list(frames.glob("f_*.png")))
        assert 2 <= n <= 240, (name, n)
        Image = pytest.importorskip("PIL.Image")  # pixels, not bytes: Blender stamps frame metadata in each PNG
        first, mid = (Image.open(frames / f).convert("RGB").tobytes() for f in ("f_0001.png", "f_0024.png"))
        assert first != mid, name  # each frame file shows its own pose
        argv = ["/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist",
                "file", "-f", "image2", "-framerate", "24", "-start_number", "1", "-i", f"{frames}/f_%04d.png",
                "-frames:v", str(n), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", "-n", f"{d}/object.mp4"]
        r2 = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        assert r2.returncode == 0, (name, r2.stderr[-2000:])
        mp4 = d / "object.mp4"
        assert mp4.stat().st_size > 1024 and mp4.read_bytes()[4:8] == b"ftyp", name
        out[name] = (clips, jdoc, bins)
        print(f"{name}: clips={len(clips)} frames={n} glb={glb.stat().st_size} "
              f"png={png.stat().st_size} mp4={mp4.stat().st_size}")
    assert len(out["ball"][0]) >= 1, out["ball"][0]
    assert len(out["boxes"][0]) >= 3, out["boxes"][0]
    jdoc, bins = out["boxes"][1], out["boxes"][2]
    boxes = {i for i, node in enumerate(jdoc["nodes"]) if node.get("name", "").startswith("box_")}
    assert len(boxes) == 3, jdoc["nodes"]
    checked = 0
    for anim in jdoc["animations"]:
        for ch in anim["channels"]:
            if ch["target"]["path"] == "translation" and ch["target"]["node"] in boxes:
                vals = _vec3s(jdoc, bins, jdoc["accessors"][anim["samplers"][ch["sampler"]]["output"]])
                assert vals[-1][1] < vals[0][1], (ch["target"]["node"], vals[0], vals[-1])
                checked += 1
    assert checked >= 3, checked
