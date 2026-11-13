"""v0.5 with the real Claude Code CLI (run with C2B_REAL_CLAUDE=1; takes a few minutes).

Renders two reference photographs with Cycles, then asks Claude through the
panel to match them in a plain working scene:

1. Environment: lighting + background from a studio reference, camera NOT allowed
   -> follow-up "make the shadows softer"
2. Composition: camera angle + exploded arrangement of the existing parts
   -> follow-up "increase the spacing"

Results are rendered to tests/out/ for a visual comparison.
"""

import math
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

if not os.environ.get("C2B_REAL_CLAUDE"):
    print("Set C2B_REAL_CLAUDE=1 to run this test with your signed-in Claude Code.")
    sys.exit(0)

OUT = os.environ.get("C2B_OUT") or os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)
ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("  ok:", msg)


def product(scene):
    """A small CAD-like assembly: base, lid (with a screw parented to it), gear; plus an unrelated shelf."""
    def box(name, size, loc, mat):
        bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
        o = bpy.context.active_object
        o.name = o.data.name = name
        o.scale = size
        o.data.materials.append(mat)
        return o

    grey = bpy.data.materials.new("Body Grey")
    grey.use_nodes = True
    grey.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.35, 0.36, 0.38, 1)
    grey.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.4
    orange = bpy.data.materials.new("Accent Orange")
    orange.use_nodes = True
    orange.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.8, 0.25, 0.04, 1)
    base = box("Base", (0.24, 0.16, 0.06), (0, 0, 0.03), grey)
    lid = box("Lid", (0.22, 0.14, 0.02), (0, 0, 0.07), grey)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.05, depth=0.02, vertices=48, location=(0, 0, 0.09))
    gear = bpy.context.active_object
    gear.name = gear.data.name = "Gear"
    gear.data.materials.append(orange)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.006, depth=0.03, location=(0.09, 0.05, 0.075))
    screw = bpy.context.active_object
    screw.name = screw.data.name = "Screw"
    screw.data.materials.append(orange)
    screw.parent = lid
    screw.matrix_parent_inverse = lid.matrix_world.inverted()
    shelf = box("Shelf", (0.3, 0.3, 0.01), (1.5, 1.5, 0), grey)
    shelf.hide_render = True
    return base, lid, gear, screw, shelf


def setup_render(scene, path, samples=48):
    scene.render.engine = "CYCLES"
    scene.cycles.samples = samples
    scene.cycles.device = "CPU"
    scene.render.resolution_x, scene.render.resolution_y = 480, 360
    scene.render.resolution_percentage = 100
    scene.render.filepath = path
    try:
        scene.cycles.use_denoising = False
    except Exception:
        pass


def add_light(name, kind, loc, target, energy, size, color=(1, 1, 1), size_y=None):
    d = bpy.data.lights.new(name, kind)
    d.energy = energy
    d.color = color
    if kind == "AREA":
        d.size = size
        if size_y:
            d.shape = "RECTANGLE"
            d.size_y = size_y
    o = bpy.data.objects.new(name, d)
    bpy.context.scene.collection.objects.link(o)
    o.location = loc
    direction = Vector(target) - Vector(loc)
    o.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return o


def add_camera(loc, target, lens):
    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    bpy.context.scene.collection.objects.link(cam)
    cam.location = loc
    cam.rotation_euler = (Vector(target) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    cam.data.lens = lens
    bpy.context.scene.camera = cam
    return cam


FRONT_CAM = ((0, -0.75, 0.22), (0, 0, 0.05), 50)

# ---------------------------------------------------------------- reference 1: studio lighting
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
base, lid, gear, screw, shelf = product(sc)
ref_env = os.path.join(OUT, "ref_environment.png")
setup_render(sc, ref_env)
# Light grey sweep
bpy.ops.mesh.primitive_plane_add(size=4, location=(0, 0, 0))
floor = bpy.context.active_object
bpy.ops.mesh.primitive_plane_add(size=4, location=(0, 1.2, 1.0), rotation=(math.pi / 2, 0, 0))
wall = bpy.context.active_object
light_grey = bpy.data.materials.new("Sweep")
light_grey.use_nodes = True
light_grey.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.16, 0.16, 0.17, 1)
light_grey.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.8
floor.data.materials.append(light_grey)
wall.data.materials.append(light_grey)
# Big warm softbox upper-left front, thin cool rim back-right, weak fill
add_light("RefKey", "AREA", (-0.7, -0.35, 0.5), (0, 0, 0.05), 35, 0.6, (1.0, 0.85, 0.68))
add_light("RefRim", "AREA", (0.45, 0.5, 0.3), (0, 0, 0.05), 3, 0.05, (0.7, 0.82, 1.0), size_y=0.5)
w = bpy.data.worlds.new("RefWorld")
w.use_nodes = True
w.node_tree.nodes["Background"].inputs["Color"].default_value = (0.02, 0.02, 0.025, 1)
w.node_tree.nodes["Background"].inputs["Strength"].default_value = 1.0
sc.world = w
add_camera(*FRONT_CAM)
bpy.ops.render.render(write_still=True)
check(os.path.isfile(ref_env), "rendered the studio lighting reference")

