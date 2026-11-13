"""Change history: targeted Undo / Redo of each Apply, independent of Claude.

Every Apply records exactly what it changed, as before/after values:

* object transforms, visibility and material slots
* mesh material lists and per-face material indices
* materials and worlds edited in place (kept as a hidden backup copy that is
  swapped in and out, so node trees are restored exactly)
* light and camera settings, the scene's world, camera and colour management
* datablocks the change created (objects are unlinked on Undo, relinked on Redo)
* mesh geometry, only when Preserve Geometry is off (hidden mesh backup)

Undo writes the "before" values back and Redo writes the "after" values, for
the recorded items only, so unrelated objects and anything you changed by hand
in between are left alone. Redo never re-runs Claude's code; "Apply Again"
does, on purpose, as a new history entry.

Blender's own Ctrl+Z keeps working: Undo/Redo/Go To are normal undo steps, and
after any native undo/redo the history re-checks which entries are in effect.
"""

import time
from array import array
from hashlib import blake2b

import bpy
from bpy.app.handlers import persistent

MAX_ENTRIES = 10
BACKUP_PREFIX = ".c2b"

CREATABLE = ("objects", "materials", "worlds", "lights", "cameras", "images",
             "node_groups", "meshes", "textures", "collections", "curves")
SWAPPABLE = ("materials", "worlds")
SCENE_ROOT = "<scene>"

APPLIED, UNDONE, ALT = "APPLIED", "UNDONE", "ALT"


class HistoryError(Exception):
    pass


_entries = []      # oldest first
_next_id = 1


# ---------------------------------------------------------------- helpers

def _val(v):
    if isinstance(v, float):
        return round(v, 6)
    if hasattr(v, "name_full"):
        return ("ID", v.name_full)
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    try:
        return tuple(_val(x) for x in v)
    except TypeError:
        return repr(v)


_SKIP_PROPS = {"rna_type", "name", "name_full", "is_evaluated", "original", "users", "use_fake_user",
               "use_extra_user", "is_embedded_data", "is_missing", "is_runtime_data", "tag",
               "is_library_indirect", "library", "library_weak_reference", "asset_data",
               "override_library", "preview", "session_uid", "id_type", "select", "dimensions",
               "location", "width", "height", "is_editor_only"}


def simple_props(rna_obj):
    out = {}
    for p in rna_obj.bl_rna.properties:
        ident = p.identifier
        if ident in _SKIP_PROPS or p.is_readonly:
            continue
        if p.type in {"BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"} or (
                p.type == "POINTER" and getattr(p, "fixed_type", None) is not None
                and isinstance(getattr(rna_obj, ident, None), bpy.types.ID)):
            try:
                out[ident] = _val(getattr(rna_obj, ident))
            except Exception:
                pass
    return out


def node_signature(idb):
    h = blake2b(digest_size=16)
    h.update(repr(sorted(simple_props(idb).items())).encode())
    nt = getattr(idb, "node_tree", None)
    if nt is not None:
        for n in sorted(nt.nodes, key=lambda n: n.name):
            h.update(f"{n.name}|{n.bl_idname}|{sorted(simple_props(n).items())}".encode())
            for s in n.inputs:
                if hasattr(s, "default_value"):
                    h.update(repr(_val(s.default_value)).encode())
        for link in nt.links:
            h.update(f"{link.from_node.name}.{link.from_socket.identifier}>"
                     f"{link.to_node.name}.{link.to_socket.identifier}".encode())
    return h.hexdigest()


