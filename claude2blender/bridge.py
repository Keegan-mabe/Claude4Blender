"""The link between this panel and Claude.

v0.1 reuses the existing Blender MCP instead of opening a second connection:

    Claude Desktop --MCP--> blender-mcp server --TCP :9876--> MCP add-on in Blender
                                                                   |
                                                         execute_code (main thread)
                                                                   |
                                     bpy.app.driver_namespace["claude2blender"]  <- this module

1. "Copy for Claude Desktop" snapshots the prompt, mode, selection (objects and edit-mode
   faces) and references into a request, and copies a short message to the
   clipboard for you to paste into Claude Desktop.
2. Claude reads the full request with the MCP's execute_blender_code tool:
       bpy.app.driver_namespace["claude2blender"].get_request()
3. Claude stages its change instead of running it directly:
       bpy.app.driver_namespace["claude2blender"].stage_proposal(code=..., summary=...)
4. You review the proposal in the sidebar and press Apply (one undo step), or Discard.

A later version can replace steps 1-3 with a direct connection from inside
Blender; the request and proposal formats stay the same.
"""

import json
import os
import time

import bpy
import bmesh
from bpy.app.handlers import persistent

from . import history, scene_tools
from .properties import MODE_LABELS, PERMISSION_ITEMS, get_state, permissions_for

API_NAME = "claude2blender"
API_VERSION = "0.5.0"

# Cap on face indices sent per object, so a dense CAD mesh can't flood the MCP socket.
MAX_FACE_INDICES = 20000

MODE_GUIDANCE = {
    "MATERIAL": (
        "Material Match: analyse the material references and build a photorealistic node-based "
        "material (Principled BSDF with procedural detail). Put it on the targets with "
        "assign_material(mat), which handles objects vs selected faces exactly. Don't create or edit UVs."
    ),
    "ENVIRONMENT": (
        "Environment Match: recreate the reference's lighting, background and studio character with real, "
        "editable Blender elements (area/spot/sun lights, a procedural world, a backdrop, exposure and "
        "contrast) using the c2b helpers. Stay inside request['permissions']; never move product objects "
        "unless object positions/rotations are allowed."
    ),
    "COMPOSITION": (
        "Composition Match: recreate the reference's camera angle, focal length, framing and the arrangement "
        "of the EXISTING scene objects (identify them by name and layout; never add replacement objects). "
        "Use c2b.camera / c2b.frame / c2b.place / c2b.explode. Move only request['permissions']['movable'] "
        "objects, and change transforms only (never mesh data, scale or parenting)."
    ),
}

PRESERVE_RULE = (
    "Preserve Geometry is ON: do not edit mesh data (vertices, edges, faces), object scale or "
    "dimensions, or add/remove geometry modifiers on existing objects, and do not delete existing "
    "objects. Moving/rotating objects, materials, lights, cameras and new helper objects are fine. "
    "The panel rolls back any Apply that breaks this."
)

_last_request = None


# ---------------------------------------------------------------- selection

def _round(values, digits=4):
    return [round(float(v), digits) for v in values]


def compress_indices(indices):
    """[0,1,2,5,7,8] -> '0-2,5,7-8'"""
    if not indices:
        return ""
    indices = sorted(indices)
    parts = []
    start = prev = indices[0]
    for i in indices[1:]:
        if i == prev + 1:
            prev = i
            continue
        parts.append(f"{start}-{prev}" if prev != start else str(start))
        start = prev = i
    parts.append(f"{start}-{prev}" if prev != start else str(start))
    return ",".join(parts)


def expand_indices(text):
    """Inverse of compress_indices. Handy inside staged code."""
    out = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def object_info(obj, active):
    info = {
        "name": obj.name,
        "type": obj.type,
        "active": obj == active,
        "location": _round(obj.location),
        "rotation_euler": _round(obj.rotation_euler),
        "scale": _round(obj.scale),
        "dimensions": _round(obj.dimensions),
        "materials": [slot.material.name if slot.material else None for slot in obj.material_slots],
    }
    if obj.type == "MESH" and obj.data is not None:
        me = obj.data
        info["mesh"] = {"name": me.name, "vertices": len(me.vertices), "faces": len(me.polygons)}
    return info


def face_selection(context):
    """Selected faces for every mesh currently in Edit Mode."""
    result = []
    if context.mode != "EDIT_MESH":
        return result
    objects = getattr(context, "objects_in_mode", None) or [context.edit_object]
    for obj in objects:
        if obj is None or obj.type != "MESH":
            continue
        bm = bmesh.from_edit_mesh(obj.data)
        selected = [f for f in bm.faces if f.select]
        if not selected:
            continue
        indices = [f.index for f in selected]
        entry = {
            "object": obj.name,
            "selected_faces": len(indices),
            "total_faces": len(bm.faces),
            "material_indices_in_use": sorted({f.material_index for f in selected}),
            "faces": compress_indices(indices[:MAX_FACE_INDICES]),
        }
        if len(indices) > MAX_FACE_INDICES:
            entry["faces_truncated"] = True
        result.append(entry)
    return result


