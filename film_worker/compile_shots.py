"""Compile an observed, frame-indexed shot plan into an editable Blender scene."""
import argparse
import json
import math
import runpy
import sys
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

import bpy
from mathutils import Euler, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
from actor_guard import ACTOR_GEOMETRY, assert_unchanged, capture
from artifact_staging import staging_directory
from engineering_checks import EngineeringValidationError, require_cycles
from shot_draft import inputs_hash
from production_palette import ACTOR_ROLES, PALETTE, PROP_ROLES, ROLE_PROPERTY, apply_palette, color, material

RAD = math.radians
UP = Vector((0, 0, 1))
JOINTS = {"torso": ("DEF-spine", None), "head": ("DEF-spine.006", "torso")}
for side in ("L", "R"):
    for part, parent in (("upper_arm", "torso"), ("forearm", "upper_arm." + side),
                         ("thigh", None), ("shin", "thigh." + side)):
        JOINTS[part + "." + side] = (part + "_fk." + side, parent)


def numeric(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"需要有限数值，收到 {value!r}")
    return float(value)


def vector(value, size=3):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f"需要 {size} 个坐标，收到 {value!r}")
    return tuple(numeric(v) for v in value)


def positive(value):
    value = numeric(value)
    if value <= 0:
        raise ValueError("尺寸、距离和焦距必须大于 0")
    return value


def palette_spec(name, spec, inherited="prop"):
    is_actor = spec["kind"] == "actor"
    role = spec.get("palette_role", None if is_actor else inherited)
    if not isinstance(role, str) or role not in (ACTOR_ROLES if is_actor else PROP_ROLES):
        raise ValueError(f"{name} 的 palette_role 无效：{role!r}")
    return dict(spec, palette_role=role, parts=[
        palette_spec(f"{name} · {part.get('name', index)}", part, role)
        for index, part in enumerate(spec.get("parts", []), 1)
    ])


def new_object(name, data, collection, parent=None):
    ob = bpy.data.objects.new(name, data)
    collection.objects.link(ob)
    ob.parent = parent
    return ob


def rotation(degrees):
    return Euler(tuple(RAD(v) for v in vector(degrees))).to_quaternion()


def key_rotation(ob, quat, frame):
    quat.make_compatible(ob.rotation_quaternion.copy())
    ob.rotation_mode = "QUATERNION"
    ob.rotation_quaternion = quat
    ob.keyframe_insert(data_path="rotation_quaternion", frame=frame)


def keys(entries, start, end):
    if not isinstance(entries, list) or not entries:
        raise ValueError("镜头相机和物体轨道都需要首帧")
    previous = start - 1
    for entry in entries:
        f = entry["f"]
        if type(f) is not int or not previous < f <= end or (previous < start and f != start):
            raise ValueError(f"轨道帧号 {f} 无效，应从 {start} 开始，递增至镜头末帧 {end} 以内")
        previous = f
        yield f, entry


def span(ob, start, end, count):
    for attr in ("hide_render", "hide_viewport"):
        states = {1: True, start: getattr(ob, attr)}
        if end < count:
            states[end + 1] = True
        for f, hidden in states.items():
            setattr(ob, attr, hidden)
            ob.keyframe_insert(data_path=attr, frame=f)


@lru_cache(None)
def load_actor(library):
    with bpy.data.libraries.load(str(library), link=False) as (available, loaded):
        if "L3" not in available.objects:
            raise RuntimeError(f"{library} 缺少 L3 人偶，请在工作台设置正确的人体白模。")
        loaded.objects = ["L3"]
    source = loaded.objects[0]
    # Appended materials retain a library weak path. Copies keep the original
    # shader graph and let the saved scene travel without the source file.
    return source, [mat.copy() for mat in source.data.materials]