# ---------------------------------------------------------------- reference 2: exploded hero view
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
base, lid, gear, screw, shelf = product(sc)
ref_comp = os.path.join(OUT, "ref_composition.png")
setup_render(sc, ref_comp, samples=32)
lid.location.z += 0.08
gear.location.z += 0.16
add_light("Key", "AREA", (-0.6, -0.8, 1.0), (0, 0, 0.1), 150, 1.0)
w = bpy.data.worlds.new("W")
w.use_nodes = True
w.node_tree.nodes["Background"].inputs["Color"].default_value = (0.25, 0.25, 0.27, 1)
sc.world = w
add_camera((0.75, -0.85, 0.75), (0, 0, 0.12), 85)   # high three-quarter view from front-right, long lens
bpy.ops.render.render(write_still=True)
check(os.path.isfile(ref_comp), "rendered the exploded composition reference")
if os.environ.get("C2B_REFS_ONLY"):
    sys.exit(0)

# ---------------------------------------------------------------- working scene (plain)
bpy.ops.wm.read_factory_settings(use_empty=True)
import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import claude_cli, history, scene_tools  # noqa: E402

C = bpy.context
scene = C.scene
state = C.window_manager.claude2blender
base, lid, gear, screw, shelf = product(scene)
user_light = add_light("Light", "POINT", (1, -1, 1.5), (0, 0, 0), 60, 0.1)
cam = add_camera(*FRONT_CAM)
scene.render.engine = "BLENDER_EEVEE_NEXT" if bpy.app.version < (5, 0, 0) else "BLENDER_EEVEE"
scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
C.view_layer.update()

PARTS = (base, lid, gear, screw)


def xf(o):
    return (tuple(o.location), tuple(o.rotation_euler), tuple(o.scale), o.parent)


def geo():
    return {o.name: (history.geometry_signature(o.data), tuple(o.scale)) for o in PARTS + (shelf,)}


XF0 = {o.name: xf(o) for o in bpy.data.objects}
GEO0 = geo()
CAM0 = (xf(cam), cam.data.lens)

claude_cli._login_path = os.path.dirname(shutil.which("claude", path=os.environ["PATH"]) or "")
os.environ.pop("ANTHROPIC_API_KEY", None)


def select(*objs):
    for o in bpy.data.objects:
        o.select_set(o in objs)
    C.view_layer.objects.active = objs[0] if objs else None


def generate(prompt):
    state.prompt = prompt
    bpy.ops.claude2blender.generate()
    claude_cli.pump_until_done(600)
    print("   ->", state.gen_status, "|", state.gen_title, "|", state.gen_detail)
    for icon, text in claude_cli.scene_summary_lines(state):
        print("      ", icon, text)
    if state.scene_limits:
        print("       limits:", state.scene_match, "|", state.scene_limits)
    return state.gen_status == "COMPLETED" and state.proposal_pending


def apply():
    bpy.ops.claude2blender.preview_proposal()
    pv = history.preview_entry()
    if pv is None:
        print("   preview blocked/failed:", state.status)
        return False
    for icon, text in pv["changes"]:
        print("       change:", text)
    bpy.ops.claude2blender.apply_proposal()
    return state.last_apply_ok


def render(name):
    r = scene.render
    saved = (r.engine, r.resolution_x, r.resolution_y, r.filepath)
    setup_render(scene, os.path.join(OUT, name), samples=32)
    bpy.ops.render.render(write_still=True)
    r.engine, r.resolution_x, r.resolution_y, r.filepath = saved


def studio_lights():
    coll = bpy.data.collections.get(scene_tools.STUDIO_COLLECTION)
    return [o for o in coll.objects if o.type == "LIGHT" and o.name in scene.objects and not o.hide_render] if coll else []


render("work_before.png")

# ---------------------------------------------------------------- 1. environment
state.mode = "ENVIRONMENT"
state.references.clear()
ref = state.references.add()
ref.filepath = ref_env
ref.role = "ENVIRONMENT"
select(base, lid, gear)
check(generate("Match the lighting and background of the reference photo. Keep my camera as it is."),
      "real env: Claude staged an environment change")
