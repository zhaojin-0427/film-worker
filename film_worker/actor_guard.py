"""Record geometry and appearance that belong to the supplied human model."""
import hashlib
import json
import sys
from pathlib import Path

if __package__:
    from .engineering_checks import EngineeringValidationError
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from engineering_checks import EngineeringValidationError

ACTOR_GEOMETRY = ".actor-geometry.json"
BODY_PROPERTY = "movie_actor_body"
RENDERABLE = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "GREASEPENCIL", "VOLUME"}


def geometry(scene):
    meshes = {}
    actors = {}
    for obj in scene.objects:
        if obj.type != "MESH" or not obj.get(BODY_PROPERTY):
            continue
        mesh = obj.data
        if mesh not in meshes:
            data = (
                [tuple(vertex.co) for vertex in mesh.vertices],
                [(tuple(face.vertices), face.material_index, face.use_smooth) for face in mesh.polygons],
            )
            meshes[mesh] = hashlib.sha256(json.dumps(data).encode()).hexdigest()
        actors[obj.name] = [meshes[mesh], list(obj.scale), list(obj.modifiers.keys()),
                            len(mesh.shape_keys.key_blocks) if mesh.shape_keys else 0]
    return actors


def material_signature(material):
    if material is None:
        return None
    def socket_value(socket):
        value = getattr(socket, "default_value", None)
        if value is None or isinstance(value, (str, bool, float, int)):
            return value
        if hasattr(value, "name"):
            return value.name
        return list(value)

    def graph(tree):
        if tree is None:
            return None
        nodes = [
            [node.name, node.bl_idname, node.mute, getattr(node, "operation", None),
             getattr(node, "blend_type", None), graph(getattr(node, "node_tree", None)),
             [socket_value(socket) for socket in node.inputs]] for node in tree.nodes
        ]
        links = sorted([link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier]
                       for link in tree.links)
        return [nodes, links]

    data = [list(material.diffuse_color), material.use_nodes, graph(material.node_tree if material.use_nodes else None)]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


def capture(scene):
    bodies = geometry(scene)
    appearance, attachments = {}, {}
    members = set()
    materials = {}
    for name in bodies:
        obj = scene.objects[name]
        slots = []
        for slot in obj.material_slots:
            if slot.material not in materials:
                materials[slot.material] = material_signature(slot.material)
            slots.append([slot.link, materials[slot.material]])
        appearance[name] = [obj.parent.name if obj.parent else None, obj.get("movie_palette_role"), slots]
        members.add(obj)
        if obj.parent and obj.parent.type in {"EMPTY", "ARMATURE"}:
            members.add(obj.parent)
    for obj in scene.objects:
        if obj.type not in RENDERABLE or obj.name in bodies:
            continue
        parent = obj.parent
        while parent and parent not in members:
            parent = parent.parent
        targets = sorted({target.name for constraint in obj.constraints
                          if (target := getattr(constraint, "target", None)) in members})
        if parent or targets:
            attachments[obj.name] = [obj.parent.name if parent else None, targets]
    scales = {obj.name: [list(obj.scale), list(obj.delta_scale)] for obj in members}
    return dict(schema_version=2, geometry=bodies, appearance=appearance, attachments=attachments, scales=scales)


def assert_unchanged(scene, original):
    tracked = original["geometry"] if original.get("schema_version") == 2 else original
    current = geometry(scene)
    for name in sorted(set(tracked) | set(current)):
        if current.get(name) != tracked.get(name):
            raise EngineeringValidationError(f"{name} 的人体白模几何、比例或表面分区已改变，请恢复模型，仅修正动作")
    if original.get("schema_version") == 2:
        actual = capture(scene)
        if actual["appearance"] != original["appearance"]:
            raise EngineeringValidationError("人体白模的材质、配色或部件层级已改变，请恢复基础白模")
        if actual["scales"] != original["scales"]:
            raise EngineeringValidationError("人体白模的比例已改变，请恢复基础白模并用位置或镜头调整大小")
        if actual["attachments"] != original["attachments"]:
            raise EngineeringValidationError("人体白模的挂接部件已改变，请移除擅自添加的外观部件并恢复既有道具")


if __name__ == "__main__":
    import bpy

    snapshot = Path(sys.argv[sys.argv.index("--") + 1])
    original = json.loads(snapshot.read_text(encoding="utf-8"))
    assert_unchanged(bpy.context.scene, original)
    snapshot.write_text(json.dumps(capture(bpy.context.scene), ensure_ascii=False), encoding="utf-8")
