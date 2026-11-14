"""Add-on state.

Everything lives on the WindowManager, which Blender does not save into .blend
files. Using the panel therefore never changes the project file you have open;
only an Apply you confirm does.
"""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from .presets import PRESET_ITEMS
from .scene_tools import STYLE_ITEMS

MODES = [
    ("MATERIAL", "Material", "Material Match: recreate a material, texture or finish from a reference", "MATERIAL", 0),
    ("ENVIRONMENT", "Environment", "Environment Match: match lighting, background and world", "WORLD", 1),
    ("COMPOSITION", "Composition", "Composition Match: match camera, product pose or exploded view", "CAMERA_DATA", 2),
]

MODE_LABELS = {
    "MATERIAL": "Material Match",
    "ENVIRONMENT": "Environment Match",
    "COMPOSITION": "Composition Match",
}

REFERENCE_ROLES = [
    ("MATERIAL", "Material", "Material, texture or surface finish reference", "MATERIAL", 0),
    ("ENVIRONMENT", "Environment", "Lighting, background or HDRI reference", "WORLD", 1),
    ("COMPOSITION", "Composition", "Camera angle and framing reference", "CAMERA_DATA", 2),
    ("PRODUCT", "Product", "Product pose or exploded CAD view reference", "MESH_CUBE", 3),
]

ROLE_ICONS = {item[0]: item[3] for item in REFERENCE_ROLES}


class C2B_Reference(bpy.types.PropertyGroup):
    filepath: StringProperty(name="File", subtype="FILE_PATH")
    role: EnumProperty(name="Use as", items=REFERENCE_ROLES, default="MATERIAL")
    note: StringProperty(
        name="Note",
        description="Optional: what Claude should take from this image (e.g. 'brushed finish only, ignore colour')",
    )


HISTORY_STATES = [
    ("APPLIED", "Applied", "In effect", "CHECKMARK", 0),
    ("UNDONE", "Undone", "Undone; Redo or Go To brings it back", "LOOP_BACK", 1),
    ("ALT", "Saved", "No longer in the timeline; Apply Again re-runs its code", "FILE_SCRIPT", 2),
]


class C2B_HistoryItem(bpy.types.PropertyGroup):
    uid: IntProperty()
    summary: StringProperty()
    time: StringProperty()
    state: EnumProperty(items=HISTORY_STATES, default="APPLIED")


PERMISSION_ITEMS = (
    # (key, label, icon, description)
    ("lighting", "Lighting", "LIGHT_AREA", "Create and adjust lights, exposure and contrast"),
    ("world", "World", "WORLD", "Set the world background and environment lighting"),
    ("backdrop", "Backdrop", "MESH_PLANE", "Create a studio backdrop or floor"),
    ("camera", "Camera", "CAMERA_DATA", "Move the camera and change its lens or depth of field"),
    ("positions", "Positions", "ORIENTATION_GLOBAL", "Move the selected objects"),
    ("rotations", "Rotations", "ORIENTATION_GIMBAL", "Rotate the selected objects"),
    ("spacing", "Spacing", "FULLSCREEN_ENTER", "Change the distances between the selected objects (exploded views)"),
    ("output", "Output", "OUTPUT", "Change the render engine or output resolution"),
)

PERMISSION_DEFAULTS = {
    # Environment: lights, world and backdrop; the camera and objects stay put unless you allow them.
    "ENVIRONMENT": {"lighting": True, "world": True, "backdrop": True},
    # Composition: camera and the selected objects; lighting stays as it is.
    "COMPOSITION": {"camera": True, "positions": True, "rotations": True, "spacing": True},
}


def _permissions_class(mode):
    defaults = PERMISSION_DEFAULTS[mode]
    ann = {}
    for key, label, _icon, desc in PERMISSION_ITEMS:
        ann[key] = BoolProperty(name=label, description=f"Claude may: {desc.lower()}",
                                default=defaults.get(key, False))
    ann["full_assembly"] = BoolProperty(
        name="Full assembly",
        description="Allow moving every object in the scene, not only the selected ones",
        default=False,
    )
    return type(f"C2B_Permissions_{mode.title()}", (bpy.types.PropertyGroup,), {"__annotations__": ann})


C2B_PermissionsEnv = _permissions_class("ENVIRONMENT")
C2B_PermissionsComp = _permissions_class("COMPOSITION")


def permissions_for(state, mode=None):
    """The allowed-change toggles of a mode as a dict (None for Material mode)."""
    mode = (mode or state.mode).upper()
    group = {"ENVIRONMENT": state.perm_env, "COMPOSITION": state.perm_comp}.get(mode)
    if group is None:
        return None
    out = {key: getattr(group, key) for key, *_ in PERMISSION_ITEMS}
    out["full_assembly"] = group.full_assembly
    return out