@lru_cache(None)
def actor_geometry(library, height):
    # Keep every original rigid face and both direction markers.
    source, local_materials = load_actor(library)
    rig_to_mesh = (source.matrix_parent_inverse @ source.matrix_basis).inverted()
    bottom = min(v.co.z for v in source.data.vertices)
    scale = height / (max(v.co.z for v in source.data.vertices) - bottom)
    pivots = {tag: (rig_to_mesh @ source.parent.data.bones[bone].head_local - bottom * UP) * scale
              for tag, (bone, _) in JOINTS.items()}
    region = {"DEF-spine": "torso", "DEF-spine.006": "head"}
    region.update({"DEF-" + name: name for name in JOINTS if "." in name})
    groups = [region[source.vertex_groups[max(v.groups, key=lambda w: w.weight).group].name]
              for v in source.data.vertices]
    parts = {}
    for joint, pivot in pivots.items():
        ids = [i for i, g in enumerate(groups) if g == joint]
        remap = {source_id: local for local, source_id in enumerate(ids)}
        faces = [p for p in source.data.polygons if groups[p.vertices[0]] == joint]
        vertices = [(source.data.vertices[i].co - bottom * UP) * scale - pivot for i in ids]
        mesh = bpy.data.meshes.new("L3 " + joint)
        mesh.from_pydata(vertices, [], [tuple(remap[i] for i in p.vertices) for p in faces])
        mesh.update()
        for mat in local_materials:
            mesh.materials.append(mat)
        for dest, original in zip(mesh.polygons, faces):
            dest.material_index = original.material_index
            dest.use_smooth = original.use_smooth
        parts[joint] = mesh
    return pivots, parts


def actor(name, definition, collection, library):
    height = positive(definition.get("height", 1.72))
    pivots, meshes = actor_geometry(library, height)
    root = new_object(name, None, collection)
    parts = {}
    role = definition.get("palette_role")
    for joint, (_, parent) in JOINTS.items():
        ob = new_object(name + " · " + joint, meshes[joint], collection, parts[parent] if parent else root)
        ob.location = pivots[joint] - (pivots[parent] if parent else Vector((0, 0, 0)))
        if role:
            ob["movie_actor_body"] = True
            apply_palette(ob, role)
        else:
            ob.material_slots[0].link = "OBJECT"
            ob.material_slots[0].material = material(definition.get("color", "#5280bd"))
        parts[joint] = ob
    return root, parts


def prop(name, spec, collection, parent=None):
    kind = spec["kind"]
    if kind == "group":
        ob = new_object(name, None, collection, parent)
    else:
        builders = {"box": bpy.ops.mesh.primitive_cube_add, "sphere": bpy.ops.mesh.primitive_uv_sphere_add,
                    "cylinder": bpy.ops.mesh.primitive_cylinder_add, "cone": bpy.ops.mesh.primitive_cone_add}
        if kind not in builders:
            raise ValueError(f"未知物体类型 {kind}")
        dims = tuple(positive(v) for v in vector(spec["size"]))
        builders[kind]()
        ob = bpy.context.object
        ob.name = name
        ob.users_collection[0].objects.unlink(ob)
        collection.objects.link(ob)
        # Bake the size so attached children keep metre-based coordinates.
        for v in ob.data.vertices:
            v.co = (v.co.x * dims[0] / 2, v.co.y * dims[1] / 2, v.co.z * dims[2] / 2)
        if spec.get("palette_role"):
            apply_palette(ob, spec["palette_role"])
        else:
            ob.data.materials.append(material(spec.get("color", "#989898")))
        ob.parent = parent
    ob.location = vector(spec.get("at", [0, 0, 0]))
    ob.rotation_mode = "QUATERNION"
    ob.rotation_quaternion = rotation(spec.get("rot", [0, 0, 0]))
    for i, part in enumerate(spec.get("parts", [])):
        prop(name + " · " + str(part.get("name", i + 1)), part, collection, ob)
    return ob


def preset(name):
    if name not in ("stand", "sit", "step.L", "step.R"):
        raise ValueError(f"未知动作 {name}")
    angles = dict.fromkeys(JOINTS, (0, 0, 0))
    for side in ("L", "R"):
        swing = 0 if name == "stand" else -78 if name == "sit" else -25 if name.endswith(side) else 22
        knee = 84 if name == "sit" else 18 if name.startswith("step") and swing > 0 else 0
        angles["thigh." + side] = (swing, 0, 0)
        angles["shin." + side] = (knee, 0, 0)
    return angles