def geometry_signature(me):
    co = array("f", [0.0]) * (len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    loops = array("i", [0]) * len(me.loops)
    me.loops.foreach_get("vertex_index", loops)
    h = blake2b(co.tobytes(), digest_size=16)
    h.update(loops.tobytes())
    for layer in me.uv_layers:  # UVs count as geometry: CAD unwraps must survive
        uv = array("f", [0.0]) * (len(layer.data) * 2)
        layer.data.foreach_get("uv", uv)
        h.update(layer.name.encode())
        h.update(uv.tobytes())
    return h.hexdigest()


def _material_indices(me):
    idx = array("i", [0]) * len(me.polygons)
    me.polygons.foreach_get("material_index", idx)
    return idx.tobytes()


def _constraints_sig(obj):
    return tuple((c.name, c.type, getattr(getattr(c, "target", None), "name", None), c.mute)
                 for c in obj.constraints)


def _object_state(obj):
    # Transforms are kept exact (not rounded): Undo must give back the very same floats.
    return {
        "parent": obj.parent.name if obj.parent else None,
        "matrix_parent_inverse": tuple(tuple(row) for row in obj.matrix_parent_inverse),
        "location": tuple(obj.location),
        "rotation_mode": obj.rotation_mode,
        "rotation_euler": tuple(obj.rotation_euler),
        "rotation_quaternion": tuple(obj.rotation_quaternion),
        "rotation_axis_angle": tuple(obj.rotation_axis_angle),
        "scale": tuple(obj.scale),
        "hide_viewport": obj.hide_viewport,
        "hide_render": obj.hide_render,
        "constraints": _constraints_sig(obj),
        "slots": tuple((s.link, s.material.name if s.material else None) for s in obj.material_slots),
        "data": obj.data.name if obj.data is not None else None,
    }


# Keys Undo/Redo can't write back; a change to them is reported, and the guard blocks it.
UNPATCHABLE_OBJECT_KEYS = ("constraints",)


def data_state(item):
    """Light / camera settings, including the camera's depth of field."""
    out = simple_props(item)
    dof = getattr(item, "dof", None)
    if dof is not None and isinstance(item, bpy.types.Camera):
        for k, v in simple_props(dof).items():
            out["dof." + k] = v
    return out


def _mesh_state(me):
    return {"materials": tuple(m.name if m else None for m in me.materials), "indices": _material_indices(me)}


def _scene_state(scene):
    vs = scene.view_settings
    r = scene.render
    out = {
        "world": scene.world.name if scene.world else None,
        "camera": scene.camera.name if scene.camera else None,
        "view_transform": vs.view_transform,
        "look": vs.look,
        "exposure": round(vs.exposure, 6),
        "gamma": round(vs.gamma, 6),
        "film_transparent": r.film_transparent,
        "render.engine": r.engine,
        "render.resolution_x": r.resolution_x,
        "render.resolution_y": r.resolution_y,
        "render.resolution_percentage": r.resolution_percentage,
        "render.pixel_aspect_x": round(r.pixel_aspect_x, 6),
        "render.pixel_aspect_y": round(r.pixel_aspect_y, 6),
    }
    # Engine settings an environment change may touch (soft shadows, samples, bloom, AO ...).
    for prefix in ("eevee", "cycles"):
        group = getattr(scene, prefix, None)
        if group is not None:
            for k, v in simple_props(group).items():
                out[f"{prefix}.{k}"] = v
    return out


def _users_collections(obj, scene):
    names = []
    for coll in obj.users_collection:
        names.append(SCENE_ROOT if coll == scene.collection else coll.name)
    return names


def _diff(before, after):
    keys = [k for k in after if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in keys}, {k: after[k] for k in keys}


# ---------------------------------------------------------------- capture

class Pending:
    """State captured just before Claude's code runs."""

    def __init__(self, preserve_geometry):
        scene = bpy.context.scene
        self.scene_name = scene.name
        self.preserve_geometry = preserve_geometry
        self.ids = {c: {i.name for i in getattr(bpy.data, c)} for c in CREATABLE}
        self.objects = {o.name: _object_state(o) for o in bpy.data.objects}
        self.meshes = {m.name: _mesh_state(m) for m in bpy.data.meshes}
        self.geometry = {} if preserve_geometry else {m.name: geometry_signature(m) for m in bpy.data.meshes}
        self.sigs = {c: {i.name: node_signature(i) for i in getattr(bpy.data, c)} for c in SWAPPABLE}
        self.datas = {c: {i.name: data_state(i) for i in getattr(bpy.data, c)} for c in ("lights", "cameras")}
        self.scene = _scene_state(scene)
        # Hidden copies of everything that could be edited in place.
        self.backups = {c: {} for c in SWAPPABLE + ("meshes",)}
        for c in SWAPPABLE:
            for item in list(getattr(bpy.data, c)):
                self.backups[c][item.name] = self._backup(item)
        if not preserve_geometry:
            for me in list(bpy.data.meshes):
                self.backups["meshes"][me.name] = self._backup(me)

    @staticmethod
    def _backup(idb):
        copy = idb.copy()
        copy.name = f"{BACKUP_PREFIX}~{idb.name}"[:63]
        copy.use_fake_user = True
        return copy.name

    def discard(self):
        for c, mapping in self.backups.items():
            coll = getattr(bpy.data, c)
            for bname in mapping.values():
                item = coll.get(bname)
                if item is not None:
                    coll.remove(item)
        self.backups = {}


def commit(pending, code, summary, request, preview=False):
    """Record what the Apply changed. Returns the new entry.

    With preview=True the entry is kept aside as the live preview instead of
    joining the history; end_preview() later keeps it (Apply) or reverts it.
    """
    entry = build_entry(pending, code, summary, request)
    if preview:
        global _preview
        entry["preview"] = True
        _preview = entry
        sync_ui()
        return entry
    _append(entry)
    return entry


def discard_change(entry):
    """Take back a change that was never recorded (e.g. blocked by the permission guard)."""
    revert(entry)
    _drop_patch(entry)


def build_entry(pending, code, summary, request):
    """What the code changed, as an entry that can be reverted and re-applied. Not stored."""
    global _next_id
    scene = bpy.data.scenes.get(pending.scene_name) or bpy.context.scene
    backup_names = {b for m in pending.backups.values() for b in m.values()}
    entry = {
        "id": _next_id,
        "summary": summary,
        "code": code,
        "request": request,
        "time": time.strftime("%H:%M:%S"),
        "scene": scene.name,
        "state": APPLIED,
        "patch_ok": True,
        "warnings": [],
        "created": {},
        "created_links": {},
        "objects": {},
        "meshes": {},
        "swaps": {},
        "datas": {},
        "scene_props": None,
    }
    _next_id += 1

    for c in CREATABLE:
        now = {i.name for i in getattr(bpy.data, c)}
        new = sorted(now - pending.ids[c] - backup_names)
        if new:
            entry["created"][c] = new
        gone = pending.ids[c] - now
        if gone:
            entry["warnings"].append(f"removed {c}: {', '.join(sorted(gone)[:3])}")
            entry["patch_ok"] = False
    for name in entry["created"].get("objects", []):
        entry["created_links"][name] = _users_collections(bpy.data.objects[name], scene)
    entry["created_collections"] = {}
    for name in entry["created"].get("collections", []):
        parents = [SCENE_ROOT if p == scene.collection else p.name
                   for p in [scene.collection] + list(bpy.data.collections) if name in p.children]
        entry["created_collections"][name] = parents

    # In-place edits of materials / worlds / (geometry, when unprotected): keep backup, swap later.
    for c in SWAPPABLE + ("meshes",):
        coll = getattr(bpy.data, c)
        for name, bname in pending.backups.get(c, {}).items():
            item, backup = coll.get(name), coll.get(bname)
            if item is None or backup is None:
                continue
            if c == "meshes":
                changed = geometry_signature(item) != pending.geometry[name]
            else:
                changed = node_signature(item) != pending.sigs[c][name]
            if changed:
                entry["swaps"].setdefault(c, {})[name] = {
                    "backup": bname,
                    "fake": item.use_fake_user,
                    "after": node_signature(item) if c != "meshes" else geometry_signature(item),
                    "before": pending.sigs[c][name] if c != "meshes" else pending.geometry[name],
                }
            else:
                coll.remove(backup)

    swapped_meshes = set(entry["swaps"].get("meshes", {}))
    for name, before in pending.objects.items():
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        b, a = _diff(before, _object_state(obj))
        b.pop("data", None), a.pop("data", None)
        if a:
            entry["objects"][name] = (b, a)
    for name, before in pending.meshes.items():
        me = bpy.data.meshes.get(name)
        if me is None or name in swapped_meshes:
            continue
        b, a = _diff(before, _mesh_state(me))
        if a:
            entry["meshes"][name] = (before, _mesh_state(me))
    for c, items in pending.datas.items():
        for name, before in items.items():
            item = getattr(bpy.data, c).get(name)
            if item is None:
                continue
            b, a = _diff(before, data_state(item))
            if a:
                entry["datas"].setdefault(c, {})[name] = (b, a)
    b, a = _diff(pending.scene, _scene_state(scene))
    if a:
        entry["scene_props"] = (b, a)
    for name, (b, a) in entry["objects"].items():
        if any(k in a for k in UNPATCHABLE_OBJECT_KEYS):
            entry["warnings"].append(f"constraints changed on {name}")
    entry["changes"] = describe(entry, pending)
    return entry


_TRANSFORM_KEYS = ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "rotation_mode")
_LABELS = {"view_transform": "view transform", "look": "contrast look", "exposure": "exposure",
           "gamma": "gamma", "film_transparent": "transparent film", "render.engine": "render engine",
           "render.resolution_x": "resolution", "render.resolution_y": "resolution",
           "render.resolution_percentage": "resolution %"}


