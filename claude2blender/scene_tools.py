"""Scene helpers for Environment and Composition changes.

Claude's staged code gets these as `c2b` (for example `c2b.light(...)`). They
make the common steps exact and repeatable:

* Lights, cameras, backdrops and studio worlds are looked up by name and
  updated if they exist, so refinements never pile up duplicates. Everything
  they create goes into one "Claude2Blender Studio" collection and is tagged
  with a `c2b_role` custom property.
* `place`, `look_at` and `explode` move objects in world space but write only
  location and rotation, through the parent's matrix. Scale, parenting and the
  parent inverse are never touched, so children and CAD dimensions stay intact.
* Nothing here deletes anything. `retire(obj)` hides a light or helper the
  look no longer needs.

`scene_layout()` builds the scene description Claude receives for these modes.
"""

import math

import bpy
import bmesh
from mathutils import Euler, Matrix, Vector

STUDIO_COLLECTION = "Claude2Blender Studio"
ROLE_KEY = "c2b_role"

EEVEE_ENGINES = {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}


# ---------------------------------------------------------------- small utilities

def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def color(value, alpha=False):
    """'#RRGGBB' (sRGB) or an (r, g, b) tuple of linear floats -> linear (r, g, b[, a])."""
    if isinstance(value, str):
        h = value.strip().lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        rgb = tuple(srgb_to_linear(int(h[i:i + 2], 16) / 255.0) for i in (0, 2, 4))
    else:
        rgb = tuple(float(v) for v in tuple(value)[:3])
    return rgb + (1.0,) if alpha else rgb


def role(idb):
    try:
        return idb.get(ROLE_KEY)
    except Exception:
        return None


def is_generated(idb):
    return bool(role(idb))


def _vec(v):
    if v is None:
        return None
    if isinstance(v, bpy.types.Object):
        return v.matrix_world.translation.copy()
    return Vector(tuple(v)[:3])


