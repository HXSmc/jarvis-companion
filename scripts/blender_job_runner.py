"""Fixed driver Blender runs inside the blender-job.sh sandbox (jfollow10d).

`blender -b --factory-startup --python blender_job_runner.py -- <jobdir>`: run the job's object.py,
then (fixed code, never the job's) light and render a 512x512 Workbench preview to preview.png and
export the scene to object.glb (GLB, Draco off, no cameras or lights). A failing object.py leaves the
tail of its traceback in error.txt and exits 1 with nothing exported.
"""
import math
import os
import runpy
import sys
import traceback

ERROR_MAX = 8 * 1024


def job_dir() -> str:
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(args) != 1:
        sys.exit(2)
    return args[0]


def preview(bpy, mathutils, job: str) -> None:
    """Frame a camera on every mesh, add a sun, render Workbench 512x512 to <job>/preview.png."""
    bpy.context.view_layer.update()  # matrix_world is stale after a script that only set location/scale/parent
    scene = bpy.context.scene
    meshes = [o for o in scene.objects if getattr(o, "type", "") == "MESH"]
    # The world-space bounding box, not the origins: an origin at the base or a tiny object still frames whole.
    corners = [tuple(o.matrix_world @ mathutils.Vector(c)) for o in meshes for c in o.bound_box]
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
    try:
        preview(bpy, mathutils, job)
    except Exception:  # noqa: BLE001 - no preview: the GLB still exports, the wrapper says no_png
        traceback.print_exc()
    bpy.ops.export_scene.gltf(filepath=os.path.join(job, "object.glb"), export_format="GLB",
                              export_draco_mesh_compression_enable=False, export_apply=True,
                              export_cameras=False, export_lights=False, export_yup=True)


if __name__ == "__main__":
    main()