def _names(names, n=3):
    names = list(names)
    return ", ".join(names[:n]) + (f" +{len(names) - n}" if len(names) > n else "")


def describe(entry, pending=None):
    """Review & Apply summary: [(icon, text)] grouped by lights, camera, world, backdrop, objects, render."""
    out = []
    created_objs = [bpy.data.objects.get(n) for n in entry["created"].get("objects", [])]
    created_objs = [o for o in created_objs if o is not None]
    by_type = {}
    for o in created_objs:
        by_type.setdefault(o.type, []).append(o.name)

    # Lights
    light_mod = set()
    for name, (b, a) in entry["objects"].items():
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "LIGHT":
            light_mod.add(name)
    for name in entry["datas"].get("lights", {}):
        light_mod.update(o.name for o in bpy.data.objects if o.type == "LIGHT" and o.data
                         and o.data.name == name and o.name not in by_type.get("LIGHT", []))
    if by_type.get("LIGHT"):
        out.append(("LIGHT_AREA", f"Lights created: {_names(by_type['LIGHT'])}"))
    if light_mod:
        out.append(("LIGHT", f"Lights modified: {_names(sorted(light_mod))}"))

    # Camera
    cam_bits = []
    sp = entry["scene_props"]
    if sp and "camera" in sp[1]:
        cam_bits.append(f"active camera {sp[0].get('camera') or 'none'} → {sp[1]['camera']}")
    if by_type.get("CAMERA"):
        cam_bits.append("created " + _names(by_type["CAMERA"]))
    moved_cams = [n for n in entry["objects"]
                  if (o := bpy.data.objects.get(n)) is not None and o.type == "CAMERA"]
    if moved_cams:
        cam_bits.append("moved " + _names(moved_cams))
    for name, (b, a) in entry["datas"].get("cameras", {}).items():
        keys = []
        if "lens" in a:
            keys.append(f"{b.get('lens', 0):.0f} → {a['lens']:.0f} mm")
        other = [k for k in a if k != "lens"]
        if other:
            keys.append(f"{len(other)} setting{'s' if len(other) != 1 else ''}")
        cam_bits.append(f"{name}: " + ", ".join(keys))
    if cam_bits:
        out.append(("CAMERA_DATA", "Camera: " + "; ".join(cam_bits)))

    # World
    world_bits = []
    if sp and "world" in sp[1]:
        world_bits.append(f"{sp[0].get('world') or 'none'} → {sp[1]['world']}")
    edited = list(entry["swaps"].get("worlds", {}))
    if edited:
        world_bits.append("edited " + _names(edited))
    if sp and "film_transparent" in sp[1]:
        world_bits.append("transparent background " + ("on" if sp[1]["film_transparent"] else "off"))
    if world_bits:
        out.append(("WORLD", "World: " + "; ".join(world_bits)))

    # Backdrop / other created objects
    others = [n for t, names in by_type.items() if t not in {"LIGHT", "CAMERA"} for n in names]
    if others:
        out.append(("MESH_PLANE", f"Backdrop/helpers created: {_names(others)}"))

    # Existing objects moved / rotated
    moved, rotated, follow = [], [], 0
    for name, (b, a) in entry["objects"].items():
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type in {"LIGHT", "CAMERA"}:
            continue
        if "location" in a:
            moved.append(name)
        if any(k in a for k in _TRANSFORM_KEYS[1:]):
            rotated.append(name)
        if "location" in a or any(k in a for k in _TRANSFORM_KEYS[1:]):
            follow += len([c for c in obj.children_recursive if c.name not in entry["objects"]])
    if moved:
        out.append(("ORIENTATION_GLOBAL", f"Moved: {_names(moved, 4)}"))
    if rotated:
        out.append(("ORIENTATION_GIMBAL", f"Rotated: {_names(rotated, 4)}"))
    if follow:
        out.append(("CON_CHILDOF", "1 child object follows its parent" if follow == 1
                    else f"{follow} child objects follow their parents"))

    # Materials (backdrop materials are created; existing ones edited in place)
    mats = entry["created"].get("materials", [])
    if mats:
        out.append(("MATERIAL", f"Materials created: {_names(mats)}"))
    edited_m = list(entry["swaps"].get("materials", {}))
    if edited_m:
        out.append(("MATERIAL", f"Materials edited: {_names(edited_m)}"))

    # Render / colour management
    if sp:
        keys = [k for k in sp[1] if k not in ("world", "camera", "film_transparent")]
        labels = sorted({_LABELS.get(k, k.split(".")[0] + " settings") for k in keys})
        if labels:
            text = ", ".join(labels)
            if "exposure" in sp[1]:
                text = text.replace("exposure", f"exposure {sp[0].get('exposure', 0):+.2f} → {sp[1]['exposure']:+.2f}")
            out.append(("SCENE", "Render: " + text))

    if entry["warnings"]:
        out.append(("ERROR", "; ".join(entry["warnings"])))
    if pending is not None:
        if pending.preserve_geometry:
            out.append(("LOCKED", "Geometry, UVs, scale and modifiers unchanged"))
        else:
            n = len(entry["swaps"].get("meshes", {}))
            out.append(("UNLOCKED", f"Geometry not protected · {n} mesh{'es' if n != 1 else ''} edited"))
    return out


