import os
import time

import bpy
from bpy.types import Panel, UIList

from . import bridge, claude_cli, history
from .properties import PERMISSION_ITEMS, ROLE_ICONS, get_state, permissions_for
from .references import icon_for

CATEGORY = "Claude"

TYPE_ICONS = {
    "MESH": "OUTLINER_OB_MESH",
    "CURVE": "OUTLINER_OB_CURVE",
    "LIGHT": "OUTLINER_OB_LIGHT",
    "CAMERA": "OUTLINER_OB_CAMERA",
    "EMPTY": "OUTLINER_OB_EMPTY",
}
MAX_LISTED_OBJECTS = 6


def _length(scene, value):
    units = scene.unit_settings
    try:
        return bpy.utils.units.to_string(units.system, "LENGTH", value * units.scale_length, precision=3)
    except Exception:
        return f"{value:.3g}"


def _dims(scene, obj):
    return " × ".join(_length(scene, d) for d in obj.dimensions)


class _Base:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = CATEGORY


class C2B_PT_main(_Base, Panel):
    bl_idname = "C2B_PT_main"
    bl_label = "Claude2Blender"

    def draw_header(self, context):
        self.layout.label(icon="LIGHT_SUN")

    def draw(self, context):
        layout = self.layout
        state = get_state(context)

        # Optional Claude Desktop fallback (through the Blender MCP add-on). Generate with
        # Claude doesn't need it, so nothing is shown when that add-on isn't installed.
        status, port = bridge.mcp_status()
        if status in ("running", "stopped"):
            row = layout.row(align=True)
            if status == "running":
                row.label(text=f"Blender MCP connected · port {port}", icon="LINKED")
            else:
                row.label(text="Blender MCP not running", icon="UNLINKED")
                row.operator("claude2blender.start_mcp", text="Connect", icon="PLAY")
            layout.separator(factor=0.5)

        col = layout.column(align=True)
        col.label(text="Mode")
        col.row(align=True).prop(state, "mode", expand=True)
        if state.mode == "ENVIRONMENT":
            row = col.row(align=True)
            row.prop(state, "studio_style", text="", icon="LIGHT_AREA")
            row.operator("claude2blender.stage_style", text="Use Style", icon="IMPORT")
            from . import operators as _ops
            row = col.row(align=True)
            if _ops.match_running():
                row.label(text=state.status or "Measuring…", icon="TIME")
                row.operator("claude2blender.cancel_measure", text="", icon="CANCEL")
            else:
                row.operator("claude2blender.measure_lighting", text="Measure Lighting from Reference",
                             icon="LIGHT_AREA")
        if state.mode != "MATERIAL":
            _draw_permissions(col, context, state)
        else:
            row = col.row(align=True)
            row.prop(state, "preset", text="", icon="MATERIAL")
            row.operator("claude2blender.stage_preset", text="Use Preset", icon="IMPORT")

        layout.separator(factor=0.5)

        col = layout.column(align=True)
        head = col.row(align=True)
        head.label(text="Prompt")
        head.operator("claude2blender.edit_prompt", text="", icon="GREASEPENCIL", emboss=False)
        if state.prompt:
            head.operator("claude2blender.clear_prompt", text="", icon="X", emboss=False)
        if _supports_placeholder():
            col.prop(state, "prompt", text="", placeholder="Describe what Claude should do")
        else:
            col.prop(state, "prompt", text="")

        layout.separator(factor=0.5)
        layout.prop(
            state,
            "preserve_geometry",
            icon="LOCKED" if state.preserve_geometry else "UNLOCKED",
            toggle=True,
        )

        _draw_generate(layout, context, state)

        row = layout.row()
        row.operator("claude2blender.send", icon="COPYDOWN")

        if state.request_summary:
            sub = layout.column()
            sub.scale_y = 0.8
            sub.label(text=f"Last sent {state.request_summary}", icon="CHECKMARK")
        if state.status and state.gen_status == "IDLE":
            sub = layout.column()
            sub.scale_y = 0.8
            sub.label(text=state.status, icon="INFO")