def pose(parts, entry, f, first):
    angles = preset(entry.get("preset", "stand")) if first or "preset" in entry else {}
    angles.update(entry.get("pose", {}))
    for joint, degrees in angles.items():
        if joint not in parts:
            raise ValueError(f"未知关节 {joint}")
        quat = rotation(degrees)
        if joint.startswith("upper_arm."):
            quat = quat @ Quaternion((0, 1, 0), RAD(57 if joint.endswith(".L") else -57))
        key_rotation(parts[joint], quat, f)


def camera_for(shot, collection, scene):
    start, end = shot["start"], shot["end"]
    data = bpy.data.cameras.new(f"Lens {start}")
    cam = new_object(f"Camera {start}", data, collection)
    data.sensor_fit, data.sensor_width, data.clip_start, data.clip_end = "HORIZONTAL", 36, .01, 3000
    eye = aim = None
    lens, roll = 42, 0
    for f, k in keys(shot["camera"], start, end):
        eye = Vector(vector(k.get("eye", eye[:] if eye is not None else None)))
        aim = Vector(vector(k.get("aim", aim[:] if aim is not None else None)))
        if (aim - eye).length < 1e-6:
            raise ValueError("相机位置与注视点重合")
        lens, roll = positive(k.get("lens", lens)), numeric(k.get("roll", roll))
        cam.location = eye
        cam.keyframe_insert(data_path="location", frame=f)
        key_rotation(cam, (aim - eye).to_track_quat("-Z", "Y") @ Quaternion((0, 0, 1), RAD(roll)), f)
        data.lens = lens
        data.keyframe_insert(data_path="lens", frame=f)
    scene.timeline_markers.new(f"F{start} · {shot['description'][:40]}", frame=start).camera = cam
    return cam


def view_extents(cam, scene):
    corners = cam.data.view_frame(scene=scene)
    return (max(abs(p.x / p.z) for p in corners), max(abs(p.y / p.z) for p in corners))


def screen_location(entry, cam, scene, height):
    u, v = vector(entry["uv"], 2)
    x, y = view_extents(cam, scene)
    if ("screen_h" in entry) == ("depth" in entry):
        raise ValueError("uv 需要填写 screen_h 或 depth 中的一项")
    depth = (positive(height) / (2 * y * positive(entry["screen_h"])) if "screen_h" in entry
             else positive(entry["depth"]))
    point = cam.matrix_world @ Vector(((2 * u - 1) * x * depth, (1 - 2 * v) * y * depth, -depth))
    return point - UP * (numeric(entry.get("anchor", 0)) * height)


def film_bars(shot, cam, collection, scene):
    top, bottom = vector(shot.get("bars", [0, 0]), 2)
    if min(top, bottom) < 0 or top + bottom >= 1:
        raise ValueError("上下黑边的比例之和必须小于 1")
    for label, amount, sign in (("top", top, 1), ("bottom", bottom, -1)):
        if not amount:
            continue
        mesh = bpy.data.meshes.new(label)
        inner = sign * (1 - 2 * amount)
        mesh.from_pydata([(-2, inner, -1), (2, inner, -1), (2, sign * 2, -1), (-2, sign * 2, -1)],
                         [], [(0, 1, 2, 3)])
        ob = new_object(f"Matte {label} {shot['start']}", mesh, collection, cam)
        ob["film_matte"] = True
        mat = bpy.data.materials.new("Camera black")
        mat.use_nodes = True
        bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        mat.node_tree.nodes.remove(bsdf)  # Unshaded, perfectly black camera-only gate.
        mesh.materials.append(mat)
        for ray in ("shadow", "diffuse", "glossy", "transmission", "volume_scatter"):
            setattr(ob, "visible_" + ray, False)
        for f, _ in keys(shot["camera"], shot["start"], shot["end"]):
            scene.frame_set(f)
            x, y = view_extents(cam, scene)
            ob.scale = (.02 * x, .02 * y, .02)
            ob.keyframe_insert(data_path="scale", frame=f)


