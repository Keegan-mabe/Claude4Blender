"""v0.5 Environment & Composition: sample scenes, run through the real Apply/Preview/History operators.

1. lighting + background from a reference (staged code like Claude writes)
2. matching a product camera angle
3. exploded composition of several objects (with a parented part)
4. refining lighting and spacing through follow-ups (no duplicates)
5. undo / redo / go to version of whole environment and composition changes
6. unrelated objects and geometry unchanged; Allowed Changes blocks the rest
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import bridge, guard, history, scene_tools  # noqa: E402

ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("  ok:", msg)


C = bpy.context
scene = C.scene
state = C.window_manager.claude2blender
api = bpy.app.driver_namespace["claude2blender"]


# ---------------------------------------------------------------- sample product scene

def mesh_obj(name, op, loc, **kw):
    op(location=loc, **kw)
    obj = C.active_object
    obj.name = obj.data.name = name
    return obj


base = mesh_obj("Base", bpy.ops.mesh.primitive_cube_add, (0, 0, 0.1), size=0.2)
lid = mesh_obj("Lid", bpy.ops.mesh.primitive_cube_add, (0, 0, 0.25), size=0.2)
gear = mesh_obj("Gear", bpy.ops.mesh.primitive_cylinder_add, (0, 0, 0.4), radius=0.08, depth=0.04)
screw = mesh_obj("Screw", bpy.ops.mesh.primitive_cylinder_add, (0.05, 0.05, 0.37), radius=0.01, depth=0.06)
shelf = mesh_obj("Shelf", bpy.ops.mesh.primitive_plane_add, (3, 0, 0), size=1)
base.scale = (1.0, 1.5, 0.5)   # a CAD part with non-unit scale must keep it exactly
lid.rotation_euler = (0, 0, 0.3)
screw.parent = lid
screw.matrix_parent_inverse = lid.matrix_world.inverted()
bpy.ops.object.light_add(type="POINT", location=(2, -2, 3))
user_light = C.active_object
user_light.name = user_light.data.name = "Light"
user_light.data.energy = 500
bpy.ops.object.camera_add(location=(0.8, -1.2, 0.6))
cam = C.active_object
cam.name = cam.data.name = "Camera"
cam.data.lens = 50
scene.camera = cam
world = bpy.data.worlds.new("World")
world.use_nodes = True
scene.world = world
scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 1920, 1080, 100
C.view_layer.update()

PRODUCTS = (base, lid, gear, screw)


def geo():
    return {o.name: (history.geometry_signature(o.data), tuple(o.scale), tuple(o.dimensions))
            for o in bpy.data.objects if o.type == "MESH" and not scene_tools.is_generated(o)}


def xf(obj):
    return (tuple(obj.location), tuple(obj.rotation_euler), tuple(obj.scale), obj.parent,
            tuple(map(tuple, obj.matrix_parent_inverse)))


def world_pos(obj):
    return obj.matrix_world.translation.copy()


GEO0 = geo()
XF0 = {o.name: xf(o) for o in bpy.data.objects}
SHELF0 = xf(shelf)
RES0 = (1920, 1080, 100)


def res():
    r = scene.render
    return (r.resolution_x, r.resolution_y, r.resolution_percentage)


def select(*objs):
    for o in bpy.data.objects:
        o.select_set(o in objs)
    if objs:
        C.view_layer.objects.active = objs[0]


def stage(mode, code, summary, *sel):
    state.mode = mode
    select(*sel)
    bridge.send_request(C, copy_to_clipboard=False)
    api.stage_proposal(code, summary)


def apply(mode, code, summary, *sel):
    stage(mode, code, summary, *sel)
    last = history.entries()[-1]["id"] if history.entries() else 0
    bpy.ops.claude2blender.apply_proposal()
    return bool(history.entries()) and history.entries()[-1]["id"] != last  # (History keeps 10)


def studio_objects():
    coll = bpy.data.collections.get(scene_tools.STUDIO_COLLECTION)
    return sorted(o.name for o in coll.objects) if coll else []


def in_scene(name):
    obj = bpy.data.objects.get(name)
    return obj is not None and obj.name in scene.objects


# ---------------------------------------------------------------- the request Claude gets

state.mode = "ENVIRONMENT"
select(base, lid, gear)
req, text = bridge.send_request(C, copy_to_clipboard=False)
perms = req["permissions"]
check(perms["lighting"] and perms["world"] and perms["backdrop"], "Environment allows lighting, world, backdrop by default")
check(not (perms["camera"] or perms["positions"] or perms["rotations"] or perms["spacing"] or perms["output"]),
      "Environment: camera, object transforms and output off by default")
check(perms["movable"] == [], "Environment: nothing may move by default")
lay = req["scene"]["layout"]
check([l["name"] for l in lay["lights"]] == ["Light"] and lay["cameras"][0]["active"], "layout lists lights and the active camera")
check({o["name"] for o in lay["objects"]} >= {"Base", "Lid", "Gear", "Screw", "Shelf"}, "layout lists the scene objects")
scr = next(o for o in lay["objects"] if o["name"] == "Screw")
check(scr["parent"] == "Lid", "layout reports parenting")
check(lay["render"]["resolution"] == [1920, 1080, 100], "layout reports resolution")
check("Allowed changes: Lighting, World, Backdrop" in bridge.clipboard_message(req), "Desktop message names the allowed changes")
state.mode = "COMPOSITION"
req = bridge.build_request(C)
check(req["permissions"]["camera"] and req["permissions"]["spacing"] and not req["permissions"]["lighting"],
      "Composition allows camera + transforms, not lighting, by default")
check(set(req["permissions"]["movable"]) == {"Base", "Lid", "Gear"}, "Composition: only the selection may move")
check("Lighting" in req["permissions"]["not_allowed"], "permissions list what's not allowed")


# ---------------------------------------------------------------- 1. lighting + background

ENV_V1 = '''
from mathutils import Vector
b = c2b.bounds(c2b.targets())
c, s = b["center"], max(b["size"])
c2b.studio_world(top="#2E2E30", bottom="#6B6B6E", strength=0.35, camera_color="#3A3A3D")
c2b.light("C2B Key", "AREA", location=c + Vector((-3, -2.5, 3)) * s, target=c, energy=180, size=1.2 * s,
          color="#FFF1E0", shape="RECTANGLE", size_y=0.8 * s)
c2b.light("C2B Fill", "AREA", location=c + Vector((3, -2, 1)) * s, target=c, energy=45, size=2.0 * s)
c2b.light("C2B Rim", "AREA", location=c + Vector((0.5, 3, 2.5)) * s, target=c, energy=120, size=0.3 * s,
          size_y=2 * s, shape="RECTANGLE", color="#E0ECFF")
c2b.backdrop("C2B Backdrop", "sweep", color="#8A8A8D", roughness=0.7)
c2b.exposure(value=-0.3)
'''

state.mode = "ENVIRONMENT"
check(apply("ENVIRONMENT", ENV_V1, "Soft studio lighting", base, lid, gear), "1. environment change applied")
e1 = history.entries()[-1]
check(studio_objects() == ["C2B Backdrop", "C2B Fill", "C2B Key", "C2B Rim"], "three lights + backdrop in the Studio collection")
check(scene.world.name == "C2B Studio World" and "World" in bpy.data.worlds, "studio world assigned; the original kept")
key = bpy.data.objects["C2B Key"]
aim = key.matrix_world.to_3x3() @ Vector((0, 0, -1))
to_target = (scene_tools.bounds([base, lid, gear])["center"] - key.matrix_world.translation).normalized()
check(aim.dot(to_target) > 0.999, "key light aims at the product")
check(abs(key.data.size - 1.2 * max(scene_tools.bounds([base, lid, gear])["size"])) < 1e-5, "key size scales with the product")
check(scene.view_settings.exposure == -0.3 or abs(scene.view_settings.exposure + 0.3) < 1e-6, "exposure set")
check(geo() == GEO0, "product geometry, scale and dimensions unchanged")
check(all(xf(o) == XF0[o.name] for o in (*PRODUCTS, shelf, user_light, cam)), "products, shelf, existing light and camera untouched")
check(user_light.data.energy == 500 and cam.data.lens == 50, "existing light/camera settings untouched")
check(res() == RES0, "output resolution preserved")
texts = " | ".join(t for _i, t in e1["changes"])
check("Lights created: C2B Fill, C2B Key, C2B Rim" in texts, "summary lists the lights created")
check("World: World → C2B Studio World" in texts and "Backdrop/helpers created: C2B Backdrop" in texts, "summary lists world and backdrop")
check("Render: exposure" in texts and "Geometry, UVs, scale and modifiers unchanged" in texts, "summary lists exposure and geometry protection")


# ---------------------------------------------------------------- 4. refine lighting (follow-ups)

ENV_V2 = ENV_V1.replace("energy=180, size=1.2 * s", "energy=150, size=2.4 * s").replace('color="#8A8A8D"', 'color="#B5B5B8"')
check(apply("ENVIRONMENT", ENV_V2, "Softer shadows, lighter backdrop", base, lid, gear), "4. refinement applied")
check(studio_objects() == ["C2B Backdrop", "C2B Fill", "C2B Key", "C2B Rim"], "refinement made no duplicate lights or backdrops")
check(len([w for w in bpy.data.worlds if not w.name.startswith(".c2b")]) == 2, "no duplicate worlds")
size_v2 = key.data.size
check(abs(size_v2 - 2 * 1.2 * max(scene_tools.bounds([base, lid, gear])["size"])) < 1e-5, "key light is larger (softer shadows)")
bd_mat = bpy.data.objects["C2B Backdrop"].material_slots[0].material
check(abs(bd_mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value[0] - scene_tools.color("#B5B5B8")[0]) < 1e-5,
      "backdrop is a lighter grey")
check(bpy.data.objects["C2B Fill"].data.energy == 45, "unrequested lights unchanged")
e2 = history.entries()[-1]
check(any("Lights modified: C2B Key" in t for _i, t in e2["changes"]), "refinement summary: key modified")

# Backdrop resize through its own scale is allowed (it's generated, not your geometry)
ENV_BD = "c2b.backdrop('C2B Backdrop', 'sweep', width=4, depth=3, height=1.5)"
check(apply("ENVIRONMENT", ENV_BD, "Bigger backdrop", base), "generated backdrop can be resized")
bd = bpy.data.objects["C2B Backdrop"]
check(abs(bd.dimensions.x - 4) < 1e-4 and abs(bd.dimensions.y - 3) < 1e-4, "backdrop is 4 × 3 m")
check(geo() == GEO0, "product geometry still unchanged")
bpy.ops.claude2blender.history_undo()
check(abs(bd.scale.x - 1) < 1e-9, "undo restores the backdrop size")


# ---------------------------------------------------------------- 5a. undo / redo the environment

bpy.ops.claude2blender.history_undo()
check(abs(key.data.size - size_v2 / 2) < 1e-6 and bpy.data.objects["C2B Key"].data.energy == 180, "undo: back to v1 lighting")
bpy.ops.claude2blender.history_undo()
check(scene.world.name == "World", "undo all: original world back")
check(not any(in_scene(n) for n in ("C2B Key", "C2B Fill", "C2B Rim", "C2B Backdrop")), "undo all: studio objects gone from the scene")
check(abs(scene.view_settings.exposure) < 1e-6, "undo all: exposure back")
check(all(xf(o) == XF0[o.name] for o in (*PRODUCTS, shelf, user_light, cam)), "undo all: everything else as before")
bpy.ops.claude2blender.history_redo()
bpy.ops.claude2blender.history_redo()
check(abs(key.data.size - size_v2) < 1e-6 and in_scene("C2B Rim") and scene.world.name == "C2B Studio World",
      "redo: the refined environment is back exactly")
bpy.ops.claude2blender.history_goto(uid=e1["id"])
check(abs(key.data.size - size_v2 / 2) < 1e-6, "go to version 1 works")
bpy.ops.claude2blender.history_goto(uid=e2["id"])
check(abs(key.data.size - size_v2) < 1e-6, "go to version 2 works")


# ---------------------------------------------------------------- 6. Allowed Changes blocks the rest

def blocked(mode, code, *sel):
    before = {o.name: xf(o) for o in bpy.data.objects}
    names = {o.name for o in bpy.data.objects}
    n = history.entries()[-1]["id"] if history.entries() else 0
    stage(mode, code, "should be blocked", *sel)
    bpy.ops.claude2blender.apply_proposal()
    n_after = history.entries()[-1]["id"] if history.entries() else 0
    same = ({o.name for o in bpy.data.objects} == names
            and all(xf(o) == before[o.name] for o in bpy.data.objects))
    return state.status.startswith("Blocked") and n_after == n and same and state.proposal_pending


check(blocked("ENVIRONMENT", "c2b.light('C2B Extra', location=(1,1,1))\nbpy.data.objects['Base'].location.x += 0.5", base),
      "Environment: moving the product is blocked, and the new light is taken back too")
check("Object positions is off" in state.status or "moved Base" in state.status, "status says why")
check(not in_scene("C2B Extra") and "C2B Extra" not in bpy.data.objects, "blocked change leaves nothing behind")
check(blocked("ENVIRONMENT", "bpy.data.objects['Camera'].data.lens = 85", base), "Environment: camera off blocks lens change")
check(blocked("ENVIRONMENT", "bpy.context.scene.render.resolution_x = 1080", base), "output resolution change is blocked")
check(res() == RES0, "resolution still 1920 × 1080")
check(blocked("ENVIRONMENT", "bpy.data.objects['Base'].hide_render = True", base), "hiding a product is blocked")
check(blocked("ENVIRONMENT", "m = bpy.data.materials.new('X')\nbpy.data.objects['Base'].data.materials.append(m)", base),
      "changing product materials is not an environment change")
check(len(base.data.materials) == 0 and "X" not in bpy.data.materials, "the material change was taken back")
state.perm_env.lighting = False
check(blocked("ENVIRONMENT", "c2b.light('C2B Key', energy=999)", base), "Lighting off: light changes blocked")
state.perm_env.lighting = True

# Deletion is always blocked (checked directly: headless runs can't use Blender's own undo)
tmp = bpy.data.objects.new("Temp Light", bpy.data.lights.new("Temp Light", "POINT"))
scene.collection.objects.link(tmp)
snap = guard.Snapshot({"mode": "environment", "selection": {"objects": [], "faces": []}},
                      {k: True for k in guard.CATEGORIES})
bpy.data.objects.remove(tmp)
problems, untracked = snap.check()
check(problems and "deleted Temp Light" in problems[0] and untracked, "deleting a scene element is always blocked")


# ---------------------------------------------------------------- 2. product camera angle

state.perm_comp.camera = True
CAM = '''
cam = c2b.camera(use_existing=bpy.context.scene.camera, lens=85)
c2b.frame(cam, c2b.targets(), direction=(1.0, -1.2, 0.7), margin=1.2)
'''
check(apply("COMPOSITION", CAM, "Three-quarter hero angle, 85 mm", base, lid, gear), "2. camera angle applied")
e_cam = history.entries()[-1]
check(cam.data.lens == 85, "lens 85 mm")
center = scene_tools.bounds([base, lid, gear])["center"]
d = (cam.matrix_world.translation - center).normalized()
check(d.dot(Vector((1.0, -1.2, 0.7)).normalized()) > 0.9999, "camera looks from the requested direction")
aim = cam.matrix_world.to_3x3() @ Vector((0, 0, -1))
check(aim.dot(-d) > 0.9999, "camera aims at the product")
from bpy_extras.object_utils import world_to_camera_view  # noqa: E402
C.view_layer.update()
pts = [world_to_camera_view(scene, cam, o.matrix_world @ Vector(c)) for o in (base, lid, gear) for c in o.bound_box]
inside = all(0 <= p.x <= 1 and 0 <= p.y <= 1 for p in pts)
tight = max(max(abs(p.x - 0.5), abs(p.y - 0.5)) for p in pts) > 0.4
check(inside and tight, "frame() fits every corner inside the 16:9 frame, filling it (10% margin)")
check(studio_objects() == ["C2B Backdrop", "C2B Fill", "C2B Key", "C2B Rim"], "no new camera created")
check(res() == RES0 and geo() == GEO0, "resolution and geometry unchanged")
check(all(xf(o) == XF0[o.name] for o in (*PRODUCTS, shelf)), "objects didn't move")
check(any(t.startswith("Camera:") and "50 → 85 mm" in t for _i, t in e_cam["changes"]), "summary shows the lens change")


# ---------------------------------------------------------------- 3. exploded view

lid_screw_local = xf(screw)
EXPLODE = "c2b.explode([bpy.data.objects[n] for n in ('Base', 'Lid', 'Gear')], axis=(0, 0, 1), spacing={sp})"
check(apply("COMPOSITION", EXPLODE.format(sp=0.4), "Exploded along Z", base, lid, gear), "3. exploded view applied")
e_ex = history.entries()[-1]
zs = [world_pos(o).z for o in (base, lid, gear)]
check(abs((zs[1] - zs[0]) - 0.4) < 1e-5 and abs((zs[2] - zs[1]) - 0.4) < 1e-5, "parts evenly spaced 0.4 m along Z")
check(abs(sum(zs) / 3 - 0.25) < 1e-5, "group stays centred")
check(xf(screw) == lid_screw_local, "child keeps its local transform and parent")
check(abs((world_pos(screw) - world_pos(lid)).length - (Vector((0.05, 0.05, 0.37)) - Vector((0, 0, 0.25))).length) < 1e-5,
      "child follows its parent rigidly")
check(geo() == GEO0 and tuple(base.scale) == XF0["Base"][2], "no scale or geometry change on any part")
check(xf(shelf) == SHELF0, "unselected Shelf unchanged")
texts = " | ".join(t for _i, t in e_ex["changes"])
check("Moved: Base, Gear" in texts and "Lid" not in texts, "summary: moved parts (the middle part, Lid, stays put)")

# 4b. refine spacing
check(apply("COMPOSITION", EXPLODE.format(sp=0.6), "More spacing", base, lid, gear), "4b. spacing refinement applied")
zs = [world_pos(o).z for o in (base, lid, gear)]
check(abs((zs[1] - zs[0]) - 0.6) < 1e-5, "spacing increased to 0.6 m")

# 5b. undo / redo composition
bpy.ops.claude2blender.history_undo()
zs = [world_pos(o).z for o in (base, lid, gear)]
check(abs((zs[1] - zs[0]) - 0.4) < 1e-5, "undo: back to 0.4 m spacing")
bpy.ops.claude2blender.history_undo()
check(all(xf(o) == XF0[o.name] for o in PRODUCTS), "undo: parts back exactly (same floats)")
bpy.ops.claude2blender.history_undo()
check(cam.data.lens == 50 and xf(cam) == XF0["Camera"], "undo: camera back exactly")
bpy.ops.claude2blender.history_redo()
bpy.ops.claude2blender.history_redo()
bpy.ops.claude2blender.history_redo()
zs = [world_pos(o).z for o in (base, lid, gear)]
check(cam.data.lens == 85 and abs((zs[1] - zs[0]) - 0.6) < 1e-5, "redo: camera + exploded view back")


# ---------------------------------------------------------------- 6b. composition guard

check(blocked("COMPOSITION", "bpy.data.objects['Shelf'].location.x += 1", base, lid, gear),
      "Composition: moving an unselected object is blocked")
check("isn't selected" in state.status, "status names the unselected object")
state.perm_comp.full_assembly = True
check(apply("COMPOSITION", "bpy.data.objects['Shelf'].location.x += 1", "Shelf over", base), "Full assembly allows it")
bpy.ops.claude2blender.history_undo()
check(xf(shelf) == SHELF0, "undo puts it back")
state.perm_comp.full_assembly = False

state.perm_comp.spacing = False
check(blocked("COMPOSITION", EXPLODE.format(sp=0.9), base, lid, gear), "Spacing off: exploding blocked")
check(apply("COMPOSITION", "for n in ('Base','Lid','Gear'):\n    o = bpy.data.objects[n]\n    c2b.place(o, location=o.matrix_world.translation + __import__('mathutils').Vector((0.2, 0, 0)))",
            "Shift group", base, lid, gear), "Spacing off: moving the group rigidly is fine")
check(any(t == "1 child object follows its parent" for _i, t in history.entries()[-1]["changes"]),
      "summary notes the child that follows Lid")
bpy.ops.claude2blender.history_undo()
state.perm_comp.spacing = True
state.perm_comp.positions = False
check(apply("COMPOSITION", EXPLODE.format(sp=0.9), "Explode in place", base, lid, gear), "Positions off, spacing on: explode around the centre is fine")
check(blocked("COMPOSITION", "bpy.data.objects['Base'].location.x += 0.3", base, lid, gear),
      "Positions off: moving the group off-centre blocked")
bpy.ops.claude2blender.history_undo()
state.perm_comp.positions = True
state.perm_comp.rotations = False
check(blocked("COMPOSITION", "bpy.data.objects['Gear'].rotation_euler.z += 0.5", base, lid, gear), "Rotations off: rotating blocked")
state.perm_comp.rotations = True
check(blocked("COMPOSITION", "bpy.data.objects['Screw'].parent = None", base, lid, gear, screw), "re-parenting is always blocked")
check(blocked("COMPOSITION", "c2b.light('C2B Kicker', location=(1, 1, 1))", base), "Composition: lighting off by default")


# ---------------------------------------------------------------- preview: Original vs Proposed

stage("ENVIRONMENT", ENV_V2.replace("C2B Key", "C2B Key 2"), "Preview test", base, lid, gear)
bpy.ops.claude2blender.preview_proposal()
pv = history.preview_entry()
check(pv is not None and in_scene("C2B Key 2"), "preview shows the proposed lights")
check(any("Lights created: C2B Key 2" in t for _i, t in pv["changes"]), "preview lists its changes")
bpy.ops.claude2blender.compare_preview(show_original=True)
check(history.showing_original() and not in_scene("C2B Key 2"), "Original: proposed light hidden, scene as before")
bpy.ops.claude2blender.compare_preview(show_original=False)
check(in_scene("C2B Key 2"), "Proposed: back on")
bpy.ops.claude2blender.compare_preview(show_original=True)
n = history.entries()[-1]["id"]
bpy.ops.claude2blender.apply_proposal()
check(history.entries()[-1]["id"] != n and in_scene("C2B Key 2") and history.preview_entry() is None,
      "Apply while showing Original applies the proposed change")
bpy.ops.claude2blender.history_undo()
check(not in_scene("C2B Key 2"), "and it undoes cleanly")

stage("ENVIRONMENT", "c2b.light('C2B Temp', location=(0, 0, 2))\nc2b.studio_world(top='#FFFFFF')", "Discard test", base)
world_before = scene.world.name
bpy.ops.claude2blender.preview_proposal()
check(in_scene("C2B Temp"), "preview 2 on")
bpy.ops.claude2blender.discard_proposal()
check(not in_scene("C2B Temp") and scene.world.name == world_before, "Discard ends the preview exactly")
check("C2B Temp" not in bpy.data.objects, "discarded preview leaves no objects behind")


# ---------------------------------------------------------------- EEVEE and Cycles

for engine in ("CYCLES", "BLENDER_EEVEE_NEXT" if bpy.app.version < (5, 0, 0) else "BLENDER_EEVEE"):
    scene.render.engine = engine
    check(apply("ENVIRONMENT", ENV_V1.replace("energy=180", "energy=210"), f"Lighting on {engine}", base),
          f"environment change applies with {engine}")
    check(scene.render.engine == engine, f"{engine} kept as the render engine")
    bpy.ops.claude2blender.history_undo()


# ---------------------------------------------------------------- end state

while history.can_undo():
    bpy.ops.claude2blender.history_undo()
check(all(xf(o) == XF0[o.name] for o in (*PRODUCTS, shelf, user_light, cam)), "after undoing everything: every transform identical")
check(geo() == GEO0 and res() == RES0, "geometry and resolution as at the start")
# (History keeps the last 10 changes, so undoing all of them stops at the oldest one kept.)
check(len(history.entries()) == history.MAX_ENTRIES, "history capped at 10 entries")

print(f"\nAll {ok} scene checks passed on Blender {bpy.app.version_string}")
addon.unregister()