def _draw_permissions(layout, context, state):
    """Compact 'Allowed changes' row; expands into toggles."""
    group = state.perm_env if state.mode == "ENVIRONMENT" else state.perm_comp
    perms = permissions_for(state)
    on = [label for key, label, *_ in PERMISSION_ITEMS if perms[key]]
    box = layout.box()
    col = box.column(align=True)
    row = col.row(align=True)
    row.prop(state, "show_permissions", text="", emboss=False,
             icon="DISCLOSURE_TRI_DOWN" if state.show_permissions else "DISCLOSURE_TRI_RIGHT")
    sub = row.row(align=True)
    sub.scale_y = 0.9
    summary = ", ".join(on) if on else "nothing"
    sub.label(text=f"Allowed: {summary}" if len(summary) < 44 else f"Allowed: {len(on)} of {len(PERMISSION_ITEMS)}")
    if not state.show_permissions:
        return
    grid = col.grid_flow(row_major=True, columns=2, even_columns=True, align=True)
    for key, label, icon, _desc in PERMISSION_ITEMS:
        grid.prop(group, key, text=label, icon=icon, toggle=True)
    moves = perms["positions"] or perms["rotations"] or perms["spacing"]
    if state.mode == "COMPOSITION" or moves:
        col.separator(factor=0.4)
        col.prop(group, "full_assembly", text="Full assembly (any object may move)", icon="OUTLINER_COLLECTION")
    hint = col.column(align=True)
    hint.scale_y = 0.8
    hint.active = False
    if moves:
        if group.full_assembly:
            hint.label(text="Any object in the scene may move", icon="INFO")
        else:
            names = [o.name for o in context.selected_objects if o.type not in {"LIGHT", "CAMERA"}]
            if names:
                text = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
                hint.label(text=f"May move: {text}", icon="INFO")
            else:
                hint.label(text="Select the objects that may move", icon="INFO")
    else:
        hint.label(text="Objects stay where they are", icon="LOCKED")


GEN_ICONS = {"CONNECTING": "SORTTIME", "PROCESSING": "TIME", "COMPLETED": "CHECKMARK", "FAILED": "ERROR"}


def _draw_generate(layout, context, state):
    running = claude_cli.busy()
    row = layout.row(align=True)
    row.scale_y = 1.6
    if running:
        row.operator("claude2blender.cancel_generate", text="Cancel", icon="CANCEL")
    else:
        label = "Refine with Claude" if state.gen_follow_up and state.gen_session_id else "Generate with Claude"
        row.operator("claude2blender.generate", text=label, icon="LIGHT_SUN")

    if state.gen_session_id:
        row = layout.row(align=True)
        row.prop(state, "gen_follow_up", text=f"Continue conversation ({state.gen_turns})", toggle=True,
                 icon="LINKED" if state.gen_follow_up else "UNLINKED")
        row.operator("claude2blender.new_conversation", text="", icon="ADD")

    if state.gen_status != "IDLE":
        _draw_status_card(layout, context, state, running)


def _text_width(context):
    scale = context.preferences.system.ui_scale or 1.0
    return int((context.region.width if context.region else 300) / (7 * scale))


def _wrapped(col, context, text, max_lines):
    sub = col.column(align=True)
    sub.scale_y = 0.8
    lines = claude_cli.wrap(text, _text_width(context))
    for line in lines[:max_lines]:
        sub.label(text=line)
    if len(lines) > max_lines:
        sub.label(text="…")


def _card_outcome(state):
    """(label, icon) for the change this Generate produced."""
    if state.gen_outcome == "APPLIED":
        entry = history.find(state.gen_history_uid)
        if entry is not None and entry["state"] != "APPLIED":
            return "Undone", "LOOP_BACK"
        return "Applied", "CHECKMARK"
    if state.gen_outcome == "STAGED":
        if state.proposal_pending and history.preview_entry() is not None:
            return "Previewing · not saved until Apply", "HIDE_OFF"
        return ("Staged for review", "TIME") if state.proposal_pending else ("Not applied", "X")
    if state.gen_outcome == "DISCARDED":
        return "Discarded", "X"
    return None, None


