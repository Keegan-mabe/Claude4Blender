"""v0.4 Visual Material Matching: face/object isolation, preview, save safety, image prep, material summary.

Uses a fresh scene and a stand-in `claude` CLI. Set C2B_REAL_CLAUDE=1 to also send a rendered
material photograph to the real, signed-in Claude Code CLI and check the analysis.
"""

import json
import os
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402
import bmesh  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import claude_cli, history, materials, ui  # noqa: E402

api = bpy.app.driver_namespace["claude2blender"]
state = bpy.context.window_manager.claude2blender
scene = bpy.context.scene
tmp = tempfile.mkdtemp()
ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("  ok:", msg)


class _L:
    def __getattr__(self, name):
        return lambda *a, **k: _L()


def draw_panels():
    for cls in (ui.C2B_PT_main, ui.C2B_PT_apply, ui.C2B_PT_history):
        p = type("P", (), {})()
        p.layout = _L()
        cls.draw(p, bpy.context)


def face_mats(obj):
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
        out = face_mats(obj)
        bpy.ops.object.mode_set(mode="EDIT")
        return out
    me = obj.data
    return [(me.materials[p.material_index].name if me.materials and me.materials[p.material_index] else None)
            for p in me.polygons]


def uv_sig(obj):
    if bpy.context.mode != "OBJECT":   # edit-mode meshes aren't synced to obj.data
        bpy.ops.object.mode_set(mode="OBJECT")
        sig = history.geometry_signature(obj.data)
        bpy.ops.object.mode_set(mode="EDIT")
        return sig
    return history.geometry_signature(obj.data)


def select_faces(obj, indices):
    for o in bpy.data.objects:
        o.select_set(o == obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.context.tool_settings.mesh_select_mode = (False, False, True)   # face select, like a user would
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    for f in bm.faces:
        f.select_set(False)
    for f in bm.faces:
        if f.index in indices:
            f.select_set(True)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)


def select_objects(*objs):
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    for o in bpy.data.objects:
        o.select_set(o in objs)
    bpy.context.view_layer.objects.active = objs[0]


def stage(code, summary):
    bridge = sys.modules["claude2blender.bridge"]
    bridge.send_request(bpy.context, copy_to_clipboard=False)
    api.stage_proposal(code, summary)


MAT_CODE = '''
mat = bpy.data.materials.get("{name}") or bpy.data.materials.new("{name}")
mat.use_nodes = True
mat.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = {rough}
assign_material(mat)
'''

# ---------------------------------------------------------------- scene: a CAD-like part with 2 materials and UVs
bpy.ops.mesh.primitive_cube_add(size=2)
part = bpy.context.active_object
part.name = "Housing"
paint = bpy.data.materials.new("Paint")
rubber = bpy.data.materials.new("Rubber")
part.data.materials.append(paint)
part.data.materials.append(rubber)
for p in part.data.polygons:
    p.material_index = 1 if p.index in (0, 1) else 0
bpy.ops.mesh.primitive_cube_add(location=(4, 0, 0))
bare = bpy.context.active_object
bare.name = "Bracket"                      # no materials at all
bpy.ops.object.duplicate(linked=True)
twin = bpy.context.active_object
twin.name = "Bracket.Twin"                 # linked duplicate: shares Bracket's mesh
twin.location.x = 8
geo = {o.name: uv_sig(o) for o in bpy.data.objects}
before_faces = face_mats(part)

# ---------------------------------------------------------------- 1. Edit Mode: only the selected faces
select_faces(part, {2, 3})
stage(MAT_CODE.format(name="Graphite", rough=0.6), "Graphite on 2 faces")
bpy.ops.claude2blender.apply_proposal()
after = face_mats(part)
check(after[2] == after[3] == "Graphite", "selected faces get the new material")
check(all(after[i] == before_faces[i] for i in range(6) if i not in (2, 3)),
      "every unselected face keeps its material (Paint and Rubber faces untouched)")
check([m.name for m in part.data.materials][:2] == ["Paint", "Rubber"], "existing slots keep their order")
check(uv_sig(part) == geo["Housing"], "vertices, topology and UVs unchanged")
check(bpy.context.mode == "EDIT_MESH", "returns to Edit Mode after Apply")

bpy.ops.claude2blender.history_undo()
check(face_mats(part) == before_faces and len(part.data.materials) == 2, "Undo restores every face exactly")
bpy.ops.claude2blender.history_redo()
check(face_mats(part)[2] == "Graphite", "Redo puts the face material back")

# Mesh with no materials: unselected faces must stay "no material"
select_faces(bare, {4})
stage(MAT_CODE.format(name="Orange", rough=0.4), "Orange on 1 face")
bpy.ops.claude2blender.apply_proposal()
fm = face_mats(bare)
check(fm[4] == "Orange" and all(fm[i] is None for i in range(6) if i != 4),
      "on a mesh without materials only the selected face changes")