def studio_collection(scene=None):
    scene = scene or bpy.context.scene
    coll = bpy.data.collections.get(STUDIO_COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(STUDIO_COLLECTION)
        coll[ROLE_KEY] = "studio"
    if coll.name not in scene.collection.children and not _nested_in(scene.collection, coll):
        scene.collection.children.link(coll)
    return coll


def _nested_in(parent, coll):
    for child in parent.children:
        if child == coll or _nested_in(child, coll):
            return True
    return False


def _get_object(name, data_kind, obj_type, make_data, role_name):
    obj = bpy.data.objects.get(name)
    if obj is not None and obj.type != obj_type:
        raise ValueError(f"'{name}' already exists and is a {obj.type}, not a {obj_type}; use another name")
    if obj is None:
        data = make_data()
        obj = bpy.data.objects.new(name, data)
        obj[ROLE_KEY] = role_name
        studio_collection().objects.link(obj)
    if not obj.users_collection:
        studio_collection().objects.link(obj)
    obj.hide_viewport = False
    obj.hide_render = False
    return obj


# ---------------------------------------------------------------- transforms (parent-aware)

def _world_to_local(obj, world_matrix):
    """matrix_basis that gives `world_matrix`, respecting the parent and its inverse."""
    if obj.parent is None:
        return world_matrix
    parent_world = obj.parent.matrix_world @ obj.matrix_parent_inverse
    return parent_world.inverted_safe() @ world_matrix


def place(obj, location=None, rotation=None, world=True):
    """Move/rotate `obj` without touching its scale, parent or children's local transforms.

    location: (x, y, z); rotation: Euler XYZ in radians, a Matrix or a Quaternion.
    world=True means both are given in world space; children follow as usual.
    """
    if location is None and rotation is None:
        return obj
    if not world:
        if location is not None:
            obj.location = tuple(location)
        if rotation is not None:
            _set_rotation(obj, _rot_matrix(rotation))
        _refresh()
        return obj
    cur = obj.matrix_world.copy()
    loc, rot, _scale = cur.decompose()
    if location is not None:
        loc = Vector(tuple(location))
    rot_m = _rot_matrix(rotation) if rotation is not None else rot.to_matrix()
    target = Matrix.Translation(loc) @ rot_m.to_4x4()
    basis = _world_to_local(obj, target)
    b_loc, b_rot, _ = basis.decompose()
    obj.location = b_loc
    if rotation is not None:
        _set_rotation(obj, b_rot.to_matrix())
    _refresh()
    return obj


def _refresh():
    # matrix_world is only recomputed on a depsgraph update; later steps read it.
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass


def _rot_matrix(rotation):
    if isinstance(rotation, Matrix):
        return rotation.to_3x3()
    if hasattr(rotation, "to_matrix"):
        return rotation.to_matrix()
    return Euler(tuple(rotation), "XYZ").to_matrix()


def _set_rotation(obj, rot3):
    mode = obj.rotation_mode
    if mode == "QUATERNION":
        obj.rotation_quaternion = rot3.to_quaternion()
    elif mode == "AXIS_ANGLE":
        axis, angle = rot3.to_quaternion().to_axis_angle()
        obj.rotation_axis_angle = (angle, *axis)
    else:
        obj.rotation_euler = rot3.to_euler(mode, obj.rotation_euler)


def look_at(obj, target, roll=0.0):
    """Point a camera/light (its -Z axis) at `target` (a point or an object), in world space."""
    target = _vec(target)
    pos = obj.matrix_world.translation
    direction = target - pos
    if direction.length < 1e-9:
        return obj
    rot = direction.to_track_quat("-Z", "Y").to_matrix()
    if roll:
        rot = rot @ Matrix.Rotation(roll, 3, "Z")
    place(obj, rotation=rot)
    return obj


def bounds(objects=None):
    """World-space bounding box of objects (default: the request targets).

    Returns dict(min, max, center, size, radius) with Vectors.
    """
    objects = list(objects) if objects is not None else targets()
    pts = []
    for obj in objects:
        if obj.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}:
            pts.extend(obj.matrix_world @ Vector(c) for c in obj.bound_box)
        else:
            pts.append(obj.matrix_world.translation.copy())
    if not pts:
        z = Vector((0, 0, 0))
        return {"min": z, "max": z, "center": z, "size": z, "radius": 0.0}
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return {"min": lo, "max": hi, "center": (lo + hi) / 2, "size": hi - lo, "radius": (hi - lo).length / 2}


def explode(objects, axis=(0, 0, 1), spacing=0.1, center=None, order=None):
    """Spread objects along `axis` (world space) with `spacing` between successive parts.

    Parts are ordered by their current position along the axis (or by `order`, a
    list of objects) and moved only along that axis, around their middle, so the
    group stays centred. Rotation, scale and parenting are untouched. Children
    of a moved part follow it, so pass top-level parts.
    """
    axis = Vector(tuple(axis)).normalized()
    objects = list(order or sorted(objects, key=lambda o: o.matrix_world.translation.dot(axis)))
    if not objects:
        return []
    n = len(objects)
    proj = [o.matrix_world.translation.dot(axis) for o in objects]
    mid = sum(proj) / n if center is None else _vec(center).dot(axis)
    start = mid - spacing * (n - 1) / 2
    for i, obj in enumerate(objects):
        world = obj.matrix_world.translation.copy()
        world += axis * ((start + i * spacing) - proj[i])
        place(obj, location=world)
    return objects


