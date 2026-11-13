"""What an Environment or Composition change may touch, checked after it runs.

The toggles under "Allowed changes" decide which categories Claude's code
may change. The guard compares the scene before and after the code and lists
every change outside them; the caller then takes the whole change back.

Categories:
  lighting   lights (new or existing), exposure / contrast look, engine light settings
  world      the scene's world (assign or edit) and a transparent film
  backdrop   backdrop / helper objects and their materials
  camera     cameras, the active camera, lens and depth of field
  positions  moving the movable objects as a group
  rotations  rotating the movable objects
  spacing    changing the distances between the movable objects (exploded views)
  output     render engine and output resolution

Always blocked in these modes: deleting anything, re-parenting, constraints,
hiding or swapping the data of existing objects, editing their materials,
and moving anything that isn't movable (the selection, or the full assembly
when that is allowed). Geometry, scale and modifiers are Preserve
Geometry's job.
"""

import itertools

import bpy

from . import history
from .scene_tools import is_generated, role, selected_names

CATEGORIES = ("lighting", "world", "backdrop", "camera", "positions", "rotations", "spacing", "output")
LABELS = {
    "lighting": "Lighting", "world": "World environment", "backdrop": "Background/backdrop",
    "camera": "Camera", "positions": "Object positions", "rotations": "Object rotations",
    "spacing": "Object spacing", "output": "Render output",
}
GUARDED_MODES = ("environment", "composition")
ROT_KEYS = ("rotation_mode", "rotation_euler", "rotation_quaternion", "rotation_axis_angle")
IDS = ("objects", "lights", "cameras", "worlds", "materials", "collections", "meshes")
OUTPUT_KEYS = ("render.engine", "render.resolution_x", "render.resolution_y", "render.resolution_percentage",
               "render.pixel_aspect_x", "render.pixel_aspect_y")


def applies(request):
    return bool(request) and request.get("mode") in GUARDED_MODES


def movable_names(request, permissions):
    if permissions.get("full_assembly"):
        return {o.name for o in bpy.context.scene.objects
                if o.type not in {"LIGHT", "CAMERA"} and not is_generated(o)}
    names = set()
    for name in selected_names(request):
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type not in {"LIGHT", "CAMERA"}:
            names.add(name)
    return names