def live_selection(context=None):
    context = context or bpy.context
    active = context.view_layer.objects.active
    selected = list(context.selected_objects)
    if context.mode == "EDIT_MESH":
        for obj in getattr(context, "objects_in_mode", None) or []:
            if obj not in selected:
                selected.append(obj)
    return {
        "mode": context.mode,
        "active_object": active.name if active else None,
        "objects": [object_info(o, active) for o in selected],
        "faces": face_selection(context),
    }


# ---------------------------------------------------------------- request

def build_request(context=None):
    context = context or bpy.context
    state = get_state(context)
    scene = context.scene
    refs = []
    for ref in state.references:
        path = bpy.path.abspath(ref.filepath)
        refs.append({
            "path": path,
            "file": os.path.basename(path),
            "role": ref.role.lower(),
            "note": ref.note,
            "exists": os.path.isfile(path),
        })
    request = {
        "claude2blender": API_VERSION,
        "request_id": state.request_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "prompt": state.prompt.strip(),
        "mode": state.mode.lower(),
        "mode_label": MODE_LABELS[state.mode],
        "mode_guidance": MODE_GUIDANCE[state.mode],
        "preserve_geometry": state.preserve_geometry,
        "rules": [PRESERVE_RULE] if state.preserve_geometry else [],
        "permissions": permissions_block(state, context),
        "selection": live_selection(context),
        "references": refs,
        "scene": {
            "name": scene.name,
            "file": bpy.data.filepath or None,
            "render_engine": scene.render.engine,
            "unit_system": scene.unit_settings.system,
            "unit_scale": scene.unit_settings.scale_length,
            "camera": scene.camera.name if scene.camera else None,
        },
        "how_to_respond": (
            "Do not change the scene directly. Put your change in Python code and stage it with "
            "bpy.app.driver_namespace['claude2blender'].stage_proposal(code=..., summary=...). "
            "The code runs in Object Mode with `bpy` and `request` (this dict) available, and "
            "`expand_indices('0-2,5')` to turn face strings into lists, `assign_material(mat)` to put a "
            "material on exactly the selected objects or faces, and `c2b` scene helpers for Environment and "
            "Composition (c2b.light, c2b.camera, c2b.backdrop, c2b.studio_world, c2b.exposure, c2b.place, "
            "c2b.look_at, c2b.frame, c2b.explode, c2b.bounds, c2b.retire; see "
            "bpy.app.driver_namespace['claude2blender'].helpers_help()). The user reviews it and presses Apply."
        ),
    }
    if state.mode in ("ENVIRONMENT", "COMPOSITION"):
        request["scene"]["layout"] = scene_tools.scene_layout(request)
    return request


def permissions_block(state, context=None):
    """What an Environment/Composition change may touch, for Claude and for the guard."""
    perms = permissions_for(state)
    if perms is None:
        return None
    allowed = [label for key, label, *_ in PERMISSION_ITEMS if perms[key]]
    blocked = [label for key, label, *_ in PERMISSION_ITEMS if not perms[key]]
    from . import guard
    movable = sorted(guard.movable_names({"selection": live_selection(context)}, perms))
    moves = perms["positions"] or perms["rotations"] or perms["spacing"]
    return dict(perms, allowed=allowed, not_allowed=blocked,
                movable=movable if moves else [],
                note=("Changes outside these are rolled back automatically. Never delete anything; "
                      "hide with c2b.retire(obj) instead. Keep the output resolution and render engine "
                      "unless 'output' is allowed."))


def _selection_phrase(request):
    sel = request["selection"]
    if sel["faces"]:
        return ", ".join(f"{f['object']} ({f['selected_faces']} faces)" for f in sel["faces"])
    names = [o["name"] for o in sel["objects"]]
    if not names:
        return "nothing selected"
    if len(names) > 6:
        return ", ".join(names[:6]) + f" and {len(names) - 6} more"
    return ", ".join(names)


def clipboard_message(request):
    refs = request["references"]
    lines = [
        f"[Claude2Blender request #{request['request_id']}] {request['mode_label']}",
        "",
        f"Request: {request['prompt'] or '(no prompt written)'}",
        f"Targets: {_selection_phrase(request)}",
        "Preserve geometry: " + ("ON" if request["preserve_geometry"] else "off"),
    ]
    perms = request.get("permissions")
    if perms:
        lines.append("Allowed changes: " + (", ".join(perms["allowed"]) or "none")
                     + (" · may move: " + ", ".join(perms["movable"][:6]) if perms["movable"] else ""))
    if refs:
        lines.append("References: " + "; ".join(f"{r['file']} ({r['role']})" for r in refs))
        lines.append("(I've attached the reference images to this message.)")
    lines += [
        "",
        "Use the Blender MCP. First read the full request with execute_blender_code:",
        "import bpy, json; print(json.dumps(bpy.app.driver_namespace['claude2blender'].get_request(), indent=1))",
        "Then stage your change for my review instead of running it directly:",
        "bpy.app.driver_namespace['claude2blender'].stage_proposal(code=CODE, summary=SUMMARY)",
        "I'll review it and press Apply in the Claude2Blender panel.",
    ]
    return "\n".join(lines)


