"""Runs INSIDE Blender (bpy). Not imported by the app.

Usage:
    blender -b --factory-startup --python runtime.py -- payload.json

Payload keys:
    mode: "python" | "screenshot" | "render" | "probe"
    blend: path to scene.blend (opened if it exists, saved after python mode)
    code: bpy code for python mode
    ...
Prints a single line:  SCENEDIRECTOR_RESULT:{json}
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path

import bpy
from mathutils import Vector

# --------------------------------------------------------------------- helpers
def report() -> dict:
    scene = bpy.context.scene
    objects = []
    for obj in scene.objects:
        item = {"name": obj.name, "type": obj.type,
                "location": [round(v, 3) for v in obj.location]}
        if obj.type == "MESH":
            item["dims"] = [round(v, 3) for v in obj.dimensions]
            item["verts"] = len(obj.data.vertices)
        objects.append(item)
    return {
        "scene": scene.name,
        "objects": objects,
        "object_count": len(objects),
        "cameras": [o.name for o in scene.objects if o.type == "CAMERA"],
        "active_camera": scene.camera.name if scene.camera else None,
        "engine": scene.render.engine,
        "frames": [scene.frame_start, scene.frame_end, scene.render.fps],
        "collections": [c.name for c in bpy.data.collections],
    }


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.cameras,
                  bpy.data.lights, bpy.data.images):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def collection(name: str):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)
    return col


def _link(obj, col_name: str | None = None):
    if col_name:
        col = collection(col_name)
        if obj.name not in col.objects:
            col.objects.link(obj)
        for old in list(obj.users_collection):
            if old != col:
                old.objects.unlink(obj)
    return obj


def import_asset(path: str, name: str | None = None, collection_name: str = "Assets"):
    """Import gltf/glb/fbx/obj/stl. Returns names of newly created objects."""
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Asset not found: {path}")
    before = set(bpy.data.objects)
    ext = p.suffix.lower()
    if ext in {".gltf", ".glb"}:
        bpy.ops.import_scene.gltf(filepath=str(p))
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(p))
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=str(p))
        else:
            bpy.ops.import_scene.obj(filepath=str(p))
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=str(p)) if hasattr(bpy.ops.wm, "stl_import") \
            else bpy.ops.import_mesh.stl(filepath=str(p))
    elif ext in {".usd", ".usdz"}:
        bpy.ops.wm.usd_import(filepath=str(p))
    else:
        raise ValueError(f"Unsupported asset format: {ext}")

    new_objects = [o for o in bpy.data.objects if o not in before]
    for obj in new_objects:
        _link(obj, collection_name)
    if name:
        for obj in new_objects:
            if obj.name.startswith(name) or len(new_objects) == 1:
                obj.name = name
    return [o.name for o in new_objects]


def world_bounds(objects=None) -> tuple[Vector, Vector]:
    objects = objects or [o for o in bpy.context.scene.objects
                          if o.type in {"MESH", "CURVE", "SURFACE", "FONT"}]
    mins = Vector((math.inf, math.inf, math.inf))
    maxs = Vector((-math.inf, -math.inf, -math.inf))
    for obj in objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            world = obj.matrix_world @ Vector(corner)
            for i in range(3):
                mins[i] = min(mins[i], world[i])
                maxs[i] = max(maxs[i], world[i])
    if mins.x == math.inf:
        return Vector((0, 0, 0)), Vector((1, 1, 1))
    return mins, maxs


def place_on_ground(obj, z: float = 0.0) -> None:
    bpy.context.view_layer.update()
    mins, _ = world_bounds([obj])
    obj.location.z += z - mins.z
    bpy.context.view_layer.update()


def look_at(obj, target) -> None:
    target = Vector(target)
    direction = target - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def make_camera(name: str = "Camera", location=(7, -7, 5), target=(0, 0, 1),
                lens: float = 50.0):
    cam_data = bpy.data.cameras.get(name) or bpy.data.cameras.new(name)
    cam = bpy.data.objects.get(name)
    if cam is None:
        cam = bpy.data.objects.new(name, cam_data)
        bpy.context.scene.collection.objects.link(cam)
    cam.data.lens = lens
    cam.location = Vector(location)
    look_at(cam, target)
    cam.data.clip_end = 1000
    bpy.context.scene.camera = cam
    return cam


def auto_frame_camera(name: str = "Camera", margin: float = 1.35, lens: float = 50.0):
    mins, maxs = world_bounds()
    center = (mins + maxs) / 2
    size = max(maxs - mins) or 1.0
    fov = 2 * math.atan(18.0 / lens)
    dist = (size / 2) / math.tan(fov / 2) * margin
    direction = Vector((1.0, -1.0, 0.55)).normalized()
    cam = make_camera(name, center + direction * dist, center, lens)
    cam.data.lens = lens
    return cam


def add_ground(size: float = 40.0, color=(0.32, 0.30, 0.28), roughness: float = 0.85):
    mesh = bpy.data.meshes.new("GroundMesh")
    verts = [(-size, -size, 0), (size, -size, 0), (size, size, 0), (-size, size, 0)]
    mesh.from_pydata(verts, [], [(0, 1, 2, 3)])
    mesh.update()
    obj = bpy.data.objects.new("Ground", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mat = bpy.data.materials.new("GroundMaterial")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*color, 1)
        bsdf.inputs["Roughness"].default_value = roughness
    obj.data.materials.append(mat)
    return obj


def add_hdri(path: str, strength: float = 1.0, rotation_deg: float = 0.0):
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"HDRI not found: {path}")
    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputWorld")
    bg = nodes.new("ShaderNodeBackground")
    env = nodes.new("ShaderNodeTexEnvironment")
    mapping = nodes.new("ShaderNodeMapping")
    texco = nodes.new("ShaderNodeTexCoord")
    env.image = bpy.data.images.load(str(p), check_existing=True)
    bg.inputs["Strength"].default_value = strength
    mapping.inputs["Rotation"].default_value[2] = math.radians(rotation_deg)
    links.new(texco.outputs["Generated"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    links.new(env.outputs["Color"], bg.inputs["Color"])
    links.new(bg.outputs["Background"], out.inputs["Surface"])
    return world


def set_world_color(color=(0.05, 0.05, 0.06), strength: float = 1.0):
    world = bpy.context.scene.world or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (*color, 1)
        bg.inputs["Strength"].default_value = strength


def add_sun(energy: float = 3.0, rotation_deg=(50, 0, 30), color=(1, 0.96, 0.9)):
    data = bpy.data.lights.new("Sun", type="SUN")
    data.energy = energy
    data.color = color
    obj = bpy.data.objects.new("Sun", data)
    bpy.context.scene.collection.objects.link(obj)
    obj.rotation_euler = tuple(math.radians(a) for a in rotation_deg)
    return obj


def add_area_light(location=(4, -4, 5), energy: float = 400.0, size: float = 5.0,
                   color=(1, 1, 1), target=(0, 0, 1), name: str = "AreaLight"):
    data = bpy.data.lights.new(name, type="AREA")
    data.energy = energy
    data.size = size
    data.color = color
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = Vector(location)
    look_at(obj, target)
    return obj


def keyframe(obj, frame: int, location=None, rotation=None, scale=None) -> None:
    if location is not None:
        obj.location = Vector(location)
        obj.keyframe_insert("location", frame=frame)
    if rotation is not None:
        obj.rotation_euler = rotation
        obj.keyframe_insert("rotation_euler", frame=frame)
    if scale is not None:
        obj.scale = scale
        obj.keyframe_insert("scale", frame=frame)


def set_frame_range(start: int, end: int, fps: int = 24) -> None:
    scene = bpy.context.scene
    scene.frame_start = start
    scene.frame_end = end
    scene.render.fps = fps


def set_frame(frame: int) -> None:
    bpy.context.scene.frame_set(frame)


def smooth_shade(obj, angle_deg: float = 40.0) -> None:
    for poly in obj.data.polygons:
        poly.use_smooth = True
    if hasattr(obj.data, "use_auto_smooth"):
        obj.data.use_auto_smooth = True
        obj.data.auto_smooth_angle = math.radians(angle_deg)


def select_only(obj) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def apply_transforms(obj) -> None:
    select_only(obj)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)


def duplicate(obj, offset=(0, 0, 0)):
    new = obj.copy()
    new.data = obj.data.copy() if obj.data else None
    bpy.context.scene.collection.objects.link(new)
    new.location = obj.location + Vector(offset)
    return new


def scale_to_height(obj, target_height: float):
    """Uniformly scale an object so its world-space height matches target_height."""
    bpy.context.view_layer.update()
    mins, maxs = world_bounds([obj])
    height = maxs.z - mins.z
    if height <= 1e-6:
        raise ValueError("Object has no measurable height")
    factor = float(target_height) / height
    obj.scale = obj.scale * factor
    bpy.context.view_layer.update()
    return factor


def analyze_asset(path: str, target_height: float | None = None) -> dict:
    """Import a model into an empty scene and report health stats.

    Used by the analyze mode (no save), so it never touches the project scene.
    """
    import bmesh

    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"Model not found: {path}")

    names = import_asset(str(p), collection_name="Analysis")
    mesh_objects = [
        bpy.data.objects[name] for name in names
        if bpy.data.objects.get(name) and bpy.data.objects[name].type == "MESH"
    ]

    total_verts = total_faces = 0
    materials: set[str] = set()
    textures = 0
    has_uv = False
    non_manifold = 0
    loose_parts = 0

    for obj in mesh_objects:
        mesh = obj.data
        total_verts += len(mesh.vertices)
        total_faces += len(mesh.polygons)
        has_uv = has_uv or bool(mesh.uv_layers)
        for slot in obj.material_slots:
            mat = slot.material
            if not mat:
                continue
            materials.add(mat.name)
            if mat.use_nodes:
                textures += sum(1 for node in mat.node_tree.nodes
                                if node.type == "TEX_IMAGE" and node.image)
        bm = bmesh.new()
        bm.from_mesh(mesh)
        non_manifold += sum(1 for edge in bm.edges if not edge.is_manifold)
        # count connected vertex islands (loose parts)
        unseen = set(bm.verts)
        while unseen:
            loose_parts += 1
            stack = [unseen.pop()]
            while stack:
                vert = stack.pop()
                for edge in vert.link_edges:
                    other = edge.other_vert(vert)
                    if other in unseen:
                        unseen.remove(other)
                        stack.append(other)
        bm.free()

    if mesh_objects:
        mins, maxs = world_bounds(mesh_objects)
    else:
        mins, maxs = Vector((0, 0, 0)), Vector((0, 0, 0))
    dims = maxs - mins
    height = dims.z
    warnings: list[str] = []

    if not mesh_objects:
        warnings.append("No mesh objects were found in this file.")
    if height <= 1e-6:
        warnings.append("Model has no height; it may be a flat card or empty.")
    elif target_height:
        if abs(height - float(target_height)) / float(target_height) > 0.1:
            warnings.append(
                f"Height {height:.3f} m differs from target {float(target_height):.3f} m; "
                "use scale_to_height(obj, target)."
            )
    elif height < 0.05 or height > 10:
        warnings.append(
            f"Height {height:.3f} m looks like a unit mismatch (mm/cm vs m). "
            "Check scale before placing."
        )
    if mesh_objects and not materials:
        warnings.append("No materials found; the mesh will render untextured.")
    if non_manifold:
        warnings.append(f"{non_manifold} non-manifold edges; inspect before 3D printing/rigging.")
    if loose_parts > 12:
        warnings.append(f"{loose_parts} disconnected mesh islands; check for floating parts.")
    if not has_uv:
        warnings.append("No UV map; textures may not map correctly.")

    for name in names:
        obj = bpy.data.objects.get(name)
        if obj:
            bpy.data.objects.remove(obj, do_unlink=True)

    return {
        "path": str(p),
        "objects": names,
        "meshes": len(mesh_objects),
        "verts": total_verts,
        "faces": total_faces,
        "dimensions": [round(v, 4) for v in dims],
        "height": round(height, 4),
        "materials": sorted(materials),
        "textures": textures,
        "has_uv": has_uv,
        "non_manifold_edges": non_manifold,
        "loose_parts": loose_parts,
        "suggested_scale": round(float(target_height) / height, 4)
        if target_height and height > 1e-6 else None,
        "warnings": warnings,
    }


def _ensure_camera(camera_name: str | None):
    scene = bpy.context.scene
    if camera_name:
        cam = bpy.data.objects.get(camera_name)
        if cam:
            scene.camera = cam
            return cam
    if scene.camera is None:
        return auto_frame_camera()
    return scene.camera


def _apply_engine(engine: str | None):
    scene = bpy.context.scene
    engine = engine or "BLENDER_WORKBENCH"
    aliases = {"EEVEE": "BLENDER_EEVEE_NEXT", "CYCLES": "CYCLES",
               "WORKBENCH": "BLENDER_WORKBENCH"}
    engine = aliases.get(engine.upper(), engine)
    try:
        scene.render.engine = engine
    except TypeError:
        if engine == "BLENDER_EEVEE_NEXT":
            scene.render.engine = "BLENDER_EEVEE"
        else:
            raise
    return scene.render.engine


def _workbench_settings():
    shading = bpy.context.scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "MATERIAL"
    shading.show_shadows = True
    shading.show_cavity = True
    shading.cavity_type = "BOTH"
    shading.background_type = "VIEWPORT"
    shading.background_color = (0.045, 0.045, 0.055)


def render_still(path: str, camera: str | None = None, resolution=(960, 540),
                 engine: str | None = None, samples: int = 16, frame: int | None = None,
                 transparent: bool = False) -> str:
    scene = bpy.context.scene
    _ensure_camera(camera)
    if frame is not None:
        scene.frame_set(frame)
    scene.render.resolution_x, scene.render.resolution_y = resolution
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = transparent
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(out)
    scene.render.image_settings.file_format = "PNG"

    requested = _apply_engine(engine)
    if requested == "BLENDER_WORKBENCH":
        _workbench_settings()
    else:
        try:
            if requested == "CYCLES":
                scene.cycles.samples = samples
                scene.cycles.use_denoising = True
            else:
                scene.eevee.taa_render_samples = samples
            bpy.ops.render.render(write_still=True)
            return str(out)
        except Exception:
            # headless GPU issues -> fall back to workbench
            _apply_engine("BLENDER_WORKBENCH")
            _workbench_settings()
    bpy.ops.render.render(write_still=True)
    return str(out)


def render_animation(path: str, camera: str | None = None, resolution=(1280, 720),
                     engine: str | None = "BLENDER_EEVEE_NEXT", samples: int = 16,
                     frame_start: int | None = None, frame_end: int | None = None,
                     fps: int | None = None) -> str:
    """Render PNG frames, then encode to MP4 with the system ffmpeg.

    Blender 5.x no longer exposes FFMPEG as a still-image output format, and
    external encoding is faster to debug anyway. Frame range defaults to the scene.
    """
    import re
    import shutil
    import subprocess

    scene = bpy.context.scene
    _ensure_camera(camera)
    frame_start = scene.frame_start if frame_start is None else frame_start
    frame_end = scene.frame_end if frame_end is None else frame_end
    fps = scene.render.fps if fps is None else fps
    scene.render.resolution_x, scene.render.resolution_y = resolution
    scene.render.resolution_percentage = 100
    scene.render.fps = fps
    scene.frame_start = frame_start
    scene.frame_end = frame_end

    out = Path(path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    frames_dir = out.parent / f"{out.stem}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(frames_dir / "frame_")

    requested = _apply_engine(engine)
    if requested == "CYCLES":
        scene.cycles.samples = samples
    elif requested == "BLENDER_EEVEE_NEXT":
        try:
            scene.eevee.taa_render_samples = samples
        except AttributeError:
            pass
    try:
        bpy.ops.render.render(animation=True)
    except Exception as exc:
        print(f"Animation render engine failed ({exc}); falling back to Workbench")
        _apply_engine("BLENDER_WORKBENCH")
        _workbench_settings()
        for stale in frames_dir.glob("*.png"):
            stale.unlink()
        bpy.ops.render.render(animation=True)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print(f"ffmpeg not found; PNG frames are in {frames_dir}")
        return str(frames_dir)

    first_frame = frames_dir / f"frame_{frame_start:04d}.png"
    if not first_frame.exists():
        candidates = sorted(frames_dir.glob("frame_*.png"))
        if not candidates:
            raise RuntimeError("No frames were rendered")
        first_frame = candidates[0]
    pattern = re.sub(r"\d+(?=\.png$)", "%04d", first_frame.name)
    cmd = [
        ffmpeg, "-y", "-framerate", str(fps), "-start_number", str(frame_start),
        "-i", str(frames_dir / pattern),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        "-movflags", "+faststart", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-1500:]}")
    shutil.rmtree(frames_dir, ignore_errors=True)
    return str(out)


# ------------------------------------------------------------------- dispatch
def main() -> None:
    argv = sys.argv
    payload = {}
    if "--" in argv:
        payload_path = Path(argv[argv.index("--") + 1])
        payload = json.loads(payload_path.read_text(encoding="utf-8"))

    blend = payload.get("blend")
    mode = payload.get("mode", "python")
    stdout = io.StringIO()
    result: dict = {"ok": True}

    try:
        if blend and Path(blend).exists():
            bpy.ops.wm.open_mainfile(filepath=blend)

        with redirect_stdout(stdout):
            if mode == "python":
                code = payload.get("code", "")
                ns = {
                    "__name__": "__agent__",
                    "bpy": bpy,
                    "math": math,
                    "Vector": Vector,
                    "Path": Path,
                    "report": report,
                    "clear_scene": clear_scene,
                    "collection": collection,
                    "import_asset": import_asset,
                    "world_bounds": world_bounds,
                    "place_on_ground": place_on_ground,
                    "look_at": look_at,
                    "make_camera": make_camera,
                    "auto_frame_camera": auto_frame_camera,
                    "add_ground": add_ground,
                    "add_hdri": add_hdri,
                    "set_world_color": set_world_color,
                    "add_sun": add_sun,
                    "add_area_light": add_area_light,
                    "keyframe": keyframe,
                    "set_frame_range": set_frame_range,
                    "set_frame": set_frame,
                    "smooth_shade": smooth_shade,
                    "select_only": select_only,
                    "apply_transforms": apply_transforms,
                    "duplicate": duplicate,
                    "scale_to_height": scale_to_height,
                    "analyze_asset": analyze_asset,
                    "render_still": render_still,
                    "render_animation": render_animation,
                }
                exec(compile(code, "<agent>", "exec"), ns)
                if blend and payload.get("save", True):
                    bpy.ops.wm.save_as_mainfile(filepath=blend)
                result["report"] = report()

            elif mode == "screenshot":
                path = render_still(
                    payload["path"],
                    camera=payload.get("camera"),
                    resolution=tuple(payload.get("resolution", (960, 540))),
                    engine=payload.get("engine", "BLENDER_WORKBENCH"),
                    samples=int(payload.get("samples", 16)),
                    frame=payload.get("frame"),
                )
                result["path"] = path

            elif mode == "render":
                if payload.get("animation"):
                    path = render_animation(
                        payload["path"],
                        camera=payload.get("camera"),
                        resolution=tuple(payload.get("resolution", (1280, 720))),
                        engine=payload.get("engine", "BLENDER_EEVEE_NEXT"),
                        samples=int(payload.get("samples", 16)),
                        frame_start=payload.get("frame_start"),
                        frame_end=payload.get("frame_end"),
                        fps=payload.get("fps"),
                    )
                else:
                    path = render_still(
                        payload["path"],
                        camera=payload.get("camera"),
                        resolution=tuple(payload.get("resolution", (1280, 720))),
                        engine=payload.get("engine"),
                        samples=int(payload.get("samples", 64)),
                        frame=payload.get("frame"),
                    )
                result["path"] = path

            elif mode == "probe":
                result["version"] = bpy.app.version_string
                result["report"] = report()

            elif mode == "analyze":
                result["analysis"] = analyze_asset(
                    payload["path"], payload.get("target_height")
                )

            else:
                raise ValueError(f"Unknown mode: {mode}")

            if blend and mode != "python" and payload.get("save", False):
                bpy.ops.wm.save_as_mainfile(filepath=blend)

    except Exception as exc:  # noqa: BLE001 - report back to the agent
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-4000:],
        }

    result["stdout"] = stdout.getvalue()[-6000:]
    # Blender may print other lines; the bridge locates this marker.
    print("SCENEDIRECTOR_RESULT:" + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