class Snapshot:
    def __init__(self, request, permissions):
        self.request = request
        self.perms = dict(permissions)
        self.movable = movable_names(request, permissions)
        self.ids = {c: {i.name for i in getattr(bpy.data, c)} for c in IDS}
        self.objects = {}
        for obj in bpy.data.objects:
            st = history._object_state(obj)
            st["type"] = obj.type
            st["role"] = role(obj)
            st["world"] = tuple(obj.matrix_world.translation)
            self.objects[obj.name] = st
        self.datas = {c: {i.name: history.data_state(i) for i in getattr(bpy.data, c)} for c in ("lights", "cameras")}
        self.scene = history._scene_state(bpy.context.scene)
        self.worlds = {w.name: history.node_signature(w) for w in bpy.data.worlds}
        self.materials = {m.name: history.node_signature(m) for m in bpy.data.materials}

    # ------------------------------------------------------------ check

    def check(self):
        """Returns (problems, untracked): problems as short sentences; untracked=True when
        some change can't be taken back by the history patch (then use Blender's undo)."""
        perms, problems, untracked = self.perms, {}, False
        bpy.context.view_layer.update()  # code may have set location without a depsgraph update

        def need(cat, what):
            if not perms.get(cat):
                problems.setdefault(cat, []).append(what)

        def forbid(what):
            problems.setdefault("_", []).append(what)

        # Deletions and new datablocks.
        for c in IDS:
            now = {i.name for i in getattr(bpy.data, c)}
            gone = sorted(self.ids[c] - now)
            if gone:
                forbid(f"deleted {', '.join(gone[:3])}")
                untracked = True
        for name in sorted({o.name for o in bpy.data.objects} - self.ids["objects"]):
            obj = bpy.data.objects[name]
            if obj.name.startswith(history.BACKUP_PREFIX):
                continue
            if obj.type == "LIGHT":
                need("lighting", f"created {name}")
            elif obj.type == "CAMERA":
                need("camera", f"created {name}")
            elif obj.type == "EMPTY":
                if not (perms.get("lighting") or perms.get("camera") or perms.get("backdrop")):
                    need("backdrop", f"created {name}")
            else:
                need("backdrop", f"created {name}")
        for name in sorted({w.name for w in bpy.data.worlds} - self.ids["worlds"]):
            need("world", f"created world {name}")

        # Existing objects.
        moved = []
        for name, b in self.objects.items():
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            a = history._object_state(obj)
            changed = {k for k in a if a[k] != b.get(k)}
            if not changed:
                continue
            kind, generated = b["type"], bool(b["role"])
            if kind == "LIGHT":
                need("lighting", f"changed {name}")
            elif kind == "CAMERA":
                need("camera", f"changed {name}")
            elif generated:
                need("backdrop", f"changed {name}")
            else:
                if changed & {"parent", "matrix_parent_inverse"}:
                    forbid(f"re-parented {name}")
                if "constraints" in changed:
                    forbid(f"constraints changed on {name}")
                    untracked = True
                if changed & {"hide_viewport", "hide_render"}:
                    forbid(f"hid or showed {name}")
                if "slots" in changed:
                    forbid(f"changed the materials of {name} (use Material mode)")
                if "data" in changed:
                    forbid(f"replaced the data of {name}")
                    untracked = True
                if "location" in changed or changed & set(ROT_KEYS):
                    if name not in self.movable:
                        forbid(f"moved {name}, which isn't selected"
                               + ("" if self.request.get("mode") != "composition" else
                                  " (select it, or allow the full assembly)"))
                        continue
                if "location" in changed:
                    if not (perms.get("positions") or perms.get("spacing")):
                        need("positions", f"moved {name}")
                    else:
                        moved.append(name)
                if changed & set(ROT_KEYS):
                    need("rotations", f"rotated {name}")

        # Group rules for the movable set: positions = move as a group, spacing = distances between parts.
        if moved and not (perms.get("positions") and perms.get("spacing")):
            group = [n for n in self.movable if n in self.objects and bpy.data.objects.get(n) is not None]
            before = {n: self.objects[n]["world"] for n in group}
            after = {n: tuple(bpy.data.objects[n].matrix_world.translation) for n in group}
            ext = max([abs(c) for p in before.values() for c in p] + [1.0])
            tol = 1e-4 + 1e-4 * ext
            if not perms.get("positions") and len(group):
                cb = [sum(p[i] for p in before.values()) / len(group) for i in range(3)]
                ca = [sum(p[i] for p in after.values()) / len(group) for i in range(3)]
                if max(abs(x - y) for x, y in zip(cb, ca)) > tol:
                    need("positions", "moved the group as a whole (only spacing is allowed: keep its centre)")
            if not perms.get("spacing") and len(group) > 1:
                for m, n in itertools.combinations(group, 2):
                    db = _dist(before[m], before[n])
                    da = _dist(after[m], after[n])
                    if abs(db - da) > tol:
                        need("spacing", f"changed the spacing between {m} and {n}")
                        break

        # Light and camera settings.
        for c, cat in (("lights", "lighting"), ("cameras", "camera")):
            for name, b in self.datas[c].items():
                item = getattr(bpy.data, c).get(name)
                if item is not None and history.data_state(item) != b:
                    need(cat, f"changed {c[:-1]} settings of {name}")

        # Scene settings.
        now = history._scene_state(bpy.context.scene)
        diff = {k for k in now if now[k] != self.scene.get(k)}
        if "world" in diff or "film_transparent" in diff:
            need("world", "changed the scene world" if "world" in diff else "changed the transparent background")
        if "camera" in diff:
            need("camera", "changed the active camera")
        if diff & set(OUTPUT_KEYS):
            need("output", "changed the render engine or output resolution")
        light_keys = {k for k in diff if k in ("view_transform", "look", "exposure", "gamma")
                      or k.startswith(("eevee.", "cycles."))}
        if light_keys:
            need("lighting", "changed exposure or render light settings")

        # Worlds and materials edited in place.
        for name, sig in self.worlds.items():
            w = bpy.data.worlds.get(name)
            if w is not None and history.node_signature(w) != sig:
                need("world", f"edited world {name}")
        for name, sig in self.materials.items():
            m = bpy.data.materials.get(name)
            if m is None or name.startswith(history.BACKUP_PREFIX) or history.node_signature(m) == sig:
                continue
            if role(m) == "backdrop" or _only_generated_users(m):
                need("backdrop", f"edited material {name}")
            else:
                forbid(f"edited material {name} (use Material mode)")

        return _sentences(problems), untracked


def _dist(p, q):
    return sum((x - y) ** 2 for x, y in zip(p, q)) ** 0.5


def _only_generated_users(mat):
    users = [o for o in bpy.data.objects if any(s.material == mat for s in o.material_slots)]
    return bool(users) and all(is_generated(o) for o in users)


def _sentences(problems):
    out = []
    for cat, items in problems.items():
        items = list(dict.fromkeys(items))
        listed = ", ".join(items[:2]) + (f" (+{len(items) - 2})" if len(items) > 2 else "")
        if cat == "_":
            out.append(f"Not allowed: {listed}")
        else:
            out.append(f"{LABELS[cat]} is off: {listed}")
    return out
