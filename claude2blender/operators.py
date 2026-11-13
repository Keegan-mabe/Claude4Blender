import os
import traceback

import bpy
from bpy.props import CollectionProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ImportHelper

from . import bridge, claude_cli, guard, history, materials, scene_tools
from .properties import REFERENCE_ROLES, get_state, permissions_for
from .references import IMAGE_EXTENSIONS, is_image

IMAGE_GLOB = ";".join("*" + ext for ext in IMAGE_EXTENSIONS)
MODE_TO_ROLE = {"MATERIAL": "MATERIAL", "ENVIRONMENT": "ENVIRONMENT", "COMPOSITION": "COMPOSITION"}


# ---------------------------------------------------------------- prompt & request

class C2B_OT_edit_prompt(Operator):
    """Edit the prompt in a wider window"""
    bl_idname = "claude2blender.edit_prompt"
    bl_label = "Edit Prompt"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=640)

    def draw(self, context):
        self.layout.label(text="Describe what you want Claude to do:")
        self.layout.prop(get_state(context), "prompt", text="")

    def execute(self, context):
        return {"FINISHED"}


class C2B_OT_clear_prompt(Operator):
    """Clear the prompt"""
    bl_idname = "claude2blender.clear_prompt"
    bl_label = "Clear Prompt"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        get_state(context).prompt = ""
        return {"FINISHED"}


class C2B_OT_send(Operator):
    """Fallback: copy a message to paste into Claude Desktop, which reads the request through the Blender MCP"""
    bl_idname = "claude2blender.send"
    bl_label = "Copy for Claude Desktop"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        state = get_state(context)
        request, _text = bridge.send_request(context)
        if not request["prompt"]:
            self.report({"WARNING"}, "Request copied, but the prompt is empty")
        else:
            self.report({"INFO"}, "Request copied. Paste it into Claude Desktop (Cmd/Ctrl+V)")
        state.status = "Request copied to clipboard. Paste it into Claude Desktop."
        return {"FINISHED"}


class C2B_OT_generate(Operator):
    """Ask Claude (through your Claude Code sign-in) for a change. It arrives in Review & Apply; nothing runs until you press Apply"""
    bl_idname = "claude2blender.generate"
    bl_label = "Generate with Claude"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return not claude_cli.busy()

    def execute(self, context):
        try:
            claude_cli.start_generate(context)
        except RuntimeError as exc:
            state = get_state(context)
            state.gen_status = "FAILED"
            state.gen_detail = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class C2B_OT_cancel_generate(Operator):
    """Stop the running request"""
    bl_idname = "claude2blender.cancel_generate"
    bl_label = "Cancel"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        claude_cli.cancel()
        return {"FINISHED"}


class C2B_OT_new_conversation(Operator):
    """Start a new conversation: the next Generate doesn't continue the previous one"""
    bl_idname = "claude2blender.new_conversation"
    bl_label = "New Conversation"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        state = get_state(context)
        state.gen_session_id = ""
        state.gen_turns = 0
        state.gen_message = ""
        state.gen_status = "IDLE"
        state.gen_detail = ""
        return {"FINISHED"}


class C2B_OT_check_cli(Operator):
    """Check that Claude Code is installed and signed in"""
    bl_idname = "claude2blender.check_cli"
    bl_label = "Check Claude Code"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return not claude_cli.busy()

    def execute(self, context):
        claude_cli.start_check(context)
        return {"FINISHED"}


class C2B_OT_copy_request_json(Operator):
    """Copy the full request as JSON (for debugging or another tool)"""
    bl_idname = "claude2blender.copy_request_json"
    bl_label = "Copy Request JSON"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        import json
        context.window_manager.clipboard = json.dumps(bridge.build_request(context), indent=1)
        self.report({"INFO"}, "Request JSON copied")
        return {"FINISHED"}


# ---------------------------------------------------------------- references

def _add_reference(state, path, role):
    path = bpy.path.abspath(path)
    for ref in state.references:
        if bpy.path.abspath(ref.filepath) == path:
            ref.role = role
            return False
    ref = state.references.add()
    ref.filepath = path
    ref.role = role
    state.active_reference = len(state.references) - 1
    return True