def _draw_status_card(layout, context, state, running):
    box = layout.box()
    col = box.column(align=True)

    if running:
        row = col.row()
        row.label(text=state.gen_status.title(), icon=GEN_ICONS.get(state.gen_status, "TIME"))
        right = row.row()
        right.alignment = "RIGHT"
        right.label(text=f"{time.time() - state.gen_started:.0f}s")
        sub = col.row()
        sub.scale_y = 0.8
        sub.active = False
        sub.label(text=state.gen_detail)
        return

    if state.gen_status == "FAILED":
        col.alert = True
        col.label(text="Failed", icon="ERROR")
        col.alert = False
        _wrapped(col, context, state.gen_detail, 5)
        if not state.cli_ok:
            col.operator("claude2blender.check_cli", icon="FILE_REFRESH")
        return

    # Completed
    outcome, outcome_icon = _card_outcome(state)
    applied = outcome == "Applied"
    row = col.row()
    if state.gen_outcome == "QUESTION":
        row.label(text="Needs your input", icon="QUESTION")
    else:
        row.label(text="Applied" if applied else "Completed", icon="CHECKMARK")
    right = row.row()
    right.alignment = "RIGHT"
    right.active = False
    right.label(text=f"{state.gen_elapsed:.0f}s")

    if state.gen_outcome == "QUESTION":
        # A question needs an answer, so it stays visible: reply in the prompt and press Refine.
        _wrapped(col, context, state.gen_message, 8)
        return

    if state.gen_title:
        col.label(text=state.gen_title)
    _draw_material_summary(col, context, state)
    _draw_scene_summary(col, context, state)
    sub = col.row()
    sub.scale_y = 0.85
    sub.active = False
    sub.label(text=state.gen_targets or "Scene", icon="OBJECT_DATA")
    if outcome:
        sub = col.row()
        sub.scale_y = 0.85
        sub.label(text=outcome, icon=outcome_icon)

    if state.gen_outcome == "STAGED" and state.proposal_pending:
        col.separator(factor=0.5)
        _draw_review_buttons(col)

    if state.gen_message:
        row = col.row()
        row.prop(state, "gen_show_details", text="Details", emboss=False,
                 icon="DISCLOSURE_TRI_DOWN" if state.gen_show_details else "DISCLOSURE_TRI_RIGHT")
        if state.gen_show_details:
            _wrapped(col, context, state.gen_message, 12)


def _draw_material_summary(col, context, state):
    if not state.mat_name:
        return
    row = col.row(align=True)
    if state.mat_has_swatch:
        sw = row.row()
        sw.ui_units_x = 1.6
        sw.prop(state, "mat_swatch", text="")
    row.label(text=state.mat_name, icon="MATERIAL" if not state.mat_has_swatch else "NONE")
    sub = col.column(align=True)
    sub.scale_y = 0.8
    sub.active = False
    if state.mat_line:
        sub.label(text=state.mat_line)
    if state.mat_surface:
        sub.label(text=state.mat_surface)
    if state.mat_limits and state.mat_match in ("approximate", "needs_texture_maps"):
        # Honest limits stay visible: they decide whether a texture scan is needed.
        lim = col.column(align=True)
        lim.scale_y = 0.8
        head = "Needs texture maps for an exact match" if state.mat_match == "needs_texture_maps" else "Approximate match"
        lim.label(text=head, icon="INFO")
        for line in claude_cli.wrap(state.mat_limits, _text_width(context))[:3]:
            lim.label(text=line)


MATCH_HEADS = {"approximate": "Approximate match", "needs_hdri_or_textures": "Needs an HDRI or textures to match exactly"}


def _draw_scene_summary(col, context, state):
    lines = claude_cli.scene_summary_lines(state)
    if not lines:
        return
    sub = col.column(align=True)
    sub.scale_y = 0.8
    width = _text_width(context) - 3
    for icon, text in lines[:6]:
        row = sub.row()
        row.active = False
        row.label(text=text if len(text) <= width else text[: max(10, width - 1)] + "…", icon=icon)
    if state.scene_limits and state.scene_match in MATCH_HEADS:
        lim = col.column(align=True)
        lim.scale_y = 0.8
        lim.label(text=MATCH_HEADS[state.scene_match], icon="INFO")
        for line in claude_cli.wrap(state.scene_limits, _text_width(context))[:3]:
            lim.label(text=line)


