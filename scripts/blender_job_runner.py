"""Fixed driver Blender runs inside the blender-job.sh sandbox (jfollow10d, j3d-h2).

`blender -b --factory-startup --python blender_job_runner.py -- <jobdir>`: run the job's object.py,
then (fixed code, never the job's) light and render a 512x512 Workbench preview to preview.png and
export the scene to object.glb (GLB, Draco off, no cameras or lights). A failing object.py leaves the
tail of its traceback in error.txt and exits 1 with nothing exported.

An animated scene (rigid bodies or any keyframed datablock) is capped first: 24 fps, frame_start 1,
at most 240 frames, at most 32 ACTIVE bodies; rigid bodies are baked to keyframes by recording each
frame's matrix_world (the only bake that works headless), then the clip is rendered frame by frame
to <jobdir>/frames/f_0001.png..f_NNNN.png for the wrapper's ffmpeg step, and the GLB is exported with
animations in. A static scene takes today's byte-identical path.
"""
import math
import os
import runpy
import sys
import traceback

ERROR_MAX = 8 * 1024
FRAMES_MAX = 240
RIGID_MAX = 32


def job_dir() -> str:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(args) != 1:
        sys.exit(2)
    return args[0]


def die(job: str, msg: str) -> None:
    """A refused job: the message in error.txt, exit 1, nothing rendered or exported."""
    with open(os.path.join(job, "error.txt"), "w", encoding="utf-8") as f:
        f.write(msg + "\n")
    sys.exit(1)


def is_animated(scene) -> bool:
    """Physics (a rigid-body world and at least one body) or any keyframed datablock: an object,
    its data or its shape keys. Every attribute via getattr: a mock scene stays static."""
    if getattr(scene, "rigidbody_world", None) is not None and any(
            getattr(o, "rigid_body", None) is not None for o in scene.objects):
        return True
    for o in scene.objects:
        data = getattr(o, "data", None)
        for holder in (o, data, getattr(data, "shape_keys", None)):
            ad = getattr(holder, "animation_data", None) if holder is not None else None
            if ad is not None and getattr(ad, "action", None) is not None:
                return True
    return False


def bake_physics(bpy, scene, world, bodies) -> None:
    """The only bake that works headless (probe-verified): record each body's matrix_world after
    every frame_set, drop the rigid-body world, then keyframe location and quaternion rotation from
    the recorded matrices. bake_to_keyframes and ptcache.bake_all both fail headless."""
    start, end = scene.frame_start, scene.frame_end
    world.point_cache.frame_start, world.point_cache.frame_end = start, end
    seen = {o: {} for o in bodies}
    for f in range(start, end + 1):
        scene.frame_set(f)
        for o in bodies:
            seen[o][f] = o.matrix_world.copy()  # live wrapped matrix: the next frame_set overwrites it
    with bpy.context.temp_override(scene=scene):
        if bpy.ops.rigidbody.world_remove() != {"FINISHED"}:
            raise RuntimeError("rigidbody world_remove failed")
    for o in bodies:
        o.rotation_mode = "QUATERNION"
    for f in range(start, end + 1):
        for o in bodies:
            loc, rot, _scale = seen[o][f].decompose()
            o.location, o.rotation_quaternion = loc, rot
            o.keyframe_insert("location", frame=f)
            o.keyframe_insert("rotation_quaternion", frame=f)


