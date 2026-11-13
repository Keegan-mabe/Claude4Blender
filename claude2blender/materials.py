"""Material helpers shared by Generate, Apply and Preview.

`assign_material` is handed to Claude's code so targeting is exact and the same
every time:

* Object targets (Object Mode): the material replaces the materials of the
  selected objects only. If an object shares its mesh with objects that are not
  selected (linked duplicates), its slots are switched to object-level links so
  the other objects keep their materials.
* Face targets (Edit Mode): the material is added as a slot if needed and only
  the selected faces get it. Every other face keeps its slot and material. A
  mesh with no materials first gets an empty slot 0, so unselected faces stay
  exactly as they were.

Neither path touches vertices, UVs, modifiers, scale or dimensions.
"""

import bpy

from . import bridge

_DESCRIBE_TYPES = {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"}
_NODE_SKIP = {"rna_type", "name", "label", "location", "width", "width_hidden", "height", "dimensions",
              "select", "show_options", "show_preview", "show_texture", "hide", "mute", "use_custom_color",
              "color", "parent", "bl_idname", "bl_label", "bl_description", "bl_icon", "bl_static_type",
              "bl_width_default", "bl_width_min", "bl_width_max", "bl_height_default", "bl_height_min",
              "bl_height_max", "type", "internal_links", "inputs", "outputs", "is_active_output", "warning_propagation",
              "color_tag"}


class TargetNotes(list):
    """Warnings collected while assigning; shown to the user after Apply/Preview."""


def _val(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (int, bool, str)) or v is None:
        return v
    if hasattr(v, "name"):
        return v.name
    try:
        return [_val(x) for x in v]
    except TypeError:
        return str(v)


def describe_material(mat, max_nodes=40):
    """Compact, JSON-safe description of a material's node tree (for follow-ups)."""
    if mat is None:
        return None
    out = {"name": mat.name, "users": mat.users, "nodes": [], "links": []}
    nt = mat.node_tree if mat.use_nodes else None
    if nt is None:
        out["diffuse_color"] = _val(mat.diffuse_color)
        out["roughness"] = _val(mat.roughness)
        out["metallic"] = _val(mat.metallic)
        return out
    for node in list(nt.nodes)[:max_nodes]:
        entry = {"name": node.name, "type": node.bl_idname}
        props = {}
        for p in node.bl_rna.properties:
            if p.identifier in _NODE_SKIP or p.is_readonly or p.type not in _DESCRIBE_TYPES:
                continue
            try:
                props[p.identifier] = _val(getattr(node, p.identifier))
            except Exception:
                pass
        if getattr(node, "image", None) is not None:
            props["image"] = node.image.filepath or node.image.name
        if props:
            entry["props"] = props
        inputs = {}
        for sock in node.inputs:
            if sock.is_linked or not hasattr(sock, "default_value") or not sock.enabled:
                continue
            inputs[sock.name] = _val(sock.default_value)
        if inputs:
            entry["inputs"] = inputs
        out["nodes"].append(entry)
    for link in nt.links:
        out["links"].append(f"{link.from_node.name}.{link.from_socket.name} -> {link.to_node.name}.{link.to_socket.name}")
    return out


def target_materials(request):
    """Materials currently on the targets: on the selected faces, or on the selected objects."""
    seen = {}
    sel = request["selection"]
    face_objects = {f["object"]: f for f in sel["faces"]}
    if face_objects:
        for name, face in face_objects.items():
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            for idx in face.get("material_indices_in_use", []):
                if idx < len(obj.material_slots) and obj.material_slots[idx].material:
                    mat = obj.material_slots[idx].material
                    seen[mat.name] = mat
    else:
        for info in sel["objects"]:
            obj = bpy.data.objects.get(info["name"])
            if obj is None:
                continue
            for slot in obj.material_slots:
                if slot.material:
                    seen[slot.material.name] = slot.material
    return [describe_material(m) for m in list(seen.values())[:6]]


def _mesh_shared_outside(me, targets):
    return [o.name for o in bpy.data.objects if o.data == me and o.name not in targets]


def assign_material(mat, request, notes=None):
    """Put `mat` on exactly the request's targets. Returns a list of notes (warnings)."""
    notes = TargetNotes() if notes is None else notes
    sel = request["selection"]
    if sel["faces"]:
        targets = {f["object"] for f in sel["faces"]}
        for face in sel["faces"]:
            obj = bpy.data.objects.get(face["object"])
            if obj is None or obj.type != "MESH":
                notes.append(f"{face['object']}: not found")
                continue
            me = obj.data
            shared = _mesh_shared_outside(me, targets)
            if shared:
                notes.append(f"{obj.name} shares its mesh with {', '.join(shared[:3])}; "
                             "the same faces change there too")
            if len(me.materials) == 0:
                me.materials.append(None)          # keep unselected faces on "no material"
            index = next((i for i, m in enumerate(me.materials) if m == mat), None)
            if index is None:
                me.materials.append(mat)
                index = len(me.materials) - 1
            faces = bridge.expand_indices(face["faces"])
            polys = me.polygons
            for i in faces:
                if i < len(polys):
                    polys[i].material_index = index
            if face.get("faces_truncated"):
                notes.append(f"{obj.name}: very large face selection; only the first faces were sent")
            me.update()
        return notes

    names = [o["name"] for o in sel["objects"]]
    targets = set(names)
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None or not hasattr(obj.data, "materials"):
            continue
        shared = _mesh_shared_outside(obj.data, targets) if obj.data is not None else []
        if not obj.material_slots:
            if shared:
                notes.append(f"{obj.name} shares its mesh with {', '.join(shared[:3])}; they get this material too")
            obj.data.materials.append(mat)
            continue
        for slot in obj.material_slots:
            if shared:
                slot.link = "OBJECT"           # override on this object only
            slot.material = mat
    if not names:
        notes.append("Nothing was selected, so the material was created but not assigned")
    return notes


def hex_to_srgb(hex_str):
    h = (hex_str or "").strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return None