def _draw_changes(layout, context, state, entry, title):
    """What the previewed / applied change does, grouped: lights, camera, world, objects, geometry."""
    changes = entry.get("changes") or []
    if not changes:
        return
    box = layout.box()
    col = box.column(align=True)
    row = col.row()
    row.prop(state, "show_changes", text=title, emboss=False,
             icon="DISCLOSURE_TRI_DOWN" if state.show_changes else "DISCLOSURE_TRI_RIGHT")
    if not state.show_changes:
        return
    sub = col.column(align=True)
    sub.scale_y = 0.8
    width = _text_width(context) - 3
    for icon, text in changes[:10]:
        for i, line in enumerate(claude_cli.wrap(text, width)[:2]):
            sub.label(text=line, icon=icon if i == 0 else "BLANK1")


def _draw_review_buttons(layout):
    row = layout.row(align=True)
    row.operator("claude2blender.review_proposal", text="Review", icon="VIEWZOOM")
    if history.preview_entry() is None:
        row.operator("claude2blender.preview_proposal", text="Preview", icon="HIDE_OFF")
    else:
        row.operator("claude2blender.end_preview", text="End Preview", icon="HIDE_ON")
    row.operator("claude2blender.apply_proposal", text="Apply", icon="CHECKMARK")


def _supports_placeholder():
    return bpy.app.version >= (4, 1, 0)


class C2B_PT_selection(_Base, Panel):
    bl_idname = "C2B_PT_selection"
    bl_label = "Selection"
    bl_parent_id = "C2B_PT_main"

    def draw_header(self, context):
        self.layout.label(icon="RESTRICT_SELECT_OFF")

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        if context.mode == "EDIT_MESH":
            objects = getattr(context, "objects_in_mode", None) or [context.edit_object]
            any_faces = False
            for obj in objects:
                if obj is None or obj.type != "MESH":
                    continue
                me = obj.data
                n = me.total_face_sel
                any_faces |= n > 0
                row = layout.row()
                row.label(text=obj.name, icon="FACESEL")
                row.label(text=f"{n} / {len(me.polygons)} faces")
            if not any_faces:
                layout.label(text="Select faces to target a region", icon="INFO")
            else:
                layout.label(text="Face targets are sent with the request", icon="CHECKMARK")
            return

        selected = context.selected_objects
        active = context.view_layer.objects.active
        if not selected:
            layout.label(text="Nothing selected", icon="INFO")
            layout.label(text="Select objects, or Tab into Edit Mode for faces")
            return

        layout.label(text=f"{len(selected)} object{'s' if len(selected) != 1 else ''} selected")
        col = layout.column(align=True)
        ordered = sorted(selected, key=lambda o: (o != active, o.name))
        for obj in ordered[:MAX_LISTED_OBJECTS]:
            row = col.row(align=True)
            row.label(text=obj.name, icon=TYPE_ICONS.get(obj.type, "OBJECT_DATA"))
            sub = row.row()
            sub.alignment = "RIGHT"
            sub.label(text=_dims(scene, obj))
        if len(selected) > MAX_LISTED_OBJECTS:
            col.label(text=f"+ {len(selected) - MAX_LISTED_OBJECTS} more")

        if active and active.material_slots:
            layout.separator(factor=0.3)
            mats = [s.material.name for s in active.material_slots if s.material]
            if mats:
                layout.label(text="Materials: " + ", ".join(mats[:4]) + (" …" if len(mats) > 4 else ""),
                             icon="MATERIAL")


