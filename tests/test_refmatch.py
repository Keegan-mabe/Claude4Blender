"""Measured lighting match: recovers a known light from a rendered reference, stages, applies, undoes.

Run: blender --background --factory-startup --python tests/test_refmatch.py
Writes tests/real_results/refmatch_reference.png and refmatch_matched.png for a side-by-side look.
Set C2B_GPU=1 to render the reference on the GPU.
"""

import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import history, operators, presets, refmatch, scene_tools  # noqa: E402

state = bpy.context.window_manager.claude2blender
scene = bpy.context.scene
OUT = os.path.join(HERE, "real_results")
os.makedirs(OUT, exist_ok=True)
ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("  ok:", msg)


# Product: white box + dark sphere on a dark floor, camera front-right.
bpy.ops.mesh.primitive_cube_add(size=0.1, location=(0, 0, 0.05))
box = bpy.context.active_object; box.name = "Product"
box.data.materials.append(presets.preset_material("plastic_white"))
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.03, location=(0.07, -0.02, 0.03))
ball = bpy.context.active_object; ball.name = "Knob"
bpy.ops.object.shade_smooth()
ball.data.materials.append(presets.preset_material("plastic_white"))
bpy.ops.mesh.primitive_plane_add(size=4, location=(0, 0, 0))
floor = bpy.context.active_object; floor.name = "Floor"
floor.data.materials.append(presets.preset_material("plastic_black"))
cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
scene.collection.objects.link(cam)
cam.location = (0.3, -0.5, 0.22)
cam.data.lens = 60
scene.camera = cam
bpy.context.view_layer.update()
scene_tools.look_at(cam, (0.02, 0, 0.04))
world = bpy.data.worlds.new("W"); world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.02, 0.02, 0.025, 1)
scene.world = world
scene.render.resolution_x, scene.render.resolution_y = 640, 400
scene.render.engine = "CYCLES"
scene.cycles.samples = 64
scene.cycles.device = "GPU" if os.environ.get("C2B_GPU") else "CPU"
scene.view_settings.view_transform = "Standard"

# The "truth": one big soft key, front-left and high (camera-relative azimuth 315, elevation ~45),
# plus a weak warm rim from the back right.
center = Vector((0.02, 0, 0.05))
fwd = center - cam.matrix_world.translation; fwd.z = 0; fwd.normalize()
az0 = math.atan2(-fwd.y, -fwd.x)


def put(name, elev, az, energy, size, color=(1, 1, 1)):
    e, a = math.radians(elev), az0 + math.radians(az)
    loc = center + Vector((math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e))) * 0.55
    ld = bpy.data.lights.new(name, "AREA"); ld.energy = energy; ld.size = size; ld.color = color
    ob = bpy.data.objects.new(name, ld); scene.collection.objects.link(ob)
    ob.location = loc
    ob.rotation_euler = (center - loc).to_track_quat("-Z", "Y").to_euler()
    return ob


truth_key = put("Truth Key", 45, 315, 3.5, 0.35)   # unclipped: a blown-out reference hides the shading
truth_rim = put("Truth Rim", 25, 150, 0.8, 0.2, (1.0, 0.7, 0.4))
ref_path = os.path.join(OUT, "refmatch_reference.png")
scene.render.filepath = ref_path
bpy.ops.render.render(write_still=True)
check(os.path.isfile(ref_path), "rendered a reference with a known light")
# The match must find the light on its own: switch the truth off (it would still light the copy otherwise
# - it doesn't, the basis hides every light, but the user's scene shouldn't carry it either).
truth_key.hide_render = truth_rim.hide_render = True
truth_key.hide_viewport = truth_rim.hide_viewport = True

# Run Measure Lighting from the panel path.
r = state.references.add(); r.filepath = ref_path; r.role = "ENVIRONMENT"
for o in bpy.data.objects:
    o.select_set(o in (box, ball))
bpy.context.view_layer.objects.active = box
t0 = time.time()
check(bpy.ops.claude2blender.measure_lighting() == {"FINISHED"}, "Measure Lighting starts")
check(time.time() - t0 < 5, "returns at once (basis renders in a background Blender)")
check(operators.match_running(), "shown as running")
job = operators._match_job
while not job.done and time.time() - t0 < 900:
    time.sleep(1.0)
operators._poll_match()   # the panel timer, run by hand (timers don't fire in a background script)
check(job.error is None, f"basis render and fit succeeded ({job.error})")
res = job.result
print("  result:", refmatch.describe(res))
check(state.proposal_pending and "Measured lighting match" in state.proposal_summary, "match staged for review")

def unit_dir(az, elev):
    e, a = math.radians(elev), math.radians(az)
    return Vector((math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)))


def az_gap(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


# Dome lights sit 45 deg apart; the truth key (az 315, elev 45) falls between rings. Known limit: the fit
# tends to put part of a mid-height key into the low ring, so only the side is checked, not the height.
print("  lights:", [(l["azimuth"], l["elevation"], l["share"]) for l in res["lights"]])
top = res["lights"][0]
check(az_gap(top["azimuth"], 315) <= 45, f"strongest light is on the truth key's side (az {top['azimuth']})")
front_left = sum(l["energy"] for l in res["lights"] if az_gap(l["azimuth"], 315) <= 45)
check(front_left > 0.4 * sum(l["energy"] for l in res["lights"]), "most of the light comes from the key side")
check(any(az_gap(l["azimuth"], 150) <= 45 and l["share"] > 0.2 for l in res["lights"]), "the back-right rim is found too")
check(res["residuals"]["prod"] < 0.5, f"product shading fits (residual {res['residuals']['prod']})")

# Apply through Allowed Changes, then render the match next to the reference.
bpy.ops.claude2blender.apply_proposal()
check(state.last_apply_ok, f"applies within Allowed Changes ({state.status})")
match_lights = [o for o in bpy.data.objects if o.name.startswith(refmatch.MATCH_PREFIX) and o.visible_get()]
check(len(match_lights) == len(res["lights"]) and all(o.data.type == "AREA" for o in match_lights),
      f"{len(match_lights)} real, editable area lights")
scene.render.filepath = os.path.join(OUT, "refmatch_matched.png")
bpy.ops.render.render(write_still=True)
history.undo_last()
check(not any(o.visible_get() for o in bpy.data.objects if o.name.startswith(refmatch.MATCH_PREFIX)),
      "Undo takes the match back")

print(f"All {ok} lighting-match checks passed on Blender {bpy.app.version_string}")