def build_shot(shot, definitions, scene, library):
    start, end = shot["start"], shot["end"]
    collection = bpy.data.collections.new(f"F{start}–{end}")
    scene.collection.children.link(collection)
    cam = camera_for(shot, collection, scene)
    roots, joints = {}, {}
    for name in shot["tracks"]:
        spec = definitions[name]
        if spec["kind"] == "actor":
            roots[name], joints[name] = actor(name, spec, collection, library)
        else:
            roots[name] = prop(name, spec, collection)
    for name, track in shot["tracks"].items():
        ob, spec = roots[name], definitions[name]
        parent = spec.get("parent")
        if parent:
            owner, sep, joint = parent.partition(":")
            target = joints[owner][joint] if sep else roots[owner]
            if target == ob or target in ob.children_recursive:
                raise ValueError(f"循环挂接 {name}")
            ob.parent = target
        height = positive(spec.get("height", spec.get("size", [1, 1, 1.72 if name in joints else 1])[2]))
        for f, entry in keys(track, start, end):
            if "uv" in entry:
                if parent or "at" in entry:
                    raise ValueError("画面坐标只能用于无父级物体，并与 at 择一")
                scene.frame_set(f)
                ob.location = screen_location(entry, cam, scene, height)
                ob.keyframe_insert(data_path="location", frame=f)
            elif "at" in entry or f == start:
                ob.location = vector(entry.get("at", spec.get("at", [0, 0, 0])))
                ob.keyframe_insert(data_path="location", frame=f)
            if "rot" in entry or f == start:
                key_rotation(ob, rotation(entry.get("rot", spec.get("rot", [0, 0, 0]))), f)
            if name in joints:
                pose(joints[name], entry, f, f == start)
            elif "pose" in entry or "preset" in entry:
                raise ValueError(f"{name} 需要 actor 类型才能调整关节")
    film_bars(shot, cam, collection, scene)
    return cam, roots, joints