check(any("shares its mesh" in n for n in history.entries()[-1]["notes"]),
      "linked duplicate sharing the faces is reported")

# ---------------------------------------------------------------- 2. Object Mode: only selected objects
select_objects(twin)
twin_before = face_mats(bare)
stage(MAT_CODE.format(name="Steel", rough=0.3), "Steel on twin")
bpy.ops.claude2blender.apply_proposal()
check(all(s.link == "OBJECT" and s.material.name == "Steel" for s in twin.material_slots),
      "linked duplicate gets an object-level override")
check(face_mats(bare) == twin_before and bare.material_slots[1].material.name == "Orange",
      "the other linked object keeps its materials")
select_objects(part)
stage(MAT_CODE.format(name="Satin Black", rough=0.5), "Satin black housing")
bpy.ops.claude2blender.apply_proposal()
check(set(face_mats(part)) == {"Satin Black"}, "Object Mode: whole selected object")
check(bare.material_slots[1].material.name == "Orange", "unselected objects untouched")
check(all(uv_sig(o) == geo[o.name] for o in bpy.data.objects), "no geometry or UVs changed anywhere")

# Preserve Geometry now covers UVs
stage("bpy.data.objects['Housing'].data.uv_layers[0].data[0].uv = (0.3, 0.3)", "nudge a UV")
bpy.ops.claude2blender.apply_proposal()
check(not state.last_apply_ok and "UVs" in state.status, "Preserve Geometry blocks UV edits")
bpy.ops.claude2blender.discard_proposal()

# ---------------------------------------------------------------- 3. Live preview
select_objects(part)
hist_len = len(history.entries())
mats_before = len(bpy.data.materials)
faces_before_preview = face_mats(part)
stage(MAT_CODE.format(name="Preview Copper", rough=0.25), "Copper preview")
bpy.ops.claude2blender.preview_proposal()
check(history.preview_entry() is not None and set(face_mats(part)) == {"Preview Copper"},
      "Preview shows the material on the real object")
check(len(history.entries()) == hist_len and state.proposal_pending, "preview is not in History and stays staged")
draw_panels()

# Saving during preview: the file must not contain the preview or hidden backups
saved = os.path.join(tmp, "saved.blend")
bpy.ops.wm.save_as_mainfile(filepath=saved, copy=True)
with bpy.data.libraries.load(saved) as (data_from, _to):
    saved_mats = list(data_from.materials)
check("Preview Copper" not in saved_mats, "saved file does not contain the preview material")
check(not [m for m in saved_mats if m.startswith(".c2b")], "saved file contains no hidden History backups")
check(set(face_mats(part)) == {"Preview Copper"}, "preview is back on screen after saving")

bpy.ops.claude2blender.end_preview()
check(face_mats(part) == faces_before_preview and history.preview_entry() is None,
      "End Preview restores the object exactly")
check(len(bpy.data.materials) == mats_before, "End Preview removes the preview material")
check(state.proposal_pending, "the change is still staged after End Preview")

bpy.ops.claude2blender.preview_proposal()
n_mats = len(bpy.data.materials)
bpy.ops.claude2blender.apply_proposal()
check(set(face_mats(part)) == {"Preview Copper"} and len(bpy.data.materials) == n_mats,
      "Apply keeps the previewed result without running the code again")
check(len(history.entries()) == hist_len + 1 and history.entries()[-1]["summary"] == "Copper preview",
      "applied preview joins History")
bpy.ops.claude2blender.history_undo()
check(set(face_mats(part)) == {"Satin Black"}, "Undo after an applied preview works")
bpy.ops.claude2blender.history_redo()

# Discard during preview reverts it
stage(MAT_CODE.format(name="Throwaway", rough=0.9), "throwaway")
bpy.ops.claude2blender.preview_proposal()
bpy.ops.claude2blender.discard_proposal()
check("Throwaway" not in bpy.data.materials and set(face_mats(part)) == {"Preview Copper"},
      "Discard during preview reverts it")

# Blender's own undo past a preview (simulated) clears it
stage(MAT_CODE.format(name="Undo Me", rough=0.9), "undo me")
bpy.ops.claude2blender.preview_proposal()
history.revert(history.preview_entry())
history.preview_entry()["state"] = "APPLIED"
history.sync_after_native_undo()
check(history.preview_entry() is None, "Ctrl+Z past a preview clears it")
bpy.ops.claude2blender.discard_proposal()