def _append(entry):
    # A new change after Undo ends the redo branch: undone entries keep their code (Apply Again).
    for old in _entries:
        if old["state"] == UNDONE:
            _drop_patch(old)
    _entries.append(entry)
    while len(_entries) > MAX_ENTRIES:
        old = _entries.pop(0)
        if old["state"] != ALT:
            _drop_patch(old)
    sync_ui()


# ---------------------------------------------------------------- live preview

_preview = None


def preview_entry():
    return _preview


def showing_original():
    return _preview is not None and _preview["state"] == UNDONE


def compare_preview(original):
    """Flip the live preview between Original (before) and Proposed (after), exactly."""
    if _preview is None:
        raise HistoryError("Nothing is being previewed")
    if original and _preview["state"] == APPLIED:
        revert(_preview)
    elif not original and _preview["state"] == UNDONE:
        reapply(_preview)
    sync_ui()
    return _preview


def end_preview(keep):
    """Finish the live preview: keep=True moves it into History, False reverts it exactly."""
    global _preview
    entry = _preview
    if entry is None:
        return None
    if keep and entry["state"] == UNDONE:
        reapply(entry)  # Apply while showing Original: apply what was proposed
    _preview = None
    if keep:
        entry["preview"] = False
        _append(entry)
        return entry
    try:
        if entry["state"] == APPLIED:
            revert(entry)
    finally:
        entry["state"] = UNDONE
        _drop_patch(entry)
        sync_ui()
    return entry