check(apply(), "real env: previewed and applied within Allowed Changes")
lights = studio_lights()
check(len(lights) >= 2, f"real env: {len(lights)} lights recreated as real Blender lights")
key = max(lights, key=lambda o: o.data.energy * (o.data.size if o.data.type == "AREA" else 1))
center = scene_tools.bounds([base, lid, gear])["center"]
print("   key:", key.name, key.data.type, round(key.data.energy, 1), "W at", tuple(round(v, 2) for v in key.location))
check(key.location.x < center.x and key.location.z > center.z, "real env: key light is upper-left, like the reference")
check((xf(cam), cam.data.lens) == CAM0, "real env: camera untouched (not allowed)")
check(geo() == GEO0 and all(xf(o) == XF0[o.name] for o in PARTS + (shelf,)), "real env: products and shelf untouched")
check((scene.render.resolution_x, scene.render.resolution_y) == (1920, 1080), "real env: resolution preserved")
check(scene.world is not None and scene.world.name != "World" or bpy.data.objects.get("C2B Backdrop") is not None,
      "real env: background recreated (world and/or backdrop)")
render("work_environment.png")
n_lights = len(lights)
sizes = {o.name: (o.data.size if o.data.type == "AREA" else o.data.shadow_soft_size) for o in lights}

check(generate("Make the shadows softer. Keep everything else the same."), "real env follow-up: staged")
check(apply(), "real env follow-up: applied")
lights2 = studio_lights()
sizes2 = {o.name: (o.data.size if o.data.type == "AREA" else o.data.shadow_soft_size) for o in lights2}
print("   sizes before/after:", sizes, sizes2)
check(len(lights2) == n_lights, "real env follow-up: no duplicate lights")
check(any(sizes2.get(n, 0) > s * 1.15 for n, s in sizes.items()), "real env follow-up: a light got larger (softer)")
check((xf(cam), cam.data.lens) == CAM0 and geo() == GEO0, "real env follow-up: camera and geometry untouched")
render("work_environment_softer.png")

# ---------------------------------------------------------------- 2. composition
state.mode = "COMPOSITION"
bpy.ops.claude2blender.new_conversation()
state.references.clear()
ref = state.references.add()
ref.filepath = ref_comp
ref.role = "COMPOSITION"
select(base, lid, gear)
gap0 = gear.matrix_world.translation.z - base.matrix_world.translation.z
check(generate("Recreate the camera angle and the exploded arrangement in the reference with my existing parts."),
      "real comp: Claude staged a composition change")
check(apply(), "real comp: previewed and applied within Allowed Changes")
C.view_layer.update()
gap1 = gear.matrix_world.translation.z - base.matrix_world.translation.z
print("   gap Base->Gear:", round(gap0, 3), "->", round(gap1, 3), "| cam", tuple(round(v, 2) for v in cam.location),
      round(cam.data.lens, 1), "mm")
check(gap1 > gap0 * 1.8, "real comp: parts exploded upward like the reference")
check(cam.location.z > CAM0[0][0][2] + 0.05 and cam.location.x > 0.1, "real comp: camera moved to a high front-right view")
check(cam.data.lens >= 60, "real comp: long lens like the reference")
check(geo() == GEO0 and xf(shelf) == XF0["Shelf"], "real comp: scale/geometry unchanged, unselected shelf untouched")
check(xf(screw)[2:] == XF0["Screw"][2:] and xf(screw)[0] == XF0["Screw"][0], "real comp: child screw kept its local transform")
check(len([o for o in bpy.data.objects if o.type == "CAMERA"]) == 1, "real comp: no extra camera")
render("work_composition.png")

check(generate("Increase the spacing between the exploded components."), "real comp follow-up: staged")
cam_before = (tuple(cam.location), cam.data.lens)
check(apply(), "real comp follow-up: applied")
C.view_layer.update()
gap2 = gear.matrix_world.translation.z - base.matrix_world.translation.z
print("   gap:", round(gap1, 3), "->", round(gap2, 3))
check(gap2 > gap1 * 1.15, "real comp follow-up: spacing increased")
render("work_composition_spaced.png")

bpy.ops.claude2blender.history_undo()
bpy.ops.claude2blender.history_undo()
check(all(xf(o) == XF0[o.name] for o in PARTS) and (xf(cam), cam.data.lens) == CAM0,
      "real: undoing the composition restores parts and camera exactly")

print(f"\nAll {ok} real scene checks passed on Blender {bpy.app.version_string}")
addon.unregister()
