"""Studio styles: Dark / High-key / Gradient stage, pass Allowed Changes, switch cleanly, undo, scale with the product.

Run: blender --background --factory-startup --python tests/test_styles.py
Set C2B_STYLE_RENDERS=<folder> to also render a small Cycles still of each style there.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import history, presets, scene_tools  # noqa: E402

state = bpy.context.window_manager.claude2blender
scene = bpy.context.scene
ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("  ok:", msg)


def style_lights(visible_only=True):
    return sorted(o.name for o in bpy.data.objects if o.type == "LIGHT" and o.name.startswith("C2B Style ")
                  and (o.visible_get() or not visible_only))


# A small product (~12 cm) with a camera in front, plus a light of the user's own.
bpy.ops.mesh.primitive_cube_add(size=0.1, location=(0, 0, 0.05))
product = bpy.context.active_object
product.name = "Product"
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.03, location=(0.07, 0, 0.03))
knob = bpy.context.active_object
knob.name = "Knob"
cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
scene.collection.objects.link(cam)
cam.location = (0.25, -0.45, 0.2)
scene.camera = cam
bpy.context.view_layer.update()
scene_tools.look_at(cam, (0.02, 0, 0.05))
own = bpy.data.objects.new("My Light", bpy.data.lights.new("My Light", "POINT"))
scene.collection.objects.link(own)
own_energy = own.data.energy
for o in bpy.data.objects:
    o.select_set(o in (product, knob))
bpy.context.view_layer.objects.active = product


def use_style(name):
    state.studio_style = name
    assert bpy.ops.claude2blender.stage_style() == {"FINISHED"}
    bpy.ops.claude2blender.apply_proposal()
    return state.last_apply_ok, state.status


# 1) Dark
done, status = use_style("dark")
check(done, f"Dark applies through Allowed Changes ({status})")
check(state.mode == "ENVIRONMENT", "Use Style switches to Environment mode")
check(style_lights() == ["C2B Style Key", "C2B Style Rim L", "C2B Style Rim R", "C2B Style Top"], "Dark: key, two rims, top")
check(scene.world.name == "C2B Studio World", "studio world assigned")
floor = bpy.data.objects["C2B Floor"]
check(floor.visible_get() and floor.data.materials[0].diffuse_color[0] < 0.05, "dark floor")
key = bpy.data.objects["C2B Style Key"]
center = scene_tools.bounds([product, knob])["center"]
view = (center - cam.location).normalized()     # depth along the camera's view direction
depth = lambda o: (o.location - cam.location).dot(view)  # noqa: E731
check(key.location.z > center.z and depth(key) < depth(product) < depth(bpy.data.objects["C2B Style Rim L"]),
      "key is above and on the camera side, rims behind the product")
check(own.visible_get() and own.data.energy == own_energy, "your own light untouched")
dark_key_w = key.data.energy
if os.environ.get("C2B_STYLE_RENDERS"):
    product.data.materials.append(presets.preset_material("plastic_white"))
    knob.data.materials.append(presets.preset_material("soft_touch_black"))

# 2) Switching to High-key reuses names, hides the unused, swaps floor for sweep
n_lights = len([o for o in bpy.data.objects if o.type == "LIGHT"])
done, status = use_style("highkey")
check(done, f"High-key applies ({status})")
check(len([o for o in bpy.data.objects if o.type == "LIGHT"]) == n_lights, "no duplicate lights")
check(bpy.data.objects["C2B Sweep"].visible_get() and not bpy.data.objects["C2B Floor"].visible_get(),
      "sweep shown, flat floor hidden")
check(bpy.data.objects["C2B Style Key"].data.energy < dark_key_w, "high-key lights are softer")

# 3) Gradient: different rig, glowing wall
done, status = use_style("gradient")
check(done, f"Gradient applies ({status})")
check(style_lights() == ["C2B Style Blue Key", "C2B Style Orange Fill", "C2B Style Red Rim"], "Gradient rig, Dark rig hidden")
wall = bpy.data.objects["C2B Gradient Wall"]
check(wall.visible_get() and any(n.bl_idname == "ShaderNodeEmission" for n in wall.data.materials[0].node_tree.nodes),
      "glowing gradient wall")
check((wall.location - cam.location).length > (product.location - cam.location).length, "wall is behind the product")

# 4) Undo goes back exactly
history.undo_last()
check(style_lights() == ["C2B Style Key", "C2B Style Rim L", "C2B Style Rim R", "C2B Style Top"]
      and not wall.visible_get(), "Undo restores the High-key rig")
history.undo_last()
history.undo_last()
check(not style_lights() and scene.world is None or scene.world.name != "C2B Studio World", "Undo back to the start")

# 5) Scale: a 10x bigger product gets lights 10x further and 100x stronger
if os.environ.get("C2B_STYLE_RENDERS"):
    out = os.environ["C2B_STYLE_RENDERS"]
    os.makedirs(out, exist_ok=True)
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 48
    scene.cycles.device = "GPU" if os.environ.get("C2B_GPU") else "CPU"
    scene.render.resolution_x, scene.render.resolution_y = 640, 400
    cam.data.lens = 70
    own.hide_render = True
    for name in scene_tools.STUDIO_STYLES:
        use_style(name)
        scene_tools.frame(cam, [product, knob], margin=1.6)
        scene.render.filepath = os.path.join(out, f"style_{name}.png")
        bpy.ops.render.render(write_still=True)
        print("  render:", scene.render.filepath)

for o in list(bpy.data.objects):
    o.select_set(False)
r_small = scene_tools.bounds([product])["radius"]
scene_tools.bind({"selection": {"objects": [{"name": "Product"}], "faces": []}}, {})
scene_tools.studio_style("dark")
d_small = (bpy.data.objects["C2B Style Key"].location - product.matrix_world.translation).length
e_small = bpy.data.objects["C2B Style Key"].data.energy
product.scale = (10, 10, 10)
bpy.context.view_layer.update()
scene_tools.studio_style("dark")
d_big = (bpy.data.objects["C2B Style Key"].location - product.matrix_world.translation).length
e_big = bpy.data.objects["C2B Style Key"].data.energy
check(abs(d_big / d_small - 10) < 0.6 and abs(e_big / e_small - 100) < 5, "lights scale with the product")

print(f"All {ok} style checks passed on Blender {bpy.app.version_string}")
