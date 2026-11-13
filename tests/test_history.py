"""Change history: Undo / Redo / Go To / Apply Again, headless, on a fresh scene."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import history  # noqa: E402

api = bpy.app.driver_namespace["claude2blender"]
state = bpy.context.window_manager.claude2blender
scene = bpy.context.scene
ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("  ok:", msg)


def apply(code, summary):
    api.stage_proposal(code, summary)
    assert bpy.ops.claude2blender.apply_proposal() == {"FINISHED"}, summary
    assert state.last_apply_ok, state.status


def color(mat_name):
    m = bpy.data.materials[mat_name]
    return tuple(round(c, 3) for c in m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value)


def mat_of(obj_name):
    s = bpy.data.objects[obj_name].material_slots
    return s[0].material.name if s and s[0].material else None


def backups():
    return [m.name for coll in (bpy.data.materials, bpy.data.worlds, bpy.data.meshes)
            for m in coll if m.name.startswith(".c2b")]


# --- scene: Body, Lid, an untouched Bystander, a light, a world
for name, x in (("Body", 0), ("Lid", 3), ("Bystander", 6)):
    bpy.ops.mesh.primitive_cube_add(location=(x, 0, 0))
    bpy.context.active_object.name = name
lid_mat = bpy.data.materials.new("LidPaint")
lid_mat.use_nodes = True
bpy.data.objects["Lid"].data.materials.append(lid_mat)
lid_mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.5, 0.5, 0.5, 1)
by_mat = bpy.data.materials.new("BystanderPaint")
by_mat.use_nodes = True
bpy.data.objects["Bystander"].data.materials.append(by_mat)
bpy.ops.object.light_add(type="AREA", location=(0, 0, 4))
bpy.context.active_object.name = "Key"
bpy.context.active_object.data.name = "Key"; bpy.data.lights["Key"].energy = 100
scene.world = bpy.data.worlds.new("Studio")
for o in bpy.data.objects:
    o.select_set(o.name == "Body")
bpy.context.view_layer.objects.active = bpy.data.objects["Body"]

bystander_before = (history._object_state(bpy.data.objects["Bystander"]), history.node_signature(by_mat))

MAKE_MAT = '''
m = bpy.data.materials.new("{name}")
m.use_nodes = True
m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = {rgba}
o = bpy.data.objects["Body"]
if o.material_slots: o.material_slots[0].material = m
else: o.data.materials.append(m)
'''

apply(MAKE_MAT.format(name="Orange", rgba=(0.9, 0.3, 0.0, 1)), "Orange body")
e1 = history.entries()[-1]["id"]
apply(MAKE_MAT.format(name="Blue", rgba=(0.0, 0.2, 0.9, 1)), "Blue body")
e2 = history.entries()[-1]["id"]
check(mat_of("Body") == "Blue", "two material changes applied")

# In-place edit of an existing material.
apply('bpy.data.materials["LidPaint"].node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1, 0, 0, 1)',
      "Red lid paint (edit in place)")
check(color("LidPaint") == (1, 0, 0, 1), "in-place material edit applied")

# Environment-style change: new object, moved part, light, world.
apply('''
bpy.ops.mesh.primitive_uv_sphere_add(location=(0, 5, 0)); bpy.context.active_object.name = "Backdrop"
bpy.data.objects["Lid"].location.z = 2
bpy.data.lights["Key"].energy = 750
w = bpy.data.worlds.new("Sunset"); bpy.context.scene.world = w
''', "Sunset environment")
check(bpy.data.objects["Lid"].location.z == 2 and scene.world.name == "Sunset", "environment change applied")
check(len(state.history) == 4 and state.history[0].summary == "Sunset environment", "history lists 4 changes, newest first")

# The user changes something by hand after the Apply.
bpy.data.objects["Lid"].rotation_euler.z = 0.5

# --- Undo: only what the change touched is reverted
bpy.ops.claude2blender.history_undo()
lid = bpy.data.objects["Lid"]
check(lid.location.z == 0, "undo restores the part's position")
check(round(lid.rotation_euler.z, 3) == 0.5, "undo keeps the user's own later edit")
check(bpy.data.lights["Key"].energy == 100 and scene.world.name == "Studio", "undo restores light and world")
backdrop = bpy.data.objects["Backdrop"]
check(not backdrop.users_collection, "undo removes the created object from the scene (kept for redo)")

bpy.ops.claude2blender.history_undo()
check(color("LidPaint") == (0.5, 0.5, 0.5, 1) and mat_of("Lid") == "LidPaint",
      "undo restores an in-place material edit exactly, same material name")
check(state.history[0].state == "UNDONE" and state.history[1].state == "UNDONE", "undone changes stay in history")

# --- Redo: same change, no code re-run
runs_before = len(bpy.data.materials)
bpy.ops.claude2blender.history_redo()
check(color("LidPaint") == (1, 0, 0, 1), "redo re-applies the in-place edit")
bpy.ops.claude2blender.history_redo()
check(bpy.data.objects["Backdrop"].users_collection and scene.world.name == "Sunset"
      and bpy.data.lights["Key"].energy == 750, "redo relinks created object, world and light")
check(len(bpy.data.materials) == runs_before and "Backdrop.001" not in bpy.data.objects,
      "redo did not run the code again (no duplicates)")
check(round(lid.rotation_euler.z, 3) == 0.5, "redo still keeps the user's own edit")

# --- Go to a version
bpy.ops.claude2blender.history_goto(uid=e1)
check(mat_of("Body") == "Orange" and color("LidPaint") == (0.5, 0.5, 0.5, 1) and scene.world.name == "Studio",
      "go to version 1 steps back three changes")
bpy.ops.claude2blender.history_goto(uid=0)
check(mat_of("Body") is None, "go to original removes all changes")
bpy.ops.claude2blender.history_goto(uid=history.entries()[-1]["id"])
check(mat_of("Body") == "Blue" and scene.world.name == "Sunset", "go to newest brings everything back")

# --- Apply Again after undo branches the timeline, but keeps the code
bpy.ops.claude2blender.history_goto(uid=e1)
bpy.ops.claude2blender.history_apply_again(uid=e2)
check(mat_of("Body").startswith("Blue"), "Apply Again re-runs a stored change without Claude")
states = [e["state"] for e in history.entries()]
check(states.count("ALT") == 3 and states[-1] == "APPLIED", "undone branch kept as re-runnable entries")
check("Backdrop" not in bpy.data.objects, "abandoned branch's hidden objects are cleaned up")
alt = next(e for e in history.entries() if e["summary"] == "Sunset environment")
bpy.ops.claude2blender.history_apply_again(uid=alt["id"])
check(scene.world.name.startswith("Sunset") and "Backdrop" in bpy.data.objects, "Apply Again works on an old entry")

# --- Bystander never touched
check((history._object_state(bpy.data.objects["Bystander"]), history.node_signature(by_mat)) == bystander_before,
      "unrelated object and its material untouched throughout")

# --- Native Ctrl+Z sync (simulated: revert behind the history's back, then run the handler)
entry = history.entries()[-1]
history.revert(entry)
entry["state"] = "APPLIED"
history.sync_after_native_undo()
check(entry["state"] == "UNDONE", "history re-syncs after Blender's own undo")
history.reapply(entry)
history.sync_after_native_undo()
check(entry["state"] == "APPLIED", "history re-syncs after Blender's own redo")

# --- Preserve Geometry off: geometry edits are undoable too
state.preserve_geometry = False
apply("bpy.data.objects['Lid'].data.vertices[0].co.x += 1", "Nudge vertex")
x_after = bpy.data.objects["Lid"].data.vertices[0].co.x
bpy.ops.claude2blender.history_undo()
check(round(bpy.data.objects["Lid"].data.vertices[0].co.x, 4) == round(x_after - 1, 4), "geometry edit undone")
bpy.ops.claude2blender.history_redo()
check(round(bpy.data.objects["Lid"].data.vertices[0].co.x, 4) == round(x_after, 4), "geometry edit redone")
state.preserve_geometry = True

# --- Limit of 10
for i in range(12):
    apply(MAKE_MAT.format(name=f"Try{i}", rgba=(i / 12, 0.5, 0.5, 1)), f"Try {i}")
check(len(history.entries()) == 10 and len(state.history) == 10, "history keeps the last 10 changes")
check(len(backups()) <= 10, f"hidden backups stay bounded ({len(backups())})")

print(f"\nAll {ok} history checks passed on Blender {bpy.app.version_string}")