def frame(camera, objects=None, direction=None, margin=1.1, target=None):
    """Move `camera` so `objects` fill the frame, looking from `direction` (world vector from target to camera).

    Keeps the camera's current direction if none is given. Fits the real bounding-box
    corners to the camera's lens, sensor and the scene's output aspect (resolution is
    never changed). margin 1.1 = 10% breathing room.
    """
    objects = list(objects) if objects is not None else targets()
    box = bounds(objects)
    center = _vec(target) if target is not None else box["center"]
    if direction is None:
        direction = camera.matrix_world.translation - center
        if direction.length < 1e-6:
            direction = Vector((0, -1, 0.4))
    direction = Vector(tuple(direction)).normalized()
    corners = []
    for obj in objects:
        if obj.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}:
            corners.extend(obj.matrix_world @ Vector(c) for c in obj.bound_box)
        else:
            corners.append(obj.matrix_world.translation.copy())
    if not corners:
        corners = [center]
    cam = camera.data
    scene = bpy.context.scene
    aspect = (scene.render.resolution_x * scene.render.pixel_aspect_x) / max(
        1, scene.render.resolution_y * scene.render.pixel_aspect_y)
    rot = (-direction).to_track_quat("-Z", "Y").to_matrix()
    inv = rot.transposed()
    local = [inv @ (p - center) for p in corners]   # camera axes: x right, y up, -z forward
    if cam.type == "ORTHO":
        half_w = max(abs(p.x) for p in local) * margin
        half_h = max(abs(p.y) for p in local) * margin
        cam.ortho_scale = 2 * max(half_w, half_h * aspect) if aspect >= 1 else 2 * max(half_w / aspect, half_h)
        distance = max(box["radius"] * 4, 0.1)
    else:
        fit = cam.sensor_fit
        sensor = cam.sensor_height if fit == "VERTICAL" else cam.sensor_width
        half = sensor / (2 * cam.lens)          # tan(half fov) along the fitted axis
        if fit == "VERTICAL" or (fit == "AUTO" and aspect < 1):
            tan_v, tan_h = half, half * aspect
        else:
            tan_h, tan_v = half, half / aspect
        tan_h /= margin
        tan_v /= margin
        # Smallest distance where every corner is inside the frame: |x| <= tan_h * (d - z) etc.
        distance = max(max(abs(p.x) / tan_h + p.z, abs(p.y) / tan_v + p.z) for p in local)
        distance = max(distance, cam.clip_start * 2)
    place(camera, location=center + direction * distance)
    look_at(camera, center)
    return camera


def retire(obj):
    """Hide a light or helper the look no longer needs (nothing is ever deleted)."""
    obj.hide_viewport = True
    obj.hide_render = True
    return obj


# ---------------------------------------------------------------- lights

LIGHT_TYPES = {"AREA", "POINT", "SPOT", "SUN"}


def light(name, kind="AREA", location=None, target=None, rotation=None, energy=None, color_=None,
          size=None, size_y=None, shape=None, spread=None, spot_size=None, spot_blend=None,
          angle=None, shadow=True, **extra):
    """Create or update a light called `name` (refinements reuse it: no duplicates).

    energy: Watts (Sun: W/m², i.e. strength). color: '#RRGGBB' or linear RGB.
    size: area size / point & spot radius (larger = softer shadows); angle: sun
    angular diameter in radians. target: point or object to aim at.
    """
    kind = kind.upper()
    if kind not in LIGHT_TYPES:
        raise ValueError(f"kind must be one of {sorted(LIGHT_TYPES)}")
    col = extra.pop("color", color_)
    obj = _get_object(name, "lights", "LIGHT", lambda: bpy.data.lights.new(name, kind), "light")
    data = obj.data
    if data.type != kind:
        data.type = kind
    data = obj.data
    if energy is not None:
        data.energy = float(energy)
    if col is not None:
        data.color = color(col)
    if hasattr(data, "use_shadow"):
        data.use_shadow = bool(shadow)
    if kind == "AREA":
        if shape:
            data.shape = shape.upper()
        if size is not None:
            data.size = float(size)
        if size_y is not None:
            data.size_y = float(size_y)
        if spread is not None and hasattr(data, "spread"):
            data.spread = float(spread)
    elif kind in {"POINT", "SPOT"}:
        if size is not None:
            data.shadow_soft_size = float(size)
        if kind == "SPOT":
            if spot_size is not None:
                data.spot_size = float(spot_size)
            if spot_blend is not None:
                data.spot_blend = float(spot_blend)
    elif kind == "SUN" and angle is not None:
        data.angle = float(angle)
    for key, value in extra.items():
        setattr(data, key, value)
    if location is not None:
        place(obj, location=location)
    if rotation is not None:
        place(obj, rotation=rotation)
    if target is not None:
        look_at(obj, target)
    return obj