def preview(bpy, mathutils, job: str, clip: bool = False) -> None:
    """Frame a camera on every mesh, add a sun, render Workbench 512x512 to <job>/preview.png.
    With clip=True the box is the union of the world boxes over the whole clip (every 10th frame
    plus the last) so the motion stays in frame; the scene ends back at frame_start."""
    scene = bpy.context.scene
    meshes = [o for o in scene.objects if getattr(o, "type", "") == "MESH"]
    if clip:
        frames = sorted(set(range(scene.frame_start, scene.frame_end + 1, 10)) | {scene.frame_end})
        corners = []
        for f in frames:
            scene.frame_set(f)
            corners += [tuple(o.matrix_world @ mathutils.Vector(c)) for o in meshes for c in o.bound_box]
        scene.frame_set(scene.frame_start)
    else:
        bpy.context.view_layer.update()  # matrix_world is stale after a script that only set location/scale/parent
        corners = [tuple(o.matrix_world @ mathutils.Vector(c)) for o in meshes for c in o.bound_box]
    # The world-space bounding box, not the origins: an origin at the base or a tiny object still frames whole.
    center, radius = (0.0, 0.0, 0.0), 1.0
    if corners:
        center = tuple((min(v[i] for v in corners) + max(v[i] for v in corners)) / 2 for i in range(3))
        radius = max(math.dist(v, center) for v in corners) or 1.0
    dx, dy, dz = 1.0 * radius * 2.4, -1.0 * radius * 2.4, 0.8 * radius * 2.4
    cam = bpy.data.objects.new("jarvis_preview_camera", bpy.data.cameras.new("jarvis_preview_camera"))
    scene.collection.objects.link(cam)
    cam.location = (center[0] + dx, center[1] + dy, center[2] + dz)
    cam.rotation_euler = (math.atan2(math.hypot(dx, dy), dz), 0.0, math.atan2(dx, -dy))
    cam.data.clip_start, cam.data.clip_end = radius * 0.01, radius * 100  # the 0.1 m default clips small objects
    scene.camera = cam
    sun = bpy.data.objects.new("jarvis_preview_sun", bpy.data.lights.new("jarvis_preview_sun", "SUN"))
    scene.collection.objects.link(sun)
    sun.rotation_euler = (0.8, 0.2, 0.6)
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = os.path.join(job, "preview.png")
    bpy.ops.render.render(write_still=True)


def main() -> None:
    job = job_dir()
    import bpy  # Blender's own module: only here, inside Blender
    import mathutils  # ships inside Blender, like bpy

    try:
        runpy.run_path(os.path.join(job, "object.py"), run_name="__main__")
    except BaseException:  # noqa: BLE001 - any failure of the job's script, SystemExit included
        with open(os.path.join(job, "error.txt"), "w", encoding="utf-8") as f:
            f.write(traceback.format_exc()[-ERROR_MAX:])
        sys.exit(1)
    scene = bpy.context.scene
    clip = is_animated(scene) and scene.frame_end > scene.frame_start
    if clip:
        scene.render.fps, scene.render.fps_base = 24, 1.0
        if scene.frame_start != 1:
            die(job, "frame_start must be 1")
        scene.frame_end = min(scene.frame_end, FRAMES_MAX)
        world = getattr(scene, "rigidbody_world", None)
        if world is not None:
            bodies = [o for o in scene.objects
                      if getattr(getattr(o, "rigid_body", None), "type", None) == "ACTIVE"]
            if len(bodies) > RIGID_MAX:
                die(job, "too many rigid bodies (max %d)" % RIGID_MAX)
            bake_physics(bpy, scene, world, bodies)
    try:
        preview(bpy, mathutils, job, clip=clip)
    except Exception:  # noqa: BLE001 - no preview: the GLB still exports, the wrapper says no_png
        traceback.print_exc()
    if clip:  # one PNG per frame; the wrapper's second sandbox unit turns them into the mp4
        try:
            frames_dir = os.path.join(job, "frames")
            os.makedirs(frames_dir, exist_ok=True)
            for i in range(scene.frame_start, scene.frame_end + 1):
                scene.frame_set(i)  # without it every frame file shows the preview's pose
                scene.render.filepath = os.path.join(frames_dir, "f_%04d.png" % i)
                bpy.ops.render.render(write_still=True)
            scene.frame_set(scene.frame_start)
        except Exception:  # noqa: BLE001 - a frame failure costs only the mp4 (the wrapper says bad_frames)
            traceback.print_exc()
    kwargs = dict(filepath=os.path.join(job, "object.glb"), export_format="GLB",
                  export_draco_mesh_compression_enable=False, export_apply=True,
                  export_cameras=False, export_lights=False, export_yup=True)
    if clip:
        kwargs.update(export_animations=True, export_frame_range=True,
                      export_force_sampling=True, export_animation_mode="ACTIONS")
    bpy.ops.export_scene.gltf(**kwargs)


if __name__ == "__main__":
    main()