def _hidden_items():
    """Datablocks only History/Preview keep alive (fake user): backups and undone created data."""
    items = []
    for entry in _entries + ([_preview] if _preview else []):
        for c, swaps in entry["swaps"].items():
            for info in swaps.values():
                items.append((c, info["backup"]))
        if entry["state"] == UNDONE:
            for c, names in entry["created"].items():
                if c != "meshes":
                    items.extend((c, n) for n in names)
    return items


_save_state = None


@persistent
def _on_save_pre(*_args):
    """Saved files never contain the preview or History's hidden backups."""
    global _save_state
    _save_state = {"preview": False, "fake": []}
    if _preview is not None and _preview["state"] == APPLIED:
        try:
            revert(_preview)
            _save_state["preview"] = True
        except HistoryError:
            pass
    for c, name in _hidden_items():
        item = getattr(bpy.data, c).get(name)
        if item is not None and item.use_fake_user:
            item.use_fake_user = False      # zero users: Blender skips it when writing
            _save_state["fake"].append((c, name))


@persistent
def _on_save_post(*_args):
    global _save_state
    st, _save_state = _save_state, None
    if not st:
        return
    for c, name in st["fake"]:
        item = getattr(bpy.data, c).get(name)
        if item is not None:
            item.use_fake_user = True
    if st["preview"] and _preview is not None:
        try:
            reapply(_preview)
        except HistoryError:
            pass