def request_file_path():
    folder = bpy.utils.user_resource("CONFIG", path="claude2blender", create=True)
    return os.path.join(folder, "last_request.json")


def scene_overview(limit=150):
    """Compact list of scene objects, so Claude knows what else is there."""
    objs = []
    for obj in bpy.context.scene.objects[:limit]:
        objs.append({
            "name": obj.name,
            "type": obj.type,
            "materials": [s.material.name if s.material else None for s in obj.material_slots][:8],
            "location": _round(obj.location, 3),
            "dimensions": _round(obj.dimensions, 3),
        })
    return objs


def send_request(context, copy_to_clipboard=True):
    """Snapshot a new request. Returns (request, clipboard_text)."""
    global _last_request
    state = get_state(context)
    state.request_id += 1
    request = build_request(context)
    _last_request = request
    text = clipboard_message(request)
    if copy_to_clipboard:
        try:
            context.window_manager.clipboard = text
        except Exception:
            pass
        state.request_summary = f"#{request['request_id']} · {request['mode_label']} · {_selection_phrase(request)}"
    try:
        with open(request_file_path(), "w", encoding="utf-8") as fh:
            json.dump(request, fh, indent=1)
    except OSError:
        pass
    return request, text


def get_request_for_apply(context):
    return _last_request if _last_request is not None else build_request(context)


# ---------------------------------------------------------------- API for Claude

def _redraw():
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


class Claude2BlenderAPI:
    """Reached from the MCP's execute_blender_code as bpy.app.driver_namespace['claude2blender']."""

    version = API_VERSION

    def help(self):
        return __doc__

    def get_request(self):
        """The request the user last sent from the panel (None if none yet)."""
        return _last_request

    def get_request_json(self):
        return json.dumps(_last_request, indent=1)

    def live_context(self):
        """Current panel state and selection, without the user pressing Send."""
        return build_request(bpy.context)

    def stage_proposal(self, code, summary, request_id=None, _from_generate=False):
        """Stage Python code for the user to review and Apply from the panel."""
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must be a non-empty string of Python")
        compile(code, "<claude2blender proposal>", "exec")  # syntax errors surface to Claude now
        state = get_state()
        state.proposal_code = code
        state.proposal_summary = (summary or "Change from Claude").strip()
        state.proposal_request_id = request_id if request_id is not None else state.request_id
        state.proposal_pending = True
        state.target_notes = ""
        if not _from_generate:
            # Staged through the Claude Desktop fallback: the Generate card no longer describes it.
            if state.gen_outcome == "STAGED":
                state.gen_outcome = "NONE"
            state.status = "Claude staged a change. Review it in the Claude panel."
        _redraw()
        return "Staged for review. Ask the user to press Apply in the Claude2Blender sidebar panel."

    def helpers_help(self):
        """Documentation of the `c2b` helpers available to staged code."""
        import inspect
        lines = [scene_tools.__doc__]
        for name in dir(scene_tools.Helpers):
            if not name.startswith("_"):
                fn = getattr(scene_tools.Helpers, name)
                try:
                    sig = str(inspect.signature(fn))
                except (TypeError, ValueError):
                    sig = "(...)"
                lines.append(f"c2b.{name}{sig}: {(inspect.getdoc(fn) or '').strip()}")
        return "\n".join(lines)

    def scene_layout(self):
        """World-space layout of lights, cameras, world and objects (what Environment/Composition need)."""
        return scene_tools.scene_layout(_last_request)

    def proposal_status(self):
        state = get_state()
        return {
            "pending": state.proposal_pending,
            "summary": state.proposal_summary,
            "last_apply_ok": state.last_apply_ok,
            "last_apply_summary": state.last_apply_summary,
            "status": state.status,
            "history": [
                {"id": e["id"], "summary": e["summary"], "state": e["state"].lower(), "time": e["time"]}
                for e in history.entries()
            ],
        }

    expand_indices = staticmethod(expand_indices)


_api = Claude2BlenderAPI()


def _install_api():
    bpy.app.driver_namespace[API_NAME] = _api


@persistent
def _on_load_post(*_args):
    # driver_namespace is reset when a file loads.
    _install_api()


# ---------------------------------------------------------------- MCP status

def mcp_status():
    """('missing' | 'stopped' | 'running', port)"""
    if not hasattr(bpy.types, "BLENDERMCP_PT_Panel"):
        return "missing", None
    server = getattr(bpy.types, "blendermcp_server", None)
    if server is not None and getattr(server, "running", False):
        return "running", getattr(server, "port", None)
    return "stopped", None


def register():
    _install_api()
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    bpy.app.driver_namespace.pop(API_NAME, None)