# ---------------------------------------------------------------- 4. Follow-up context: current material
bridge = sys.modules["claude2blender.bridge"]
select_objects(part)
req = bridge.build_request(bpy.context)
desc = materials.target_materials(req)
check(desc and desc[0]["name"] == "Preview Copper" and any(n["type"] == "ShaderNodeBsdfPrincipled" for n in desc[0]["nodes"]),
      "current node tree is described for refinements")
pr = next(n for n in desc[0]["nodes"] if n["type"] == "ShaderNodeBsdfPrincipled")
check(abs(pr["inputs"]["Roughness"] - 0.25) < 1e-3, "described values match the material")

# ---------------------------------------------------------------- 5. Image preparation off the main thread
big = os.path.join(tmp, "photo_big.png")
img = bpy.data.images.new("big", 2600, 1400)
import random  # noqa: E402
random.seed(1)
px = [random.random() for _ in range(2600 * 1400 * 4)]
img.pixels = px
img.filepath_raw = big
img.file_format = "PNG"
img.save()
bpy.data.images.remove(img)
exr = os.path.join(tmp, "studio.exr")
img = bpy.data.images.new("hdr", 64, 64, float_buffer=True)
img.pixels = [0.5, 0.25, 0.1, 1.0] * (64 * 64)
img.filepath_raw = exr
img.file_format = "OPEN_EXR"
img.save()
bpy.data.images.remove(img)
request = {"references": [{"path": big, "exists": True, "role": "material"},
                          {"path": exr, "exists": True, "role": "environment"}]}
images_before = len(bpy.data.images)
paths, conversions = claude_cli.plan_images(request)
check(len(conversions) == 2 and all(p.endswith((".jpg", ".png")) for p in paths), "large photo and EXR planned for conversion")
check(len(bpy.data.images) == images_before, "planning decodes nothing in Blender")
t0 = time.time()
failed = claude_cli.run_conversions(conversions)
check(not failed and all(os.path.isfile(p) for p in paths), f"converted in a separate process ({time.time() - t0:.1f}s)")
probe = bpy.data.images.load(paths[0])
check(max(probe.size) <= claude_cli.MAX_IMAGE_DIM, f"large photo downsized to {tuple(probe.size)}")
bpy.data.images.remove(probe)

# ---------------------------------------------------------------- 6. Generate → material summary card (stand-in CLI)
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\n")
def emit(o): print(json.dumps(o), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "s-1"})
out = {"summary": "Bead-blasted anodised aluminium on Housing",
       "code": "mat = bpy.data.materials.get('Anodised Graphite') or bpy.data.materials.new('Anodised Graphite')\nmat.use_nodes = True\nassign_material(mat)\n",
       "message": "Long technical explanation " * 20,
       "material": {"name": "Anodised Graphite", "base_color_hex": "#2B2D30", "roughness": 0.42, "metallic": 1.0,
                    "surface": "fine bead-blast grain, ~0.1 mm", "match": "approximate",
                    "limitations": "Exact anodising colour shift needs a measured colour sample."}}