# ---------------------------------------------------------------- patching

def _need(coll, name):
    item = getattr(bpy.data, coll).get(name)
    if item is None:
        raise HistoryError(f"{name} ({coll[:-1]}) no longer exists")
    return item


def _swap(coll_name, name, info):
    coll = getattr(bpy.data, coll_name)
    live, other = coll.get(name), coll.get(info["backup"])
    if live is None or other is None:
        raise HistoryError(f"{name}: saved version is missing")
    live.user_remap(other)
    live.name = "__c2b_swap__"
    other.name = name
    live.name = info["backup"]
    other.use_fake_user = info["fake"]
    live.use_fake_user = True


def _set_object(obj, values):
    from mathutils import Matrix
    if "parent" in values:
        obj.parent = bpy.data.objects.get(values["parent"]) if values["parent"] else None
    if "matrix_parent_inverse" in values:
        obj.matrix_parent_inverse = Matrix(values["matrix_parent_inverse"])
    for key, value in values.items():
        if key in ("parent", "matrix_parent_inverse", "data") or key in UNPATCHABLE_OBJECT_KEYS:
            continue
        if key == "slots":
            for i, (link, mat_name) in enumerate(value):
                if i >= len(obj.material_slots):
                    break
                slot = obj.material_slots[i]
                slot.link = link
                slot.material = bpy.data.materials.get(mat_name) if mat_name else None
        elif key == "rotation_mode":
            obj.rotation_mode = value
        else:
            setattr(obj, key, value)


def _set_mesh(me, state):
    mats = [bpy.data.materials.get(n) if n else None for n in state["materials"]]
    if tuple(m.name if m else None for m in me.materials) != state["materials"]:
        me.materials.clear()
        for m in mats:
            me.materials.append(m)
    idx = array("i")
    idx.frombytes(state["indices"])
    if len(idx) == len(me.polygons):
        me.polygons.foreach_set("material_index", idx)
    me.update()


def _set_data(item, values):
    for key, value in values.items():
        target = item
        if key.startswith("dof."):
            target, key = item.dof, key[4:]
        try:
            if isinstance(value, tuple) and value and value[0] == "ID":
                if key == "focus_object":
                    target.focus_object = bpy.data.objects.get(value[1])
                continue
            setattr(target, key, value)
        except Exception:
            pass


def _set_scene(scene, values):
    # The engine first: its own settings groups follow it.
    for key in sorted(values, key=lambda k: k != "render.engine"):
        value = values[key]
        try:
            if key == "world":
                scene.world = bpy.data.worlds.get(value) if value else None
            elif key == "camera":
                scene.camera = bpy.data.objects.get(value) if value else None
            elif key == "film_transparent":
                scene.render.film_transparent = value
            elif "." in key:
                group, attr = key.split(".", 1)
                if isinstance(value, tuple) and value and value[0] == "ID":
                    continue
                setattr(scene.render if group == "render" else getattr(scene, group), attr, value)
            else:
                setattr(scene.view_settings, key, value)
        except Exception:
            pass


def _link_created(entry, scene, link):
    for name, parents in entry.get("created_collections", {}).items():
        coll = _need("collections", name)
        for pname in parents:
            parent = scene.collection if pname == SCENE_ROOT else bpy.data.collections.get(pname)
            if parent is None:
                continue
            if link and name not in parent.children:
                parent.children.link(coll)
            elif not link and name in parent.children:
                parent.children.unlink(coll)
        coll.use_fake_user = not link
    for name, colls in entry["created_links"].items():
        obj = _need("objects", name)
        if link:
            for cname in colls:
                coll = scene.collection if cname == SCENE_ROOT else bpy.data.collections.get(cname)
                if coll is not None and obj.name not in coll.objects:
                    coll.objects.link(obj)
            obj.use_fake_user = False
        else:
            obj.use_fake_user = True
            for coll in list(obj.users_collection):
                coll.objects.unlink(obj)
    for c, names in entry["created"].items():
        if c in ("objects", "collections"):
            continue
        if c == "meshes":
            continue  # kept alive by their (fake-user) objects
        for name in names:
            item = getattr(bpy.data, c).get(name)
            if item is not None:
                # While undone, nothing uses it any more: a fake user keeps it for Redo.
                item.use_fake_user = not link