# ---------------------------------------------------------------- camera

def camera(name="C2B Camera", location=None, target=None, rotation=None, lens=None, sensor_width=None,
           make_active=True, shift_x=None, shift_y=None, clip_start=None, clip_end=None,
           dof_target=None, fstop=None, use_existing=None):
    """Create or update a camera. use_existing=<camera object> adjusts that camera instead.

    Only lens/sensor/shift/clip/DOF and the transform are set; the render
    resolution is never touched.
    """
    if use_existing is not None:
        obj = use_existing
        if obj.type != "CAMERA":
            raise ValueError(f"{obj.name} is not a camera")
    else:
        obj = _get_object(name, "cameras", "CAMERA", lambda: bpy.data.cameras.new(name), "camera")
    cam = obj.data
    for attr, value in (("lens", lens), ("sensor_width", sensor_width), ("shift_x", shift_x),
                        ("shift_y", shift_y), ("clip_start", clip_start), ("clip_end", clip_end)):
        if value is not None:
            setattr(cam, attr, float(value))
    if dof_target is not None or fstop is not None:
        cam.dof.use_dof = True
        if dof_target is not None:
            if isinstance(dof_target, bpy.types.Object):
                cam.dof.focus_object = dof_target
            else:
                cam.dof.focus_distance = float(dof_target)
        if fstop is not None:
            cam.dof.aperture_fstop = float(fstop)
    if location is not None:
        place(obj, location=location)
    if rotation is not None:
        place(obj, rotation=rotation)
    if target is not None:
        look_at(obj, target)
    if make_active:
        bpy.context.scene.camera = obj
    return obj


# ---------------------------------------------------------------- backdrop

