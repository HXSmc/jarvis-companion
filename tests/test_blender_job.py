"""jfollow10d: the mini PC Blender sandbox wrapper, its fixed runner and the skill installer.

Linux only. HOME is a tmp dir; `sudo` (and `id` for the uid-0 case) are stubs first on a pinned PATH,
so no test can start a real unit or run Blender. The stub records its argv and writes the outputs."""
from __future__ import annotations

import getpass
import json
import os
import runpy
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "scripts" / "blender-job.sh"
RUNNER = ROOT / "scripts" / "blender_job_runner.py"
INSTALLER = ROOT / "scripts" / "install-blender-object.sh"
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="mini PC wrapper: Linux only")

STUB_SUDO = r'''#!/usr/bin/env python3
import json, os, struct, sys
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("STUB_MODE", "ok")
job = sys.argv[-1]
glb = b"glTF" + struct.pack("<II", 2, 20) + b"\0" * 8
png = b"\x89PNG\r\n\x1a\n" + b"\0" * 24
if mode == "sudo_fail":
    print("sudo: a password is required"); sys.exit(1)
if mode in ("timeout", "oom-kill"):
    print("Running as unit: run-u1.service\nFinished with result: " + mode); sys.exit(1)
if mode == "symlink":
    os.symlink(os.environ["STUB_SECRET"], os.path.join(job, "object.glb"))
elif mode == "hardlink":
    os.link(os.environ["STUB_SECRET"], os.path.join(job, "object.glb"))
elif mode == "big":
    with open(os.path.join(job, "object.glb"), "wb") as f:
        f.write(b"glTF" + struct.pack("<II", 2, 51 * 1024 * 1024)); f.truncate(51 * 1024 * 1024)
elif mode == "badmagic":
    open(os.path.join(job, "object.glb"), "wb").write(b"PK\x03\x04" + glb[4:])
else:
    open(os.path.join(job, "object.glb"), "wb").write(glb)
if mode != "nopng":
    open(os.path.join(job, "preview.png"), "wb").write(png)
print("Finished with result: success")
'''


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    blender_dir = home / ".local" / "opt" / "blender-5.2.3"
    blender_dir.mkdir(parents=True)
    (blender_dir / "blender").write_text("#!/bin/sh\nexit 0\n")
    (blender_dir / "blender").chmod(0o755)
    bin_dir = home / ".local" / "share" / "jarvis-blender" / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(WRAPPER, bin_dir / "blender-job.sh")
    (bin_dir / "blender-job.sh").chmod(0o755)
    shutil.copy(RUNNER, bin_dir / "blender_job_runner.py")
    jobs = home / ".local" / "share" / "jarvis-blender" / "jobs"
    jobs.mkdir(mode=0o700)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "sudo").write_text(STUB_SUDO)
    (stubs / "sudo").chmod(0o755)
    secret = tmp_path / "secret.txt"
    secret.write_text("canary-SECRET-bytes")
    env = {"HOME": str(home), "PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(tmp_path / "calls.log"),
           "STUB_SECRET": str(secret), "LANG": "C"}
    found = subprocess.run(["bash", "-c", "command -v sudo"], env=env, capture_output=True, text=True).stdout.strip()
    assert found == str(stubs / "sudo")  # never the real sudo
    return {"home": home, "blender": blender_dir, "bin": bin_dir, "jobs": jobs, "stubs": stubs, "env": env,
            "log": tmp_path / "calls.log", "secret": secret, "tmp": tmp_path}


def job(box, name="pawn-123456", script="import bpy\n"):
    d = box["jobs"] / name
    d.mkdir(mode=0o700)
    (d / "object.py").write_text(script)
    return d