def check_patchable(entry):
    if not entry["patch_ok"]:
        raise HistoryError("this change removed objects, so only Ctrl+Z can revert it")
    for c, swaps in entry["swaps"].items():
        for name, info in swaps.items():
            _need(c, name), _need(c, info["backup"])
    for c, names in entry["created"].items():
        for name in names:
            _need(c, name)


def revert(entry):
    check_patchable(entry)
    scene = bpy.data.scenes.get(entry["scene"]) or bpy.context.scene
    for c, swaps in entry["swaps"].items():
        for name, info in swaps.items():
            _swap(c, name, info)
    if entry["scene_props"]:
        _set_scene(scene, entry["scene_props"][0])
    for name, (before, _after) in entry["meshes"].items():
        me = bpy.data.meshes.get(name)
        if me is not None:
            _set_mesh(me, before)
    for name, (before, _after) in entry["objects"].items():
        obj = bpy.data.objects.get(name)
        if obj is not None:
            _set_object(obj, before)
    for c, items in entry["datas"].items():
        for name, (before, _after) in items.items():
            item = getattr(bpy.data, c).get(name)
            if item is not None:
                _set_data(item, before)
    _link_created(entry, scene, link=False)
    entry["state"] = UNDONE


def reapply(entry):
    check_patchable(entry)
    scene = bpy.data.scenes.get(entry["scene"]) or bpy.context.scene
    for c, swaps in entry["swaps"].items():
        for name, info in swaps.items():
            _swap(c, name, info)
    _link_created(entry, scene, link=True)
    if entry["scene_props"]:
        _set_scene(scene, entry["scene_props"][1])
    for name, (_before, after) in entry["meshes"].items():
        me = bpy.data.meshes.get(name)
        if me is not None:
            _set_mesh(me, after)
    for name, (_before, after) in entry["objects"].items():
        obj = bpy.data.objects.get(name)
        if obj is not None:
            _set_object(obj, after)
    for c, items in entry["datas"].items():
        for name, (_before, after) in items.items():
            item = getattr(bpy.data, c).get(name)
            if item is not None:
                _set_data(item, after)
    entry["state"] = APPLIED


def _remove_if_unused(coll_name, name):
    coll = getattr(bpy.data, coll_name)
    item = coll.get(name)
    if item is None:
        return
    item.use_fake_user = False
    if item.users == 0:
        coll.remove(item)


def _drop_patch(entry):
    """Forget how to undo/redo an entry and delete what only it was keeping alive. Code is kept."""
    if entry["state"] == UNDONE:
        # Inactive copies hold the "after" version; created data is unlinked.
        for name in entry["created"].get("objects", []):
            obj = bpy.data.objects.get(name)
            if obj is not None and not obj.users_collection:
                bpy.data.objects.remove(obj)
        for c, names in entry["created"].items():
            if c != "objects":
                for name in names:
                    _remove_if_unused(c, name)
    for c, swaps in entry["swaps"].items():
        for info in swaps.values():
            _remove_if_unused(c, info["backup"])
    entry["swaps"] = {}
    entry["state"] = ALT if entry["state"] == UNDONE else entry["state"]
    entry["patch_ok"] = False


# ---------------------------------------------------------------- navigation

def entries():
    return _entries


def find(entry_id):
    for e in _entries:
        if e["id"] == entry_id:
            return e
    return None


def undo_last():
    for entry in reversed(_entries):
        if entry["state"] == APPLIED:
            revert(entry)
            return entry
    raise HistoryError("Nothing to undo")


def redo_next():
    """Re-apply the oldest undone entry (the one right after the newest applied)."""
    for entry in _entries:
        if entry["state"] == UNDONE:
            reapply(entry)
            return entry
    raise HistoryError("Nothing to redo")


def can_undo():
    return any(e["state"] == APPLIED for e in _entries)


def can_redo():
    return any(e["state"] == UNDONE for e in _entries)