emit({"type": "result", "subtype": "success", "is_error": False, "structured_output": out, "result": json.dumps(out), "session_id": "s-1"})
'''
bindir = os.path.join(tmp, "bin")
os.makedirs(bindir)
fake = os.path.join(bindir, "claude")
open(fake, "w").write(FAKE)
os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
os.environ["FAKE_LOG"] = os.path.join(tmp, "log.jsonl")
claude_cli._login_path = bindir
state.references.clear()
r = state.references.add()
r.filepath = big
state.prompt = "Match this finish"
select_objects(part)
bpy.ops.claude2blender.generate()
claude_cli.pump_until_done(60)
args = json.loads(open(os.environ["FAKE_LOG"]).read().splitlines()[-1])
prompt = args[args.index("-p") + 1]
check(state.gen_status == "COMPLETED" and state.proposal_pending, "generated material staged")
check(paths[0] in prompt and os.path.dirname(paths[0]) in args, "the downsized photo itself is sent to Claude (Read + --add-dir)")
check('"target_materials"' in prompt and "Preview Copper" in prompt, "current material sent for refinement")
check(state.mat_name == "Anodised Graphite" and state.mat_line == "Roughness 0.42 · Metallic 1.00"
      and state.mat_has_swatch and abs(state.mat_swatch[0] - 0x2B / 255) < 1e-3, "compact material summary + swatch")
check(state.mat_match == "approximate" and "colour sample" in state.mat_limits, "match limitations kept visible")
check(not state.gen_show_details, "technical explanation collapsed by default")
draw_panels()

# ---------------------------------------------------------------- 7. Optional: real Claude Code on a rendered photo
if os.environ.get("C2B_REAL_CLAUDE"):
    import shutil
    photo = os.path.join(tmp, "copper_photo.png")
    # Render a real material photograph: brushed copper sphere in a simple studio.
    rs = bpy.data.scenes.new("PhotoStudio")
    rs.render.engine = "CYCLES"
    rs.cycles.samples = 48
    rs.cycles.device = "CPU"
    rs.render.resolution_x = rs.render.resolution_y = 384
    rs.render.filepath = photo
    world = bpy.data.worlds.new("Studio")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.6
    rs.world = world
    me = bpy.data.meshes.new("sphere")
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=64, v_segments=32, radius=1.0)
    for f in bm.faces:
        f.smooth = True
    bm.to_mesh(me)
    sphere = bpy.data.objects.new("Sphere", me)
    rs.collection.objects.link(sphere)
    copper = bpy.data.materials.new("TruthCopper")
    copper.use_nodes = True
    bsdf = copper.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.93, 0.45, 0.25, 1)
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.3
    me.materials.append(copper)
    light_data = bpy.data.lights.new("Key", "AREA")
    light_data.energy = 400
    light_data.size = 3
    light = bpy.data.objects.new("Key", light_data)
    light.location = (2.5, -2.5, 3)
    light.rotation_euler = (0.8, 0, 0.8)
    rs.collection.objects.link(light)
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    cam.location = (0, -4.2, 0.4)
    cam.rotation_euler = (1.48, 0, 0)
    rs.collection.objects.link(cam)
    rs.camera = cam
    window_scene = bpy.context.window.scene if bpy.context.window else None
    bpy.ops.render.render(write_still=True, scene=rs.name)
    check(os.path.isfile(photo), "rendered a copper material photograph for the test")

    claude_cli._login_path = os.path.dirname(shutil.which("claude", path=os.environ["PATH"]) or "")
    os.environ.pop("ANTHROPIC_API_KEY", None)
    bpy.ops.claude2blender.discard_proposal()
    bpy.ops.claude2blender.new_conversation()
    state.references.clear()
    r = state.references.add()
    r.filepath = photo
    state.mode = "MATERIAL"
    state.prompt = "Recreate this material for a product render"
    select_objects(part)
    geo = {o.name: uv_sig(o) for o in bpy.data.objects if o.type == "MESH"}   # (the blocked UV edit above isn't rolled back headless)
    select_faces(part, {0, 1, 2})
    untouched = face_mats(part)
    bpy.ops.claude2blender.generate()
    claude_cli.pump_until_done(300)
    print("   real:", state.gen_status, "|", state.gen_detail, "|", state.gen_title)
    print("   material:", state.mat_name, "|", state.mat_line, "|", state.mat_surface, "|", state.mat_match, "|", state.mat_limits)
    print("   swatch (sRGB):", tuple(round(c, 2) for c in state.mat_swatch))
    check(state.gen_status == "COMPLETED" and state.proposal_pending, "real: material staged from the photo")
    sw = state.mat_swatch
    check(sw[0] > sw[1] > sw[2], "real: Claude read a copper/orange hue from the photo")
    check("Metallic" in state.mat_line and float(state.mat_line.split("Metallic ")[1]) >= 0.5,
          "real: Claude identified a metal")
    bpy.ops.claude2blender.preview_proposal()
    check(history.preview_entry() is not None, "real: preview runs")
    bpy.ops.claude2blender.apply_proposal()
    fm = face_mats(part)
    check(state.last_apply_ok and fm[0] == fm[1] == fm[2] and fm[0] != untouched[0]
          and fm[3:] == untouched[3:], "real: applied to exactly the 3 selected faces")
    check(all(uv_sig(o) == geo[o.name] for o in bpy.data.objects if o.name in geo), "real: geometry and UVs untouched")
    first_name, first_line = state.mat_name, state.mat_line
    rough1 = float(first_line.split("Roughness ")[1].split(" ")[0])
    state.prompt = "Make the surface more matte. Keep everything else the same."
    bpy.ops.claude2blender.generate()
    claude_cli.pump_until_done(300)
    print("   follow-up:", state.gen_status, "|", state.mat_name, "|", state.mat_line)
    rough2 = float(state.mat_line.split("Roughness ")[1].split(" ")[0])
    check(state.gen_status == "COMPLETED" and rough2 > rough1, "real: follow-up made it more matte")
    check(state.mat_name == first_name, "real: follow-up kept the same material")
    bpy.ops.claude2blender.apply_proposal()
    check(state.last_apply_ok and face_mats(part)[3:] == untouched[3:], "real: refinement still only on the selected faces")
    bpy.ops.claude2blender.history_undo()
    check(face_mats(part)[0] == fm[0], "real: undo after refinement returns to the first result")

addon.unregister()
print(f"\nAll {ok} material checks passed on Blender {bpy.app.version_string}")
