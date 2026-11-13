"""Draws every panel against a checking stand-in for UILayout.

The bpy module has no windows, so real panels can't draw headless. This
stand-in checks each layout call's keyword arguments and property names
against Blender's own RNA definitions, which catches the usual draw errors.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402
import bmesh  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import ui, operators  # noqa: E402

FUNCS = bpy.types.UILayout.bl_rna.functions
calls = []


class Layout:
    def __init__(self):
        self.scale_x = self.scale_y = 1.0
        self.alignment = "EXPAND"
        self.enabled = self.active = True

    def __getattr__(self, name):
        if name not in FUNCS:
            raise AttributeError(f"UILayout has no {name}()")
        params = {p.identifier for p in FUNCS[name].parameters}

        def call(*args, **kwargs):
            bad = set(kwargs) - params
            if bad:
                raise TypeError(f"{name}() got unexpected {bad}")
            if name in ("prop",):
                data, prop = args[0], args[1]
                if prop not in data.bl_rna.properties:
                    raise AttributeError(f"{data} has no property {prop}")
            if name == "operator":
                op = args[0]
                mod, fn = op.split(".")
                getattr(getattr(bpy.ops, mod), fn).get_rna_type()
            if name == "template_list":
                if args[0] not in {"C2B_UL_references", "C2B_UL_history"}:
                    raise ValueError("unknown UIList")
            calls.append(name)
            return Layout()

        return call


class FakePanel:
    def __init__(self):
        self.layout = Layout()


def draw_all(label):
    for cls in (ui.C2B_PT_main, ui.C2B_PT_selection, ui.C2B_PT_references, ui.C2B_PT_apply, ui.C2B_PT_history, ui.C2B_PT_help):
        p = FakePanel()
        if hasattr(cls, "draw_header"):
            cls.draw_header(p, bpy.context)
        cls.draw(p, bpy.context)
    print("  ok: panels draw:", label)


state = bpy.context.window_manager.claude2blender
draw_all("empty scene")

bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
draw_all("one object selected")
for i in range(8):
    bpy.ops.mesh.primitive_uv_sphere_add(location=(i, 2, 0))
for o in bpy.data.objects:
    o.select_set(True)
draw_all("many objects selected")

bpy.context.view_layer.objects.active = cube
bpy.ops.object.mode_set(mode="EDIT")
draw_all("edit mode")
bpy.ops.object.mode_set(mode="OBJECT")

state.prompt = "test"
state.request_summary = "#1"
state.status = "status"
r = state.references.add()
r.filepath = "/nonexistent.png"
draw_all("with references")
state.gen_status = "FAILED"; state.gen_detail = "Claude Code isn't signed in. " * 5; state.gen_message = "A question?"
state.gen_session_id = "abc"; state.gen_turns = 2
draw_all("generate status + conversation")
state.gen_status = "COMPLETED"; state.gen_outcome = "QUESTION"
draw_all("completed: question")
state.gen_outcome = "STAGED"; state.gen_title = "Blue on Cube"; state.gen_targets = "Cube"; state.gen_show_details = True
draw_all("completed: staged, details open")
state.mat_name = "Anodised Graphite"; state.mat_has_swatch = True; state.mat_line = "Roughness 0.42 · Metallic 1.00"
state.mat_surface = "fine bead-blast grain"; state.mat_match = "needs_texture_maps"; state.mat_limits = "Needs a scan. " * 6
state.target_notes = "Bracket shares its mesh with Bracket.Twin; the same faces change there too"
draw_all("completed: material summary + limits")
state.gen_outcome = "APPLIED"; state.gen_show_details = False
draw_all("completed: applied")
for mode in ("ENVIRONMENT", "COMPOSITION"):
    state.mode = mode
    state.show_permissions = False
    draw_all(f"{mode.lower()}: permissions collapsed")
    state.show_permissions = True
    draw_all(f"{mode.lower()}: permissions open")
    state.perm_comp.full_assembly = True
    state.perm_env.positions = True
    draw_all(f"{mode.lower()}: full assembly")
state.mat_name = ""
state.scene_lines = "LIGHT_AREA|Large soft key upper left, 3:1 fill\nWORLD|Dark grey gradient\nCAMERA_DATA|" + "x" * 120
state.scene_match = "needs_hdri_or_textures"; state.scene_limits = "Needs the original HDRI. " * 4
state.gen_outcome = "STAGED"
draw_all("completed: scene summary")
state.status = "Blocked by Allowed Changes. Camera is off: changed camera settings of Camera"
state.last_apply_ok = False
draw_all("blocked status")
ui.C2B_UL_references.draw_item(None, bpy.context, Layout(), state, r, 0, state, "active_reference", 0)

bpy.app.driver_namespace["claude2blender"].stage_proposal("x = 1\ny = 2", "demo")
draw_all("proposal pending")
bpy.ops.claude2blender.preview_proposal()
draw_all("previewing")
bpy.ops.claude2blender.end_preview()
state.mode = "ENVIRONMENT"
from claude2blender import bridge  # noqa: E402
bridge.send_request(bpy.context, copy_to_clipboard=False)
bpy.app.driver_namespace["claude2blender"].stage_proposal(
    "c2b.light('C2B Key', location=(1, -1, 2), target=(0, 0, 0))\nc2b.studio_world(top='#333333')", "env")
bpy.ops.claude2blender.preview_proposal()
draw_all("previewing environment, with changes")
bpy.ops.claude2blender.compare_preview(show_original=True)
draw_all("previewing: showing original")
bpy.ops.claude2blender.end_preview()
p = FakePanel()
p.MAX_LINES = operators.C2B_OT_review_proposal.MAX_LINES
operators.C2B_OT_review_proposal.draw(p, bpy.context)
bpy.ops.claude2blender.apply_proposal()
draw_all("after apply, with history and changes")
bpy.ops.claude2blender.history_undo()
draw_all("after history undo")
state.active_history = 0
bpy.ops.claude2blender.apply_proposal() if state.proposal_pending else None
ui.C2B_UL_history.draw_item(None, bpy.context, Layout(), state, state.history[0], 0, state, "active_history", 0)

print(f"\nPanels OK ({len(calls)} layout calls checked)")
addon.unregister()