class C2B_OT_add_reference(Operator, ImportHelper):
    """Add reference images to guide materials, lighting or composition"""
    bl_idname = "claude2blender.add_reference"
    bl_label = "Add Reference Images"
    bl_options = {"INTERNAL"}

    filter_glob: StringProperty(default=IMAGE_GLOB, options={"HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})
    role: EnumProperty(name="Use as", items=REFERENCE_ROLES)

    def invoke(self, context, event):
        self.role = MODE_TO_ROLE[get_state(context).mode]
        if self.directory and self.files:  # drag and drop
            return self.execute(context)
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        state = get_state(context)
        paths = [os.path.join(self.directory, f.name) for f in self.files if f.name]
        if not paths and self.filepath:
            paths = [self.filepath]
        added = sum(_add_reference(state, p, self.role) for p in paths if is_image(p))
        skipped = len(paths) - added
        if added:
            self.report({"INFO"}, f"Added {added} reference image(s)")
        elif skipped:
            self.report({"WARNING"}, "No new image files to add")
        return {"FINISHED"}


class C2B_OT_remove_reference(Operator):
    """Remove this reference (the image file is not touched)"""
    bl_idname = "claude2blender.remove_reference"
    bl_label = "Remove Reference"
    bl_options = {"INTERNAL"}

    index: IntProperty(default=-1)

    def execute(self, context):
        state = get_state(context)
        index = self.index if self.index >= 0 else state.active_reference
        if 0 <= index < len(state.references):
            state.references.remove(index)
            state.active_reference = max(0, min(state.active_reference, len(state.references) - 1))
        return {"FINISHED"}


class C2B_OT_clear_references(Operator):
    """Remove all references"""
    bl_idname = "claude2blender.clear_references"
    bl_label = "Clear References"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        get_state(context).references.clear()
        return {"FINISHED"}


class C2B_OT_open_reference(Operator):
    """Open the reference image in your system image viewer"""
    bl_idname = "claude2blender.open_reference"
    bl_label = "Open Reference"
    bl_options = {"INTERNAL"}

    index: IntProperty(default=-1)

    def execute(self, context):
        state = get_state(context)
        index = self.index if self.index >= 0 else state.active_reference
        if 0 <= index < len(state.references):
            bpy.ops.wm.path_open(filepath=bpy.path.abspath(state.references[index].filepath))
        return {"FINISHED"}


# ---------------------------------------------------------------- geometry guard

def _mesh_signature(me):
    # Vertices, topology and UV layers.
    return (len(me.vertices), len(me.edges), len(me.polygons), history.geometry_signature(me))


def geometry_snapshot():
    meshes = {}
    snap = {}
    for obj in bpy.data.objects:
        entry = {
            # Claude2Blender's own backdrops are resized through their scale; your objects never are.
            "scale": None if scene_tools.is_generated(obj) else tuple(round(s, 6) for s in obj.scale),
            "modifiers": tuple((m.name, m.type, m.show_viewport, m.show_render) for m in obj.modifiers),
        }
        if obj.type == "MESH" and obj.data is not None:
            key = obj.data.name_full
            if key not in meshes:
                meshes[key] = _mesh_signature(obj.data)
            entry["mesh"] = meshes[key]
        snap[obj.name_full] = entry
    return snap


def geometry_violations(before, after):
    problems = []
    for name, old in before.items():
        new = after.get(name)
        if new is None:
            problems.append(f"{name}: removed or renamed")
            continue
        if old.get("mesh") != new.get("mesh"):
            problems.append(f"{name}: mesh geometry or UVs changed")
        if old["scale"] != new["scale"]:
            problems.append(f"{name}: scale changed")
        if old["modifiers"] != new["modifiers"]:
            problems.append(f"{name}: modifiers changed")
    return problems


def _schedule_rollback(reason):
    """Undo the Apply step once the operator has finished and pushed it."""

    def rollback():
        state = get_state()
        wm = bpy.context.window_manager
        done = False
        for window in wm.windows:
            try:
                with bpy.context.temp_override(window=window, screen=window.screen):
                    bpy.ops.ed.undo()
                done = True
                break
            except Exception:
                continue
        state = get_state()
        state.last_apply_ok = False
        state.status = f"Rolled back: {reason}" if done else f"{reason}. Press Ctrl+Z to undo."
        bridge._redraw()
        return None

    bpy.app.timers.register(rollback, first_interval=0.05)


# ---------------------------------------------------------------- review & apply

class C2B_OT_review_proposal(Operator):
    """Review the change Claude staged before applying it"""
    bl_idname = "claude2blender.review_proposal"
    bl_label = "Review Claude's Change"
    bl_options = {"INTERNAL"}

    MAX_LINES = 80

    @classmethod
    def poll(cls, context):
        return get_state(context).proposal_pending

    def invoke(self, context, event):
        try:
            return context.window_manager.invoke_props_dialog(self, width=760, confirm_text="Close")
        except TypeError:  # Blender < 4.1
            return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        state = get_state(context)
        layout = self.layout
        layout.label(text=state.proposal_summary, icon="INFO")
        box = layout.box()
        col = box.column(align=True)
        col.scale_y = 0.75
        lines = state.proposal_code.splitlines()
        for i, line in enumerate(lines[: self.MAX_LINES], 1):
            col.label(text=f"{i:>3}  {line.replace(chr(9), '    ')}")
        if len(lines) > self.MAX_LINES:
            col.label(text=f"... {len(lines) - self.MAX_LINES} more lines (use Copy Code to see all)")
        row = layout.row()
        row.operator("claude2blender.copy_proposal", icon="COPYDOWN")

    def execute(self, context):
        return {"FINISHED"}


class C2B_OT_copy_proposal(Operator):
    """Copy the staged code to the clipboard"""
    bl_idname = "claude2blender.copy_proposal"
    bl_label = "Copy Code"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        context.window_manager.clipboard = get_state(context).proposal_code
        self.report({"INFO"}, "Code copied")
        return {"FINISHED"}


class C2B_OT_discard_proposal(Operator):
    """Discard the staged change without running it"""
    bl_idname = "claude2blender.discard_proposal"
    bl_label = "Discard"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        state = get_state(context)
        state.proposal_pending = False
        state.proposal_code = ""
        state.proposal_summary = ""
        state.status = "Change discarded."
        if history.preview_entry() is not None:
            _end_preview(self, context, keep=False)
        if state.gen_outcome == "STAGED":
            state.gen_outcome = "DISCARDED"
        return {"FINISHED"}


def run_change(op, context, code, summary, request, preview=False):
    """Run Claude's code once, guarded, and record it in the history. Returns True on success.

    preview=True runs it as the live preview: recorded aside, reverted exactly
    on Discard / End Preview, moved into History by Apply.
    """
    state = get_state(context)

    # Make sure the state before Apply is its own undo step, so Ctrl+Z on the
    # Apply never also undoes earlier changes made outside the panel.
    try:
        bpy.ops.ed.undo_push(message="Before Claude2Blender Apply")
    except Exception:
        pass

    previous_mode = context.mode
    if context.view_layer.objects.active and previous_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    before = geometry_snapshot() if state.preserve_geometry else None
    pending = history.Pending(state.preserve_geometry)
    perms = None
    snap = None
    if guard.applies(request):
        perms = permissions_for(state, request["mode"]) or {}
        snap = guard.Snapshot(request, perms)  # after Pending, so its hidden backups count as existing
    scene_tools.bind(request, perms or {})
    notes = materials.TargetNotes()
    namespace = {
        "bpy": bpy,
        "request": request,
        "expand_indices": bridge.expand_indices,
        "assign_material": lambda mat: materials.assign_material(mat, request, notes),
        "c2b": scene_tools.helpers,
    }
    try:
        exec(compile(code, "<claude2blender proposal>", "exec"), namespace)
    except Exception as exc:
        traceback.print_exc()
        pending.discard()
        state.last_apply_ok = False
        state.status = f"Apply failed: {type(exc).__name__}: {exc}"
        op.report({"ERROR"}, state.status)
        _schedule_rollback(f"the change raised {type(exc).__name__}")
        return False

    if before is not None:
        problems = geometry_violations(before, geometry_snapshot())
        if problems:
            pending.discard()
            detail = "; ".join(problems[:3]) + (" ..." if len(problems) > 3 else "")
            state.last_apply_ok = False
            state.status = f"Preserve Geometry blocked this change ({detail})"
            op.report({"WARNING"}, state.status)
            _schedule_rollback(f"Preserve Geometry ({detail})")
            return False

    if snap is not None:
        problems, untracked = snap.check()
        if problems:
            detail = "; ".join(problems[:3]) + (" ..." if len(problems) > 3 else "")
            state.last_apply_ok = False
            state.status = f"Blocked by Allowed Changes. {detail}"
            op.report({"WARNING"}, state.status)
            entry = history.build_entry(pending, code, summary, request)
            if not untracked and entry["patch_ok"]:
                try:
                    history.discard_change(entry)  # exact, immediate: the scene is as before
                    state.status += ". Nothing was changed."
                    return False
                except history.HistoryError:
                    pass
            _schedule_rollback("Allowed Changes")
            return False

    entry = history.commit(pending, code, summary, request, preview=preview)
    entry["notes"] = list(notes)
    state.target_notes = "; ".join(notes)

    if previous_mode == "EDIT_MESH" and context.view_layer.objects.active:
        try:
            bpy.ops.object.mode_set(mode="EDIT")
        except Exception:
            pass

    if preview:
        _set_material_view(context, True, request.get("mode") if request else None)
        state.status = f"Previewing: {summary}"
        op.report({"INFO"}, f"Previewing: {summary}. Apply keeps it, Discard reverts it")
        return True

    state.active_history = 0
    state.last_apply_ok = True
    state.last_apply_summary = summary
    state.status = f"Applied: {summary}"
    if entry["warnings"]:
        state.status += " (" + "; ".join(entry["warnings"]) + ": use Ctrl+Z to revert)"
    op.report({"INFO"}, f"Applied: {summary}")
    return True


class C2B_OT_apply_proposal(Operator):
    """Run Claude's staged change. It goes into the change history, where you can undo and redo it"""
    bl_idname = "claude2blender.apply_proposal"
    bl_label = "Apply Claude Change"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return get_state(context).proposal_pending

    def execute(self, context):
        state = get_state(context)
        if history.preview_entry() is not None:
            # Approving the live preview: keep exactly what is on screen, don't run the code again.
            entry = _end_preview(self, context, keep=True)
            ok = entry is not None
            if ok:
                state.last_apply_ok = True
                state.last_apply_summary = entry["summary"]
                state.status = f"Applied: {entry['summary']}"
                state.active_history = 0
                self.report({"INFO"}, f"Applied: {entry['summary']}")
        else:
            ok = run_change(self, context, state.proposal_code, state.proposal_summary,
                            bridge.get_request_for_apply(context))
        if ok:
            state.proposal_pending = False
            if state.gen_outcome == "STAGED" and state.proposal_request_id == state.gen_request_id:
                state.gen_outcome = "APPLIED"
                state.gen_history_uid = history.entries()[-1]["id"]
                state.gen_show_details = False
        return {"FINISHED"}


# ---------------------------------------------------------------- live preview

_shading_restore = {}


def _set_material_view(context, on, mode=None):
    """While previewing, show what the change does; put every viewport back afterwards.

    Material: Solid viewports switch to Material Preview.
    Environment: Material Preview with the scene's own lights and world (EEVEE in the
    viewport, whatever the render engine), so the new lighting is visible.
    Composition: also looks through the scene camera if the change sets one.
    """
    wm = context.window_manager
    scene = context.scene
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                key = space.as_pointer()
                shading = space.shading
                if on:
                    saved = {"type": shading.type}
                    if shading.type == "SOLID":
                        shading.type = "MATERIAL"
                    if mode == "environment" and shading.type == "MATERIAL":
                        saved["use_scene_lights"] = shading.use_scene_lights
                        saved["use_scene_world"] = shading.use_scene_world
                        shading.use_scene_lights = True
                        shading.use_scene_world = True
                    r3d = getattr(space, "region_3d", None)
                    if mode == "composition" and scene.camera is not None and r3d is not None:
                        saved["view_perspective"] = r3d.view_perspective
                        r3d.view_perspective = "CAMERA"
                    _shading_restore[key] = saved
                elif key in _shading_restore:
                    saved = _shading_restore.pop(key)
                    for attr in ("use_scene_lights", "use_scene_world"):
                        if attr in saved:
                            setattr(shading, attr, saved[attr])
                    shading.type = saved["type"]
                    if "view_perspective" in saved and getattr(space, "region_3d", None) is not None:
                        space.region_3d.view_perspective = saved["view_perspective"]
            area.tag_redraw()


def _end_preview(op, context, keep):
    state = get_state(context)
    try:
        entry = history.end_preview(keep)
    except history.HistoryError as exc:
        state.status = f"Preview could not be reverted: {exc}. Press Ctrl+Z."
        op.report({"ERROR"}, state.status)
        entry = None
    _set_material_view(context, False)
    if not keep and entry is not None:
        state.status = "Preview ended. The scene is back as it was."
    return entry


class C2B_OT_preview_proposal(Operator):
    """Show the staged change in the viewport without committing it. Apply keeps it, Discard or End Preview reverts it exactly"""
    bl_idname = "claude2blender.preview_proposal"
    bl_label = "Preview"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return get_state(context).proposal_pending and history.preview_entry() is None

    def execute(self, context):
        state = get_state(context)
        run_change(self, context, state.proposal_code, state.proposal_summary,
                   bridge.get_request_for_apply(context), preview=True)
        return {"FINISHED"}


class C2B_OT_end_preview(Operator):
    """Revert the preview. The change stays staged so you can Apply, refine or discard it"""
    bl_idname = "claude2blender.end_preview"
    bl_label = "End Preview"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return history.preview_entry() is not None

    def execute(self, context):
        _end_preview(self, context, keep=False)
        return {"FINISHED"}


class C2B_OT_compare_preview(Operator):
    """Flip the viewport between the Original scene and Claude's Proposed change, exactly"""
    bl_idname = "claude2blender.compare_preview"
    bl_label = "Original / Proposed"
    bl_options = {"REGISTER", "UNDO"}

    show_original: bpy.props.BoolProperty(default=True, options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return history.preview_entry() is not None

    def execute(self, context):
        state = get_state(context)
        try:
            history.compare_preview(self.show_original)
        except history.HistoryError as exc:
            self.report({"WARNING"}, str(exc))
            return {"CANCELLED"}
        state.status = "Showing: Original" if self.show_original else "Showing: Proposed"
        return {"FINISHED"}


# ---------------------------------------------------------------- history

def _history_op(op, context, action, done_text):
    state = get_state(context)
    if history.preview_entry() is not None:
        _end_preview(op, context, keep=False)
    if context.view_layer.objects.active and context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    try:
        entry = action()
    except history.HistoryError as exc:
        state.status = str(exc)
        op.report({"WARNING"}, str(exc))
        history.sync_ui()
        return {"CANCELLED"}
    history.sync_ui()
    state.status = done_text(entry)
    op.report({"INFO"}, state.status)
    return {"FINISHED"}


class C2B_OT_history_undo(Operator):
    """Revert the newest applied change. Only what that change touched is restored; the code is kept for Redo"""
    bl_idname = "claude2blender.history_undo"
    bl_label = "Undo Change"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return history.can_undo()

    def execute(self, context):
        return _history_op(self, context, history.undo_last, lambda e: f"Undone: {e['summary']}")


class C2B_OT_history_redo(Operator):
    """Re-apply the next undone change exactly as before, without asking Claude again"""
    bl_idname = "claude2blender.history_redo"
    bl_label = "Redo Change"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return history.can_redo()

    def execute(self, context):
        return _history_op(self, context, history.redo_next, lambda e: f"Redone: {e['summary']}")


class C2B_OT_history_goto(Operator):
    """Go to this version: undo newer changes or redo up to this one"""
    bl_idname = "claude2blender.history_goto"
    bl_label = "Go to Version"
    bl_options = {"REGISTER", "UNDO"}

    uid: IntProperty(default=-1, options={"SKIP_SAVE"})

    def execute(self, context):
        uid = self.uid if self.uid >= 0 else _selected_uid(context)
        if uid is None:
            return {"CANCELLED"}

        def action():
            history.go_to(uid)
            return history.find(uid) if uid else {"summary": "original (before any change)"}

        return _history_op(self, context, action, lambda e: f"Now at: {e['summary']}")


class C2B_OT_history_apply_again(Operator):
    """Run this change's code again as a new change, without asking Claude"""
    bl_idname = "claude2blender.history_apply_again"
    bl_label = "Apply Again"
    bl_options = {"REGISTER", "UNDO"}

    uid: IntProperty(default=-1, options={"SKIP_SAVE"})

    def execute(self, context):
        uid = self.uid if self.uid >= 0 else _selected_uid(context)
        entry = history.find(uid) if uid else None
        if entry is None:
            return {"CANCELLED"}
        if history.preview_entry() is not None:
            _end_preview(self, context, keep=False)
        run_change(self, context, entry["code"], entry["summary"], entry["request"])
        return {"FINISHED"}


class C2B_OT_history_review(Operator):
    """Load this change's code into Review & Apply"""
    bl_idname = "claude2blender.history_review"
    bl_label = "Load into Review"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        entry = history.find(_selected_uid(context) or 0)
        if entry is None:
            return {"CANCELLED"}
        state = get_state(context)
        state.proposal_code = entry["code"]
        state.proposal_summary = entry["summary"]
        state.proposal_pending = True
        state.status = f"Loaded '{entry['summary']}' for review"
        return {"FINISHED"}


class C2B_OT_history_clear(Operator):
    """Forget the change history. The scene stays exactly as it is now"""
    bl_idname = "claude2blender.history_clear"
    bl_label = "Clear History"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        history.clear()
        get_state(context).status = "History cleared"
        return {"FINISHED"}


def _selected_uid(context):
    state = get_state(context)
    if 0 <= state.active_history < len(state.history):
        return state.history[state.active_history].uid
    return None


# ---------------------------------------------------------------- MCP

class C2B_OT_start_mcp(Operator):
    """Start the existing Blender MCP add-on's server so Claude Desktop can reach Blender"""
    bl_idname = "claude2blender.start_mcp"
    bl_label = "Connect to Claude"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        try:
            bpy.ops.blendermcp.start_server()
        except Exception as exc:
            self.report({"ERROR"}, f"Could not start the Blender MCP server: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


# ---------------------------------------------------------------- drag & drop (Blender 4.1+)

_file_handler = None
if hasattr(bpy.types, "FileHandler"):

    class C2B_FH_reference_drop(bpy.types.FileHandler):
        bl_idname = "C2B_FH_reference_drop"
        bl_label = "Claude2Blender reference"
        bl_import_operator = "claude2blender.add_reference"
        bl_file_extensions = ";".join(IMAGE_EXTENSIONS)

        @classmethod
        def poll_drop(cls, context):
            # Only when dropped on the sidebar, so normal image drops on the viewport are unchanged.
            area, region = context.area, context.region
            return bool(area and area.type == "VIEW_3D" and region and region.type == "UI")

    _file_handler = C2B_FH_reference_drop


_classes = (
    C2B_OT_edit_prompt,
    C2B_OT_clear_prompt,
    C2B_OT_send,
    C2B_OT_generate,
    C2B_OT_cancel_generate,
    C2B_OT_new_conversation,
    C2B_OT_check_cli,
    C2B_OT_copy_request_json,
    C2B_OT_add_reference,
    C2B_OT_remove_reference,
    C2B_OT_clear_references,
    C2B_OT_open_reference,
    C2B_OT_review_proposal,
    C2B_OT_copy_proposal,
    C2B_OT_discard_proposal,
    C2B_OT_apply_proposal,
    C2B_OT_preview_proposal,
    C2B_OT_end_preview,
    C2B_OT_compare_preview,
    C2B_OT_history_undo,
    C2B_OT_history_redo,
    C2B_OT_history_goto,
    C2B_OT_history_apply_again,
    C2B_OT_history_review,
    C2B_OT_history_clear,
    C2B_OT_start_mcp,
) + ((_file_handler,) if _file_handler else ())


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