class C2B_State(bpy.types.PropertyGroup):
    prompt: StringProperty(
        name="Prompt",
        description="Describe what you want Claude to do",
        default="",
    )
    mode: EnumProperty(name="Mode", items=MODES, default="MATERIAL")
    preset: EnumProperty(name="Studio preset", items=PRESET_ITEMS, default="plastic_white",
                         description="Tuned product material from the studio library (no Claude call needed)")
    studio_style: EnumProperty(name="Studio style", items=STYLE_ITEMS, default="dark",
                               description="Tuned lighting, world and backdrop from the studio library (no Claude call needed)")
    preserve_geometry: BoolProperty(
        name="Preserve Geometry",
        description=(
            "Protect existing meshes: Apply is rolled back if vertices, faces, "
            "scale, dimensions or geometry modifiers of existing objects change. "
            "Moving and rotating objects is still allowed"
        ),
        default=True,
    )

    perm_env: PointerProperty(type=C2B_PermissionsEnv)
    perm_comp: PointerProperty(type=C2B_PermissionsComp)
    show_permissions: BoolProperty(name="Allowed changes", description="Choose what Claude may change", default=False)

    references: CollectionProperty(type=C2B_Reference)
    active_reference: IntProperty(name="Active Reference", default=0)

    # Last request handed to Claude.
    request_id: IntProperty(default=0)
    request_summary: StringProperty(default="")

    # Change staged by Claude for review.
    proposal_pending: BoolProperty(default=False)
    proposal_summary: StringProperty(default="")
    proposal_code: StringProperty(default="")
    proposal_request_id: IntProperty(default=0)

    # Result of the last Apply.
    last_apply_ok: BoolProperty(default=False)
    last_apply_summary: StringProperty(default="")
    status: StringProperty(default="")

    # Generate with Claude (Claude Code CLI)
    gen_status: EnumProperty(items=[
        ("IDLE", "Idle", "", "BLANK1", 0),
        ("CONNECTING", "Connecting", "", "SORTTIME", 1),
        ("PROCESSING", "Processing", "", "TIME", 2),
        ("COMPLETED", "Completed", "", "CHECKMARK", 3),
        ("FAILED", "Failed", "", "ERROR", 4),
    ], default="IDLE")
    gen_detail: StringProperty(default="")
    gen_message: StringProperty(default="")
    gen_session_id: StringProperty(default="")
    gen_follow_up: BoolProperty(
        name="Follow-up",
        description="Continue the current conversation so Claude refines its previous result",
        default=True,
    )
    gen_turns: IntProperty(default=0)
    gen_started: FloatProperty(default=0.0)
    gen_request_id: IntProperty(default=0)
    # Compact completion card
    gen_elapsed: FloatProperty(default=0.0)
    gen_title: StringProperty(default="")
    gen_targets: StringProperty(default="")
    gen_outcome: EnumProperty(items=[
        ("NONE", "None", ""),
        ("STAGED", "Staged", "Waiting for review"),
        ("QUESTION", "Question", "Claude needs your input"),
        ("APPLIED", "Applied", "Applied to the scene"),
        ("DISCARDED", "Discarded", "Discarded without applying"),
    ], default="NONE")
    gen_history_uid: IntProperty(default=0)
    target_notes: StringProperty(default="")
    # Material summary from the last Generate
    mat_name: StringProperty(default="")
    mat_swatch: FloatVectorProperty(name="Colour", subtype="COLOR_GAMMA", size=3, min=0.0, max=1.0,
                                    default=(0.5, 0.5, 0.5))
    mat_has_swatch: BoolProperty(default=False)
    mat_line: StringProperty(default="")
    mat_surface: StringProperty(default="")
    mat_match: StringProperty(default="")
    mat_limits: StringProperty(default="")
    # Environment / Composition summary from the last Generate
    scene_lines: StringProperty(default="")   # "icon|text" per line
    scene_match: StringProperty(default="")
    scene_limits: StringProperty(default="")
    show_changes: BoolProperty(name="Changes", description="Show what the change does to the scene", default=True)
    gen_show_details: BoolProperty(name="Details", description="Show Claude's explanation", default=False)
    cli_status: StringProperty(default="")
    cli_ok: BoolProperty(default=False)

    # Mirror of history.py's entries for display (newest first).
    history: CollectionProperty(type=C2B_HistoryItem)
    active_history: IntProperty(name="History", default=0)


_classes = (C2B_Reference, C2B_HistoryItem, C2B_PermissionsEnv, C2B_PermissionsComp, C2B_State)


def get_state(context=None):
    context = context or bpy.context
    return context.window_manager.claude2blender


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.claude2blender = PointerProperty(type=C2B_State)


def unregister():
    del bpy.types.WindowManager.claude2blender
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