def go_to(entry_id):
    """Make `entry_id` the newest change in effect (0 = before all changes)."""
    target = find(entry_id) if entry_id else None
    if entry_id and (target is None or target["state"] == ALT):
        raise HistoryError("That version can't be restored; use Apply Again")
    index = _entries.index(target) if target else -1
    for entry in reversed(_entries[index + 1:]):
        if entry["state"] == APPLIED:
            revert(entry)
    for entry in _entries[:index + 1]:
        if entry["state"] == UNDONE:
            reapply(entry)


def clear():
    """Forget the history. Deletes hidden backups and unlinked created data; the scene stays as it is."""
    for entry in list(_entries):
        _drop_patch(entry)
    _entries.clear()
    sync_ui()


# ---------------------------------------------------------------- native undo sync

def detect_state(entry):
    """Which state the scene is in for this entry: APPLIED, UNDONE, or None if unclear."""
    votes = []
    for c, swaps in entry["swaps"].items():
        for name, info in swaps.items():
            item = getattr(bpy.data, c).get(name)
            if item is None:
                return None
            sig = geometry_signature(item) if c == "meshes" else node_signature(item)
            votes.append(APPLIED if sig == info["after"] else UNDONE if sig == info["before"] else None)
    for name, (before, after) in entry["objects"].items():
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        cur = _object_state(obj)
        cur = {k: cur[k] for k in after}
        votes.append(APPLIED if cur == after else UNDONE if cur == before else None)
    for name, (before, after) in entry["meshes"].items():
        me = bpy.data.meshes.get(name)
        if me is not None:
            cur = _mesh_state(me)
            votes.append(APPLIED if cur == after else UNDONE if cur == before else None)
    for name in entry["created"].get("objects", []):
        obj = bpy.data.objects.get(name)
        votes.append(APPLIED if obj is not None and obj.users_collection else UNDONE)
    votes = [v for v in votes if v is not None] or [None]
    return votes[0] if all(v == votes[0] for v in votes) else None


def sync_after_native_undo():
    global _preview
    if _preview is not None:
        state = detect_state(_preview)
        try:
            check_patchable(_preview)
            ok = state != UNDONE or (_preview["state"] == UNDONE and state == UNDONE)
        except HistoryError:
            ok = False
        if not ok:
            # Ctrl+Z took the preview back: forget it (the scene is already as before).
            _preview["state"] = UNDONE
            try:
                _drop_patch(_preview)
            except Exception:
                pass
            _preview = None
    for entry in _entries:
        if entry["state"] == ALT:
            continue
        state = detect_state(entry)
        if state is not None:
            entry["state"] = state
        try:
            check_patchable(entry)
        except HistoryError:
            # Ctrl+Z went back past this Apply: its saved data is gone. Apply Again still works.
            if entry["state"] != APPLIED:
                entry["state"] = ALT
    sync_ui()


# ---------------------------------------------------------------- UI mirror

def sync_ui():
    try:
        wm = bpy.context.window_manager
        state = wm.claude2blender
    except Exception:
        return
    items = state.history
    items.clear()
    for entry in reversed(_entries):
        item = items.add()
        item.uid = entry["id"]
        item.summary = entry["summary"]
        item.time = entry["time"]
        item.state = entry["state"]
    state.active_history = min(state.active_history, max(0, len(items) - 1))
    try:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


@persistent
def _on_undo_redo(*_args):
    if _entries or _preview is not None:
        sync_after_native_undo()


@persistent
def _on_load_pre(*_args):
    # A different file: the history belongs to the previous one.
    global _preview
    _entries.clear()
    _preview = None


@persistent
def _on_load_post(*_args):
    sync_ui()


_handlers = (
    (bpy.app.handlers.undo_post, _on_undo_redo),
    (bpy.app.handlers.redo_post, _on_undo_redo),
    (bpy.app.handlers.load_pre, _on_load_pre),
    (bpy.app.handlers.load_post, _on_load_post),
    (bpy.app.handlers.save_pre, _on_save_pre),
    (bpy.app.handlers.save_post, _on_save_post),
)


def register():
    for handler_list, fn in _handlers:
        if fn not in handler_list:
            handler_list.append(fn)


def unregister():
    for handler_list, fn in _handlers:
        if fn in handler_list:
            handler_list.remove(fn)