class C2B_UL_references(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        name = os.path.basename(item.filepath) or "(no file)"
        thumb = icon_for(item.filepath)
        row = layout.row(align=True)
        if thumb:
            row.label(text=name, icon_value=thumb)
        else:
            row.label(text=name, icon="ERROR" if item.filepath else "IMAGE_DATA")
        sub = row.row(align=True)
        sub.scale_x = 0.9
        sub.prop(item, "role", text="", icon=ROLE_ICONS.get(item.role, "IMAGE_DATA"))


class C2B_PT_references(_Base, Panel):
    bl_idname = "C2B_PT_references"
    bl_label = "References"
    bl_parent_id = "C2B_PT_main"

    def draw_header(self, context):
        self.layout.label(icon="IMAGE_REFERENCE")

    def draw(self, context):
        layout = self.layout
        state = get_state(context)

        if not state.references:
            box = layout.box()
            col = box.column(align=True)
            col.label(text="No reference images yet", icon="IMAGE_DATA")
            if hasattr(bpy.types, "FileHandler"):
                col.label(text="Add images, or drop them on this sidebar")
            row = layout.row()
            row.scale_y = 1.2
            row.operator("claude2blender.add_reference", text="Add Images", icon="ADD")
            return

        row = layout.row()
        row.template_list("C2B_UL_references", "", state, "references", state, "active_reference", rows=3)
        side = row.column(align=True)
        side.operator("claude2blender.add_reference", text="", icon="ADD")
        side.operator("claude2blender.remove_reference", text="", icon="REMOVE")
        side.separator()
        side.operator("claude2blender.clear_references", text="", icon="TRASH")

        if 0 <= state.active_reference < len(state.references):
            ref = state.references[state.active_reference]
            thumb = icon_for(ref.filepath)
            box = layout.box()
            if thumb:
                box.template_icon(icon_value=thumb, scale=7.0)
            else:
                box.label(text="File not found", icon="ERROR")
            box.prop(ref, "note", text="", icon="TEXT")
            box.operator("claude2blender.open_reference", text="Open Image", icon="FILE_IMAGE")


class C2B_PT_apply(_Base, Panel):
    bl_idname = "C2B_PT_apply"
    bl_label = "Review & Apply"
    bl_parent_id = "C2B_PT_main"

    def draw_header(self, context):
        self.layout.label(icon="CHECKBOX_HLT")

    def draw(self, context):
        layout = self.layout
        state = get_state(context)

        preview = history.preview_entry()
        if state.proposal_pending:
            box = layout.box()
            box.label(text="Claude staged a change", icon="INFO")
            col = box.column()
            col.scale_y = 0.85
            col.label(text=state.proposal_summary)
            n = len(state.proposal_code.splitlines())
            col.label(text=f"{n} line{'s' if n != 1 else ''} of Blender Python")
            row = layout.row(align=True)
            row.scale_y = 1.3
            if preview is not None:
                row.operator("claude2blender.end_preview", text="End Preview", icon="HIDE_ON")
            else:
                row.operator("claude2blender.preview_proposal", text="Preview", icon="HIDE_OFF")
            row.operator("claude2blender.apply_proposal", text="Apply", icon="CHECKMARK")
            if preview is not None:
                original = history.showing_original()
                row = layout.row(align=True)
                op = row.operator("claude2blender.compare_preview", text="Original", depress=original,
                                  icon="RECOVER_LAST")
                op.show_original = True
                op = row.operator("claude2blender.compare_preview", text="Proposed", depress=not original,
                                  icon="SHADING_RENDERED")
                op.show_original = False
            row = layout.row(align=True)
            row.operator("claude2blender.review_proposal", text="Review Code", icon="VIEWZOOM")
            row.operator("claude2blender.discard_proposal", text="Discard", icon="X")
            if preview is not None:
                _draw_changes(layout, context, state, preview, "Changes in this preview")
            hint = layout.column(align=True)
            hint.scale_y = 0.8
            hint.active = False
            if preview is not None:
                hint.label(text="Previewing: not saved into your file until Apply", icon="HIDE_OFF")
            if state.preserve_geometry and (preview is None or not preview.get("changes")):
                hint.label(text="Geometry, UVs and modifiers protected", icon="LOCKED")
        elif state.last_apply_ok:
            box = layout.box()
            box.label(text="Applied successfully", icon="CHECKMARK")
            sub = box.column(align=True)
            sub.scale_y = 0.85
            sub.label(text=state.last_apply_summary)
            hint = sub.row()
            hint.active = False
            hint.label(text="Undo and Redo below, versions in History")
            entries = history.entries()
            if entries and entries[-1]["summary"] == state.last_apply_summary:
                _draw_changes(layout, context, state, entries[-1], "What changed")
        else:
            col = layout.column()
            col.scale_y = 0.85
            col.label(text="No change staged yet", icon="TIME")
            col.label(text="Claude's changes wait here for your review.")
        if not state.last_apply_ok and state.status.startswith(
                ("Blocked", "Rolled back", "Preserve Geometry", "Apply failed")):
            warn = layout.column(align=True)
            warn.scale_y = 0.8
            warn.alert = True
            for i, line in enumerate(claude_cli.wrap(state.status, _text_width(context))[:4]):
                warn.label(text=line, icon="ERROR" if i == 0 else "BLANK1")
        if state.target_notes:
            warn = layout.column(align=True)
            warn.scale_y = 0.8
            for i, line in enumerate(claude_cli.wrap(state.target_notes, _text_width(context))[:3]):
                warn.label(text=line, icon="ERROR" if i == 0 else "BLANK1")

        if state.history:
            row = layout.row(align=True)
            row.scale_y = 1.2
            row.operator("claude2blender.history_undo", text="Undo", icon="LOOP_BACK")
            row.operator("claude2blender.history_redo", text="Redo", icon="LOOP_FORWARDS")


STATE_ICONS = {"APPLIED": "CHECKMARK", "UNDONE": "LOOP_BACK", "ALT": "FILE_SCRIPT"}


class C2B_UL_history(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        sub = row.row(align=True)
        sub.active = item.state == "APPLIED"
        sub.label(text=item.summary, icon=STATE_ICONS.get(item.state, "DOT"))
        right = row.row()
        right.alignment = "RIGHT"
        right.active = False
        right.label(text=item.time)


class C2B_PT_history(_Base, Panel):
    bl_idname = "C2B_PT_history"
    bl_label = "History"
    bl_parent_id = "C2B_PT_main"

    def draw_header(self, context):
        self.layout.label(icon="RECOVER_LAST")

    def draw(self, context):
        layout = self.layout
        state = get_state(context)
        if not state.history:
            col = layout.column()
            col.scale_y = 0.85
            col.label(text="Applied changes appear here", icon="INFO")
            col.label(text="Step back and forward between versions.")
            return

        layout.template_list("C2B_UL_history", "", state, "history", state, "active_history", rows=4)

        if 0 <= state.active_history < len(state.history):
            item = state.history[state.active_history]
            col = layout.column(align=True)
            row = col.row(align=True)
            if item.state != "ALT":
                op = row.operator("claude2blender.history_goto", text="Go to This Version", icon="RECOVER_LAST")
                op.uid = item.uid
            op = row.operator("claude2blender.history_apply_again", text="Apply Again", icon="PLAY")
            op.uid = item.uid
            row = col.row(align=True)
            row.operator("claude2blender.history_review", text="Load into Review", icon="VIEWZOOM")
            op = row.operator("claude2blender.history_goto", text="Original", icon="FILE_REFRESH")
            op.uid = 0

        sub = layout.row()
        sub.alignment = "RIGHT"
        sub.operator("claude2blender.history_clear", text="Clear History", icon="TRASH", emboss=False)


class C2B_PT_help(_Base, Panel):
    bl_idname = "C2B_PT_help"
    bl_label = "How it works"
    bl_parent_id = "C2B_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        col = self.layout.column()
        col.scale_y = 0.8
        for line in (
            "1. Pick a mode, write a prompt, select targets.",
            "2. Add reference images if you have them.",
            "3. Generate with Claude (uses your Claude Code",
            "    sign-in). Or copy for Claude Desktop instead.",
            "4. Claude's change waits here for review.",
            "5. Apply it. Undo/Redo step through History",
            "    without asking Claude again.",
        ):
            col.label(text=line)
        self.layout.operator("claude2blender.copy_request_json", icon="COPYDOWN")


_classes = (C2B_UL_references, C2B_UL_history, C2B_PT_main, C2B_PT_selection, C2B_PT_references, C2B_PT_apply,
            C2B_PT_history, C2B_PT_help)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
