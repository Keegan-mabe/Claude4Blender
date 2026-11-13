"""Headless smoke test. Uses a fresh empty scene; never opens a user .blend.

Run with the `bpy` wheel (pip install bpy) or Blender itself:
    python tests/test_headless.py
    blender --background --factory-startup --python tests/test_headless.py
"""

import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402
import bmesh  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)

import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import bridge, operators  # noqa: E402

ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("  ok:", msg)


# Every icon name used by the add-on must exist in this Blender.
valid = set(bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items.keys())
src_dir = os.path.join(os.path.dirname(HERE), "claude2blender")
used = set()
for name in os.listdir(src_dir):
    if name.endswith(".py"):
        text = open(os.path.join(src_dir, name)).read()
        used |= set(re.findall(r'icon="([A-Z_]+)"', text))
        used |= set(re.findall(r'"([A-Z_]+)",\s*\d\)', text))  # enum item icons
        used |= set(re.findall(r':\s*"((?:OUTLINER_OB|OBJECT)_[A-Z_]+)"', text))
from claude2blender import properties as _p, claude_cli as _cc  # noqa: E402
used |= {item[2] for item in _p.PERMISSION_ITEMS} | {icon for _k, icon in _cc.SCENE_FIELDS}
for name in ("history.py", "ui.py"):
    used |= set(re.findall(r'\(\s*"([A-Z][A-Z_]+)",\s*f?"', open(os.path.join(src_dir, name)).read()))
missing = sorted(used - valid)
check(not missing, f"all {len(used)} icons exist (missing: {missing})")

state = bpy.context.window_manager.claude2blender
check(state.preserve_geometry, "Preserve Geometry is on by default")
check("claude2blender" in bpy.app.driver_namespace, "API is installed in driver_namespace")
api = bpy.app.driver_namespace["claude2blender"]

# Scene: two cubes.
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
body = bpy.context.active_object
body.name = "Body"
bpy.ops.mesh.primitive_cube_add(size=1, location=(3, 0, 0))
lid = bpy.context.active_object
lid.name = "Lid"
body.select_set(True)
bpy.context.view_layer.objects.active = body

state.prompt = "Make the body brushed aluminium and the top face matte black"
state.mode = "MATERIAL"

# Object selection.
sel = bridge.live_selection()
check({o["name"] for o in sel["objects"]} == {"Body", "Lid"}, "selected objects recognised")
check(sel["active_object"] == "Body", "active object recognised")

# Face selection in edit mode.
lid.select_set(False)
bpy.ops.object.mode_set(mode="EDIT")
bm = bmesh.from_edit_mesh(body.data)
bm.faces.ensure_lookup_table()
for f in bm.faces:
    f.select = False
bm.faces[5].select = True
bm.faces[2].select = True
bmesh.update_edit_mesh(body.data)
faces = bridge.face_selection(bpy.context)
check(len(faces) == 1 and faces[0]["selected_faces"] == 2, "selected faces recognised in Edit Mode")
check(faces[0]["faces"] == "2,5", "face indices compressed")
check(bridge.expand_indices("0-2,5") == [0, 1, 2, 5], "expand_indices")

# Reference image.
img_path = os.path.join(tempfile.mkdtemp(), "brushed.png")
img = bpy.data.images.new("tmp", 8, 8)
img.filepath_raw = img_path
img.file_format = "PNG"
img.save()
bpy.data.images.remove(img)
operators._add_reference(state, img_path, "MATERIAL")
check(len(state.references) == 1, "reference added")

# Send.
request, text = bridge.send_request(bpy.context)
check(request["request_id"] == 1 and request["mode"] == "material", "request built")
check(request["selection"]["faces"][0]["object"] == "Body", "request carries face targets")
check(request["references"][0]["exists"], "request carries reference")
check("stage_proposal" in text and "#1" in text, "clipboard message explains the MCP steps")
check(api.get_request()["prompt"] == state.prompt, "Claude can read the request through the API")
check(os.path.isfile(bridge.request_file_path()), "request written to config folder")

bpy.ops.object.mode_set(mode="OBJECT")

# Stage and apply a material change that targets the faces.
code = '''
mat = bpy.data.materials.new("C2B Matte Black")
obj = bpy.data.objects[request["selection"]["faces"][0]["object"]]
obj.data.materials.append(mat)
slot = len(obj.data.materials) - 1
for i in expand_indices(request["selection"]["faces"][0]["faces"]):
    obj.data.polygons[i].material_index = slot
obj.location.z += 0.5   # moving is allowed under Preserve Geometry
'''
try:
    api.stage_proposal("this is not python(", "bad")
    check(False, "syntax error rejected")
except SyntaxError:
    check(True, "stage_proposal rejects code with syntax errors")
api.stage_proposal(code, "Matte black on 2 faces")
check(state.proposal_pending, "proposal staged")
res = bpy.ops.claude2blender.apply_proposal()
check(res == {"FINISHED"} and state.last_apply_ok, "apply succeeded")
check(body.data.polygons[5].material_index == 0 or "C2B Matte Black" in [m.name for m in body.data.materials],
      "material assigned")
check(body.data.polygons[5].material_index == len(body.data.materials) - 1, "face material index set")

# Preserve Geometry blocks a mesh edit.
before = operators.geometry_snapshot()
api.stage_proposal("bpy.data.objects['Lid'].data.vertices[0].co.x += 1", "nudge a vertex")
bpy.ops.claude2blender.apply_proposal()
check(not state.last_apply_ok and "Preserve Geometry" in state.status, "Preserve Geometry catches mesh edits")
check(state.proposal_pending, "blocked proposal stays pending for discard/retry")

api.stage_proposal("bpy.data.objects['Lid'].scale.x = 2", "scale")
bpy.ops.claude2blender.apply_proposal()
check("scale changed" in state.status, "Preserve Geometry catches scale changes")

state.preserve_geometry = False
api.stage_proposal("bpy.data.objects['Lid'].scale.x = 3", "scale, unprotected")
bpy.ops.claude2blender.apply_proposal()
check(state.last_apply_ok, "with Preserve Geometry off the change applies")

api.stage_proposal("raise RuntimeError('boom')", "fails")
try:
    bpy.ops.claude2blender.apply_proposal()  # ERROR reports raise when called from Python
except RuntimeError:
    pass
check("Apply failed" in state.status, "errors in Claude's code are reported")

bpy.ops.claude2blender.discard_proposal()
check(not state.proposal_pending, "discard clears proposal")

addon.unregister()
check("claude2blender" not in bpy.app.driver_namespace, "unregister cleans up")
addon.register()
addon.unregister()
check(True, "re-register / unregister cycle")
print(f"\nAll {ok} checks passed on Blender {bpy.app.version_string}")