def compile_plan(stage, preview):
    load = lambda path: json.loads(path.read_text(encoding="utf-8"))
    context = load(stage / "draft-context.json")
    timeline, plan = load(stage / context["timeline"]), load(stage / "shot-plan.json")
    definitions = plan["objects"]
    fixed_palette = context.get("palette_version") == 1
    if fixed_palette:
        definitions = {name: palette_spec(name, spec) for name, spec in definitions.items()}
    count = len(timeline["pts"])
    scene = bpy.context.scene
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    scene.timeline_markers.clear()
    scene.frame_start, scene.frame_end = 1, count
    scene.render.resolution_x, scene.render.resolution_y = timeline["width"], timeline["height"]
    scene.render.resolution_percentage = 100
    scene.render.pixel_aspect_x = scene.render.pixel_aspect_y = 1
    fps = Fraction(timeline["fps"])
    scene.render.fps = max(1, min(32767, round(fps)))
    scene.render.fps_base = float(scene.render.fps / fps)
    scene.render.engine = "CYCLES"
    scene.cycles.samples, scene.cycles.max_bounces = 4, 3
    scene.cycles.use_denoising = True
    scene.world = bpy.data.worlds.new("Background")
    scene.world.use_nodes = True
    background = PALETTE["background"][1] if fixed_palette else plan.get("background", "#aaaaaa")
    next(n for n in scene.world.node_tree.nodes if n.type == "BACKGROUND").inputs[0].default_value = color(background)
    sun_data = bpy.data.lights.new("Key", "SUN")
    sun_data.energy, sun_data.angle = 2.2, RAD(16)
    sun = new_object("Key", sun_data, scene.collection)
    sun.rotation_euler = (RAD(34), RAD(-22), RAD(-32))
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    meta, cast, joints, cameras = [], {}, {}, {}
    next_frame = 1
    for shot in plan["shots"]:
        start, end, desc = shot["start"], shot["end"], shot["description"]
        if (type(start) is not int or type(end) is not int or start != next_frame or not start <= end <= count
                or not isinstance(desc, str) or not desc.strip()):
            raise ValueError(f"镜头表必须从第 {next_frame} 帧连续覆盖，且有画面描述")
        cameras[start], cast[start], joints[start] = build_shot(shot, definitions, scene, stage / context["model"])
        meta.append(dict(start_frame=start, end_frame=end, description=desc))
        next_frame = end + 1
    if next_frame != count + 1:
        raise ValueError(f"镜头表只覆盖到第 {next_frame - 1} 帧，原片有 {count} 帧")
    scene.camera = cameras[1]
    scene.frame_set(1)
    shot_collections = {start: cam.users_collection[0] for start, cam in cameras.items()}
    original_actors = capture(scene) if fixed_palette else None
    detail = stage / "scene_details.py"
    if detail.is_file():
        runpy.run_path(str(detail), init_globals=dict(bpy=bpy, scene=scene, cast=cast, joints=joints,
                                                      cameras=cameras, shot_collections=shot_collections,
                                                      apply_palette=apply_palette))
    if original_actors is not None:
        require_cycles(scene.render.engine)
        assert_unchanged(scene, original_actors)
    for shot in plan["shots"]:
        start = shot["start"]
        scene.frame_set(start)
        owned = set(shot_collections[start].all_objects)
        for root in cast[start].values():
            owned.update(root.children_recursive)
        meshes = [ob for ob in owned if ob.type == "MESH"]
        if not any(not ob.get("film_matte") for ob in meshes):
            raise ValueError(f"镜头 F{start} 缺少场景几何，可把补建资产挂在本镜根物体或放入镜头集合")
        for ob in meshes:
            if fixed_palette and not ob.get("film_matte") and ROLE_PROPERTY not in ob:
                raise ValueError(f"{ob.name} 未指定配色，请用 apply_palette 设置物体类别")
            span(ob, start, shot["end"], count)
    scene.frame_set(1)
    bpy.data.orphans_purge(do_recursive=True)
    if not fixed_palette:
        bpy.ops.file.pack_all()
    bpy.context.preferences.filepaths.save_version = 0
    with staging_directory(stage, prefix=".compile-") as folder:
        if "FINISHED" not in bpy.ops.wm.save_as_mainfile(filepath=str(folder / "scene.blend"), compress=True):
            raise RuntimeError("无法保存 Blender 工程")
        (folder / "meta.jsonl").write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in meta), encoding="utf-8")
        (folder / ".draft-inputs").write_text(inputs_hash(stage), encoding="ascii")
        output = ["scene.blend", "meta.jsonl", ".draft-inputs"]
        if original_actors is not None:
            (folder / ACTOR_GEOMETRY).write_text(json.dumps(original_actors, ensure_ascii=False), encoding="utf-8")
            output.append(ACTOR_GEOMETRY)
        for name in output:
            (folder / name).replace(stage / name)
    if preview:
        out = stage / "draft-preview"
        out.mkdir(exist_ok=True)
        scene.render.resolution_percentage = max(1, min(100, round(48000 / max(timeline["width"], timeline["height"]))))
        scene.cycles.samples = 2
        scene.render.image_settings.file_format = "PNG"
        for f in sorted(set(preview)):
            if not 1 <= f <= count:
                raise ValueError(f"预览帧 {f} 超过片长")
            scene.frame_set(f)
            scene.render.filepath = str(out / f"{f:04d}.png")
            if "FINISHED" not in bpy.ops.render.render(write_still=True):
                raise RuntimeError(f"首版预览 F{f} 渲染失败")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", type=Path)
    parser.add_argument("--preview", type=int, nargs="+", default=[])
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
    try:
        compile_plan(args.stage.resolve(), args.preview)
    except EngineeringValidationError:
        raise
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise EngineeringValidationError(f"镜头表或补建脚本存在输入错误：{exc}") from exc
