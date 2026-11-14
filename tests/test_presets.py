"""Studio presets: every preset builds, packs are found or fall back, Use Preset stages, applies and undoes.

Run: blender --background --factory-startup --python tests/test_presets.py
Set C2B_TEXTURE_LIBRARY to a folder with texture packs to also check the textured path.
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import history, presets  # noqa: E402

state = bpy.context.window_manager.claude2blender
ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("  ok:", msg)


# 1) every preset builds with no packs installed
empty = tempfile.mkdtemp()
for key in presets.PRESETS:
    mat = presets.preset_material(key, library=empty)
    bsdf = [n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"]
    check(len(bsdf) == 1 and mat["c2b_preset"] == key and not mat["c2b_textured"], f"{key} builds (procedural)")

# 2) rebuilding keeps one material with the same name
n_before = len(bpy.data.materials)
presets.preset_material("soft_touch_black", library=empty)
check(len(bpy.data.materials) == n_before, "rebuild reuses the material by name")

# 3) a fake pack in a nested 2K folder is found and wired
lib = tempfile.mkdtemp()
pack_dir = os.path.join(lib, "downloads", "worn-old-plastic-bl", "2K")
os.makedirs(pack_dir)
img = bpy.data.images.new("tmp", 4, 4)
for stem in ("worn-old-plastic_roughness", "worn-old-plastic_height"):
    img.filepath_raw = os.path.join(pack_dir, stem + ".png")
    img.file_format = "PNG"
    img.save()
check(set(presets.pack_maps("worn_plastic", lib)) == {"rough", "height"}, "pack found two levels deep")
mat = presets.preset_material("plastic_worn_black", library=lib)
texs = [n for n in mat.node_tree.nodes if n.bl_idname == "ShaderNodeTexImage"]
check(mat["c2b_textured"] and len(texs) == 2 and all(t.projection == "BOX" for t in texs), "pack maps box-projected")

real = os.environ.get("C2B_TEXTURE_LIBRARY")
if real:
    for k, folder, found in presets.pack_status(real):
        print(f"  pack {folder}: {'found' if found else 'missing'}")

# 4) Use Preset stages a proposal; Apply assigns it; History undo restores
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
state.mode = "MATERIAL"
state.preset = "steel_brushed"
check(bpy.ops.claude2blender.stage_preset() == {"FINISHED"} and state.proposal_pending, "Use Preset stages a change")
check("preset_material('steel_brushed')" in state.proposal_code, "staged code calls preset_material")
bpy.ops.claude2blender.apply_proposal()
check(cube.material_slots and cube.material_slots[0].material.name == "C2B Brushed steel", "Apply puts the preset on the cube")
history.undo_last()
check(not cube.material_slots or cube.material_slots[0].material is None, "Undo takes it back")

# 5) nothing selected -> refused
for o in bpy.data.objects:
    o.select_set(False)
bpy.context.view_layer.objects.active = None
check(bpy.ops.claude2blender.stage_preset() == {"CANCELLED"}, "no selection: nothing staged")

print(f"test_presets: {ok} checks passed")