def _sweep_mesh(name, width, depth, height, curve, segments=16):
    """Infinity-cove backdrop: floor (y<0 side toward camera), curve, back wall. Origin at floor centre."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    curve = max(0.0, min(curve, depth * 0.95, height * 0.95))
    profile = [(-depth / 2, 0.0)]
    floor_end = depth / 2 - curve
    profile.append((floor_end, 0.0))
    if curve > 0:
        for i in range(1, segments):
            a = (math.pi / 2) * i / segments
            profile.append((floor_end + math.sin(a) * curve, curve - math.cos(a) * curve))
    profile.append((depth / 2, curve))
    profile.append((depth / 2, height))
    left, right = [], []
    for y, z in profile:
        left.append(bm.verts.new((-width / 2, y, z)))
        right.append(bm.verts.new((width / 2, y, z)))
    for i in range(len(profile) - 1):
        bm.faces.new((left[i], right[i], right[i + 1], left[i + 1]))
    bm.to_mesh(me)
    bm.free()
    for poly in me.polygons:
        poly.use_smooth = True
    return me


def backdrop(name="C2B Backdrop", kind="sweep", width=None, depth=None, height=None, curve=None,
             location=None, rotation=None, color_=None, roughness=None, material_name=None, **extra):
    """Create or update a studio backdrop under the products.

    kind: 'sweep' (floor curving into a back wall) or 'floor' (flat plane).
    Sizes default to fit the request targets. On refinements the existing
    backdrop is resized through its object scale (its mesh is generated, not
    yours). Material: matte Principled BSDF named `material_name`.
    """
    col = extra.pop("color", color_)
    box = bounds()
    span = max(box["size"].x, box["size"].y, box["size"].z, 0.1)
    width = float(width if width is not None else span * 8)
    depth = float(depth if depth is not None else span * 6)
    height = float(height if height is not None else (span * 4 if kind == "sweep" else 0.0))
    curve = float(curve if curve is not None else min(depth, max(height, 0.01)) * 0.3)
    obj = bpy.data.objects.get(name)
    if obj is not None and obj.type != "MESH":
        raise ValueError(f"'{name}' already exists and is a {obj.type}")
    if obj is None:
        if kind == "floor":
            me = _sweep_mesh(name, width, depth, 0.0, 0.0, segments=1)
        else:
            me = _sweep_mesh(name, width, depth, height, curve)
        obj = bpy.data.objects.new(name, me)
        obj[ROLE_KEY] = "backdrop"
        obj["c2b_size"] = (width, depth, max(height, 1e-6))
        studio_collection().objects.link(obj)
        if location is None:
            location = (box["center"].x, box["center"].y, box["min"].z)
    else:
        base = tuple(obj.get("c2b_size", (width, depth, max(height, 1e-6))))
        obj.scale = (width / base[0], depth / base[1], (max(height, 1e-6) / base[2]) if kind != "floor" else 1.0)
    if not obj.users_collection:
        studio_collection().objects.link(obj)
    obj.hide_viewport = obj.hide_render = False
    if location is not None:
        place(obj, location=location)
    if rotation is not None:
        place(obj, rotation=rotation)
    if col is not None or roughness is not None or not obj.material_slots or obj.material_slots[0].material is None:
        mat = backdrop_material(material_name or f"{name} Material", col if col is not None else "#BDBDBD",
                                roughness if roughness is not None else 0.6)
        if not obj.data.materials:
            obj.data.materials.append(mat)
        else:
            obj.data.materials[0] = mat
    return obj


def backdrop_material(name, col="#BDBDBD", roughness=0.6):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat[ROLE_KEY] = "backdrop"
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"), None)
    if bsdf is None:
        nt.nodes.clear()
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        out.location = (300, 0)
        nt.links.new(bsdf.outputs[0], out.inputs[0])
    bsdf.inputs["Base Color"].default_value = color(col, alpha=True)
    bsdf.inputs["Roughness"].default_value = float(roughness)
    spec = bsdf.inputs.get("Specular IOR Level") or bsdf.inputs.get("Specular")
    if spec is not None:
        spec.default_value = 0.3
    mat.diffuse_color = color(col, alpha=True)
    return mat


# ---------------------------------------------------------------- world

def studio_world(top="#3A3A3A", bottom=None, strength=1.0, camera_color=None, camera_strength=None,
                 horizon=0.5, name="C2B Studio World"):
    """A procedural studio world: vertical gradient from `bottom` to `top`.

    camera_color: what the camera sees as background, if it should differ from
    the light the world gives (e.g. a dark backdrop with a soft fill). Assigns
    it to the scene; your previous world is kept (Undo brings it back).
    """
    bottom = bottom if bottom is not None else top
    world = bpy.data.worlds.get(name) or bpy.data.worlds.new(name)
    world[ROLE_KEY] = "world"
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld")
    out.location = (900, 0)
    coord = nt.nodes.new("ShaderNodeTexCoord")
    coord.location = (-700, 0)
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    sep.location = (-500, 0)
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.location = (-300, 0)
    nt.links.new(coord.outputs["Generated"], sep.inputs[0])
    nt.links.new(sep.outputs["Z"], ramp.inputs["Fac"])
    els = ramp.color_ramp.elements
    lo = max(0.0, min(1.0, horizon - 0.25))
    hi = max(0.0, min(1.0, horizon + 0.25))
    els[0].position, els[1].position = lo, hi
    els[0].color = color(bottom, alpha=True)
    els[1].color = color(top, alpha=True)
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.location = (0, 80)
    bg.inputs["Strength"].default_value = float(strength)
    nt.links.new(ramp.outputs["Color"], bg.inputs["Color"])
    shader = bg.outputs[0]
    if camera_color is not None:
        cam_bg = nt.nodes.new("ShaderNodeBackground")
        cam_bg.location = (0, -120)
        cam_bg.inputs["Color"].default_value = color(camera_color, alpha=True)
        cam_bg.inputs["Strength"].default_value = float(camera_strength if camera_strength is not None else 1.0)
        path = nt.nodes.new("ShaderNodeLightPath")
        path.location = (0, 320)
        mix = nt.nodes.new("ShaderNodeMixShader")
        mix.location = (500, 0)
        nt.links.new(path.outputs["Is Camera Ray"], mix.inputs[0])
        nt.links.new(bg.outputs[0], mix.inputs[1])
        nt.links.new(cam_bg.outputs[0], mix.inputs[2])
        shader = mix.outputs[0]
    nt.links.new(shader, out.inputs["Surface"])
    world.color = color(bottom)
    bpy.context.scene.world = world
    return world


def exposure(value=None, look=None, view_transform=None, film_transparent=None):
    """Colour management: exposure (stops), contrast look, view transform. Resolution is never changed."""
    scene = bpy.context.scene
    vs = scene.view_settings
    if view_transform is not None:
        vs.view_transform = view_transform
    if look is not None:
        try:
            vs.look = look
        except TypeError:
            # Looks are named per view transform ('AgX - Punchy' vs 'Punchy') in some versions.
            prefixed = f"{vs.view_transform} - {look}"
            vs.look = prefixed
    if value is not None:
        vs.exposure = float(value)
    if film_transparent is not None:
        scene.render.film_transparent = bool(film_transparent)
    return vs


def soft_shadows(on=True):
    """Engine-aware soft-shadow switch (EEVEE Legacy only; EEVEE Next and Cycles use light size)."""
    eevee = bpy.context.scene.eevee
    if hasattr(eevee, "use_soft_shadows"):
        eevee.use_soft_shadows = bool(on)


# ---------------------------------------------------------------- targets and request binding

_request = None
_permissions = None


def bind(request, permissions):
    global _request, _permissions
    _request, _permissions = request, permissions


def selected_names(request=None):
    request = request if request is not None else _request
    if not request:
        return []
    sel = request.get("selection", {})
    names = [o["name"] for o in sel.get("objects", [])]
    for f in sel.get("faces", []):
        if f["object"] not in names:
            names.append(f["object"])
    return names


def targets():
    """Objects of the request the change is about: the selection, else every visible mesh."""
    objs = [bpy.data.objects.get(n) for n in selected_names()]
    objs = [o for o in objs if o is not None and o.type not in {"LIGHT", "CAMERA"}]
    if objs:
        return objs
    return [o for o in bpy.context.scene.objects
            if o.type == "MESH" and not is_generated(o) and o.visible_get()]


def movable():
    """Objects this change may move: the selection, or the whole assembly when you allowed it."""
    perms = _permissions or {}
    if perms.get("full_assembly"):
        return [o for o in bpy.context.scene.objects if o.type not in {"LIGHT", "CAMERA"} and not is_generated(o)]
    return [o for o in targets() if o.name in selected_names()]


class Helpers:
    """The `c2b` object in staged code."""

    color = staticmethod(color)
    light = staticmethod(light)
    camera = staticmethod(camera)
    backdrop = staticmethod(backdrop)
    backdrop_material = staticmethod(backdrop_material)
    studio_world = staticmethod(studio_world)
    exposure = staticmethod(exposure)
    soft_shadows = staticmethod(soft_shadows)
    place = staticmethod(place)
    look_at = staticmethod(look_at)
    bounds = staticmethod(bounds)
    explode = staticmethod(explode)
    frame = staticmethod(frame)
    retire = staticmethod(retire)
    targets = staticmethod(targets)
    movable = staticmethod(movable)
    studio_collection = staticmethod(studio_collection)


helpers = Helpers()


# ---------------------------------------------------------------- scene description for Claude

def _r(values, digits=3):
    return [round(float(v), digits) for v in values]


def _deg(euler):
    return [round(math.degrees(a), 1) for a in euler]


def _world_rot_deg(obj):
    return _deg(obj.matrix_world.to_euler("XYZ"))


def _world_summary(world):
    if world is None:
        return None
    info = {"name": world.name, "generated": is_generated(world), "color": _r(world.color)}
    if world.use_nodes and world.node_tree:
        nodes = world.node_tree.nodes
        info["nodes"] = sorted({n.bl_idname.replace("ShaderNode", "") for n in nodes})[:12]
        for n in nodes:
            if n.bl_idname == "ShaderNodeBackground":
                info.setdefault("background", []).append({
                    "color": _r(n.inputs["Color"].default_value[:3]),
                    "strength": round(n.inputs["Strength"].default_value, 3),
                    "linked": n.inputs["Color"].is_linked,
                })
            elif n.bl_idname == "ShaderNodeTexEnvironment" and n.image:
                info["hdri"] = n.image.name
    return info


def scene_layout(request=None, limit=200):
    """World-space layout of the scene: what Claude needs to light and compose it."""
    scene = bpy.context.scene
    sel = set(selected_names(request))
    objects, lights, cameras = [], [], []
    ordered = sorted(scene.objects, key=lambda o: (o.name not in sel, o.type in {"LIGHT", "CAMERA"}, o.name))
    for obj in ordered:
        loc = obj.matrix_world.translation
        base = {
            "name": obj.name,
            "type": obj.type,
            "world_location": _r(loc),
            "world_rotation_deg": _world_rot_deg(obj),
            "parent": obj.parent.name if obj.parent else None,
            "visible": obj.visible_get(),
        }
        if is_generated(obj):
            base["generated_by_claude2blender"] = role(obj)
        if obj.type == "LIGHT":
            d = obj.data
            item = dict(base, light_type=d.type, energy=round(d.energy, 3), color=_r(d.color))
            if d.type == "AREA":
                item.update(shape=d.shape, size=round(d.size, 4), size_y=round(d.size_y, 4))
            elif d.type in {"POINT", "SPOT"}:
                item["radius"] = round(d.shadow_soft_size, 4)
            elif d.type == "SUN":
                item["angle_deg"] = round(math.degrees(d.angle), 2)
            item["aim"] = _r(obj.matrix_world.to_3x3() @ Vector((0, 0, -1)))
            lights.append(item)
        elif obj.type == "CAMERA":
            d = obj.data
            cameras.append(dict(base, lens_mm=round(d.lens, 2), sensor_mm=round(d.sensor_width, 2),
                                camera_type=d.type, active=scene.camera == obj,
                                aim=_r(obj.matrix_world.to_3x3() @ Vector((0, 0, -1)))))
        elif len(objects) < limit:
            item = dict(base, selected=obj.name in sel)
            if obj.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"}:
                b = bounds([obj])
                item.update(bbox_center=_r(b["center"]), bbox_size=_r(b["size"]))
            item["children"] = len(obj.children)
            objects.append(item)
    meshes = [o for o in scene.objects if o.type == "MESH" and not is_generated(o) and o.visible_get()]
    targets_b = bounds([bpy.data.objects[n] for n in sel if n in bpy.data.objects] or meshes)
    r = scene.render
    vs = scene.view_settings
    return {
        "up_axis": "+Z",
        "unit_note": "Blender units (metres unless unit_scale says otherwise); +Y is away from a front camera",
        "targets_bounds": {k: (_r(v) if hasattr(v, "x") else round(v, 4)) for k, v in targets_b.items()},
        "scene_bounds": {k: (_r(v) if hasattr(v, "x") else round(v, 4)) for k, v in bounds(meshes).items()},
        "objects": objects,
        "objects_listed": len(objects),
        "objects_total": len(scene.objects),
        "lights": lights,
        "cameras": cameras,
        "active_camera": scene.camera.name if scene.camera else None,
        "world": _world_summary(scene.world),
        "render": {
            "engine": r.engine,
            "resolution": [r.resolution_x, r.resolution_y, r.resolution_percentage],
            "film_transparent": r.film_transparent,
            "view_transform": vs.view_transform,
            "look": vs.look,
            "exposure": round(vs.exposure, 3),
        },
    }