def run(box, *args, **env):
    r = subprocess.run(["bash", str(box["bin"] / "blender-job.sh"), *args], env={**box["env"], **env},
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout, r.stderr


def calls(box):
    return [json.loads(x) for x in box["log"].read_text().splitlines()] if box["log"].exists() else []


def test_wrapper_exact_sudo_line(box):
    d = job(box)
    code, out, _err = run(box, str(d))
    assert code == 0, out
    res = json.loads(out)
    assert res["ok"] is True and res["glb_bytes"] == 20 and res["png_bytes"] == 32 and res["ms"] >= 0
    user = getpass.getuser()
    b, r = str(box["blender"]), str(box["bin"])
    want = ["-n", "systemd-run", f"--uid={user}", f"--gid={user}", "--wait", "--pipe", "--collect",
            "-p", "PrivateNetwork=yes", "-p", "ProtectHome=tmpfs", "-p", "ProtectSystem=strict",
            "-p", f"BindPaths={d}", "-p", f"BindReadOnlyPaths={b}", "-p", f"BindReadOnlyPaths={r}",
            "-p", "PrivateTmp=yes", "-p", "NoNewPrivileges=yes", "-p", "MemoryMax=4G", "-p", "TasksMax=64",
            "-p", "RuntimeMaxSec=120", "-p", "CapabilityBoundingSet=", "-p", "RestrictAddressFamilies=AF_UNIX",
            # round 1 security fix: no host uid view (/proc/<pid>/root), no host /run sockets
            "-p", "PrivateUsers=yes", "-p", "TemporaryFileSystem=/run:ro",
            f"{b}/blender", "-b", "--factory-startup", "--python-exit-code", "1", "--python",
            f"{r}/blender_job_runner.py", "--", str(d)]
    assert calls(box) == [want]
    assert "systemd-run --user" not in WRAPPER.read_text()
    assert (d / "blender.log").exists() and not (d / "blender.log").is_symlink()


def test_wrapper_refuses(box):
    jobs = box["jobs"]
    ok = job(box, "base")
    cases = [((), {}), ((str(ok), str(ok)), {}), (("/tmp/x",), {}), ((f"{jobs}/../x",), {}),
             ((f"{jobs}/a:b",), {}), ((f"{jobs}/a b",), {}), ((f"{jobs}/a%2fb",), {}), ((f"{jobs}/A",), {}),
             ((f"{jobs}/a\nb",), {}), ((f"{jobs}/{'a' * 49}",), {}),
             ((str(ok),), {"JARVIS_BLENDER_DIR": str(box["home"])}),
             ((str(ok),), {"JARVIS_BLENDER_JOBS": "/tmp/jobs"})]
    real = job(box, "real")
    (jobs / "linked").symlink_to(real)
    cases.append(((str(jobs / "linked"),), {}))
    cases.append(((str(jobs / "missing"),), {}))
    noscript = job(box, "noscript")
    (noscript / "object.py").unlink()
    cases.append(((str(noscript),), {}))
    linkscript = job(box, "linkscript")
    (linkscript / "object.py").unlink()
    (linkscript / "object.py").symlink_to(box["secret"])
    cases.append(((str(linkscript),), {}))
    big = job(box, "bigscript", "x = 1\n" * 60000)  # 360 KB
    cases.append(((str(big),), {}))
    stale = job(box, "stale")
    (stale / "object.glb").write_bytes(b"glTF")
    cases.append(((str(stale),), {}))
    for args, env in cases:
        code, out, _err = run(box, *args, **env)
        res = json.loads(out)
        assert code == 2 and res["ok"] is False and res["reason"], (args, env, out)
    (box["blender"] / "blender").chmod(0o644)
    code, out, _err = run(box, str(ok))
    assert code == 2 and json.loads(out)["reason"] == "no_blender"
    (box["blender"] / "blender").chmod(0o755)
    (box["stubs"] / "id").write_text('#!/bin/sh\n[ "$1" = "-u" ] && { echo 0; exit 0; }\nexec /usr/bin/id "$@"\n')
    (box["stubs"] / "id").chmod(0o755)
    code, out, _err = run(box, str(ok))
    assert code == 2 and json.loads(out)["reason"] == "root"
    assert calls(box) == []  # not one sudo call


def test_wrapper_output_checks(box):
    want = {"symlink": "symlink", "hardlink": "symlink", "big": "glb_too_big", "badmagic": "bad_glb",
            "nopng": "no_png", "timeout": "timeout", "oom-kill": "oom", "sudo_fail": "sudo_failed"}
    for i, (mode, reason) in enumerate(want.items()):
        d = job(box, f"out-{i}")
        before = {p for p in box["tmp"].rglob("*")}
        code, out, _err = run(box, str(d), STUB_MODE=mode)
        assert code == 1 and json.loads(out) == {"ok": False, "reason": reason}, (mode, out)
        assert "SECRET" not in out
        for p in {p for p in box["tmp"].rglob("*")} - before:
            if p.is_file() and not p.is_symlink() and p.stat().st_nlink == 1 and p.stat().st_size < 10**6:
                assert b"canary-SECRET" not in p.read_bytes(), p
    # the job left a link named blender.log: the wrapper never writes through it
    d = job(box, "loglink")
    (d / "blender.log").symlink_to(box["secret"])
    assert run(box, str(d))[0] == 0
    assert box["secret"].read_text() == "canary-SECRET-bytes" and not (d / "blender.log").is_symlink()


def _fake_bpy():
    bpy = mock.MagicMock()
    bpy.context.scene.objects = []
    return bpy


class _Translate:
    """matrix_world stand-in: a pure translation (enough for `matrix_world @ Vector(corner)`)."""

    def __init__(self, t):
        self.t = t

    def __matmul__(self, v):
        return tuple(a + b for a, b in zip(v, self.t))


def _run_runner(job_dir, bpy):
    fake_mathutils = SimpleNamespace(Vector=tuple)
    with mock.patch.dict(sys.modules, {"bpy": bpy, "mathutils": fake_mathutils}), mock.patch.object(sys, "argv", ["blender", "--", str(job_dir)]):
        try:
            runpy.run_path(str(RUNNER), run_name="__main__")
        except SystemExit as e:
            return e.code
    return 0


def test_runner_with_fake_bpy(tmp_path):
    d = tmp_path / "job"
    d.mkdir()
    (d / "object.py").write_text("import sys\nsys.modules['bpy'].built = True\n")
    bpy = _fake_bpy()
    assert _run_runner(d, bpy) == 0 and bpy.built is True
    kw = bpy.ops.export_scene.gltf.call_args.kwargs
    assert kw["export_format"] == "GLB" and kw["export_draco_mesh_compression_enable"] is False
    assert kw["filepath"] == str(d / "object.glb") and kw["export_cameras"] is False and kw["export_lights"] is False
    bpy.ops.render.render.assert_called_once_with(write_still=True)
    assert bpy.context.scene.render.filepath == str(d / "preview.png")
    assert bpy.context.scene.render.engine == "BLENDER_WORKBENCH"
    (d / "object.py").write_text("raise RuntimeError('x' * 20000)\n")
    bpy = _fake_bpy()
    assert _run_runner(d, bpy) not in (0, None)
    assert (d / "error.txt").stat().st_size <= 8 * 1024
    assert not bpy.ops.export_scene.gltf.called


def test_preview_frames_a_small_object(tmp_path):
    """Hub ruling R3-CAM: a 5 cm object with its origin at its base fills the preview from the top third to
    the bottom third. No real Blender here: the test projects the bounding box through the runner's camera
    (Blender's default 50 mm lens on a 36 mm sensor, square frame, view along local -Z, XYZ Euler)."""
    import math
    d = tmp_path / "job"
    d.mkdir()
    (d / "object.py").write_text("")
    bpy = _fake_bpy()
    box = [(x, y, z) for x in (-0.01, 0.01) for y in (-0.01, 0.01) for z in (0.0, 0.05)]
    bpy.context.scene.objects = [SimpleNamespace(type="MESH", bound_box=box, matrix_world=_Translate((0, 0, 0)),
                                                 location=(0.0, 0.0, 0.0), dimensions=(0.02, 0.02, 0.05))]
    made = []
    bpy.data.cameras.new = lambda name: SimpleNamespace(clip_start=0.1, clip_end=100.0)  # Blender defaults
    bpy.data.objects.new = lambda name, data: made.append(SimpleNamespace(name=name, data=data)) or made[-1]
    assert _run_runner(d, bpy) == 0
    bpy.context.view_layer.update.assert_called_once_with()  # fresh matrix_world before the bounding box
    cam = next(o for o in made if o.name == "jarvis_preview_camera")
    a, b, c = cam.rotation_euler
    rx = ((1, 0, 0), (0, math.cos(a), -math.sin(a)), (0, math.sin(a), math.cos(a)))
    ry = ((math.cos(b), 0, math.sin(b)), (0, 1, 0), (-math.sin(b), 0, math.cos(b)))
    rz = ((math.cos(c), -math.sin(c), 0), (math.sin(c), math.cos(c), 0), (0, 0, 1))
    def mul(m, n):
        return tuple(tuple(sum(m[i][k] * n[k][j] for k in range(3)) for j in range(3)) for i in range(3))

    rot = mul(rz, mul(ry, rx))
    tan_half = 18 / 50
    ys = []
    for p in box:
        rel = [p[i] - cam.location[i] for i in range(3)]
        x, y, z = (sum(rot[k][i] * rel[k] for k in range(3)) for i in range(3))  # camera space = R^T (p - loc)
        assert cam.data.clip_start < -z < cam.data.clip_end, "a corner falls outside the clip range"
        assert abs(x / (-z * tan_half)) <= 1
        ys.append(y / (-z * tan_half))
    assert max(ys) <= 1 and min(ys) >= -1  # the whole object is in frame
    assert max(ys) > 1 / 3 and min(ys) < -1 / 3  # it reaches the top third and the bottom third


def _install(tmp_path, *args):
    env = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"}
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(["bash", str(INSTALLER), *args], env=env, capture_output=True, text=True, timeout=60)


def test_installer(tmp_path):
    vault = tmp_path / "my vault & notes"
    (vault / ".obsidian").mkdir(parents=True)
    home = tmp_path / "home"
    r = _install(tmp_path, "install", "--vault", str(vault), "--dry-run")
    assert r.returncode == 0 and r.stdout.strip()
    assert all(line.startswith("would: ") for line in r.stdout.splitlines()), r.stdout
    assert not (home / ".claude").exists() and not (home / ".local").exists()
    for bad in ("/nope", str(tmp_path)):
        r = _install(tmp_path, "install", "--vault", bad)
        assert r.returncode != 0 and not (home / ".claude").exists() and not (home / ".local").exists()
    r = _install(tmp_path, "install", "--vault", str(vault))
    assert r.returncode == 0, r.stderr
    skill = home / ".claude" / "skills" / "blender-object" / "SKILL.md"
    bin_dir = home / ".local" / "share" / "jarvis-blender" / "bin"
    jobs = home / ".local" / "share" / "jarvis-blender" / "jobs"
    modes = {skill: 0o644, bin_dir / "blender-job.sh": 0o755, bin_dir / "blender_job_runner.py": 0o644, jobs: 0o700}
    for path, mode in modes.items():
        assert (path.stat().st_mode & 0o777) == mode, path
    text = skill.read_text()
    assert str(vault) in text and "__JARVIS_VAULT__" not in text and text.startswith("---\nname: blender-object\n")
    r = _install(tmp_path, "uninstall")
    assert r.returncode == 0
    assert not any(p.exists() for p in modes) and not (home / ".local" / "share" / "jarvis-blender").exists()
    for script in (WRAPPER, INSTALLER):
        assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    compile(RUNNER.read_text(), str(RUNNER), "exec")  # syntax only, no __pycache__ in the repo
    if shutil.which("shellcheck"):
        r = subprocess.run(["shellcheck", "-S", "error", str(WRAPPER), str(INSTALLER)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
