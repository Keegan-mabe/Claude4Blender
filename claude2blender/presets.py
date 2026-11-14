"""Studio material presets, ported from Blender Director's tuned CMF library.

`preset_material(key)` builds (or rebuilds, keeping its name) a Principled BSDF
material for product renders. It is available to staged code next to
`assign_material`, so a preset goes through Review, Preview, Apply and History
like any change from Claude:

    assign_material(preset_material("soft_touch_black"))

Textures are box-projected in object space, so CAD parts need no UVs. Sizes are
real-world millimetres and follow the scene's unit scale.

Texture packs are optional. A preset that names a `pack` looks for that folder
under the Texture Library set in the add-on preferences (searched two levels
deep). When the pack is missing it falls back to procedural grain, so every
preset works on any machine.
"""

import math
import os

import bpy

PREFIX = "C2B "

# Texture packs: folder name -> map file stems (searched with .png/.jpg/.jpeg/.tif/.tiff/.exr).
PACKS = {
    "dry_blast": dict(folder="Poliigon_PlasticMoldDryBlast_7495",
                      rough="Poliigon_PlasticMoldDryBlast_7495_Roughness",
                      height="Poliigon_PlasticMoldDryBlast_7495_Displacement"),
    "worn_plastic": dict(folder="worn-old-plastic-bl",
                         rough="worn-old-plastic_roughness", height="worn-old-plastic_height"),
    "copper_scuffed": dict(folder="Copper-scuffed_bl", rough="Copper-scuffed_roughness",
                           height="Copper-scuffed_roughness", color="Copper-scuffed_basecolor"),
    "steel_brushed": dict(folder="Poliigon_MetalSteelBrushed_7174",
                          rough="Poliigon_MetalSteelBrushed_7174_Roughness",
                          height="Poliigon_MetalSteelBrushed_7174_Displacement",
                          color="Poliigon_MetalSteelBrushed_7174_BaseColor"),
}

# label, description, spec. Colours are linear RGB. rough=(min, max) is the range the
# roughness map is remapped into; tile_mm = real size of one texture repeat.
PRESETS = {
    "plastic_white": ("White ABS (dry blast)", "Off-white injection-moulded plastic, fine mould texture",
                      dict(color=(0.80, 0.79, 0.75), rough=(0.32, 0.62), bump=1.0, grain=0.8, grain_mm=0.22,
                           tile_mm=40, pack="dry_blast")),
    "plastic_black": ("Black ABS", "Matte black injection-moulded plastic",
                      dict(color=(0.012, 0.012, 0.014), rough=(0.30, 0.50), grain=0.5, grain_mm=0.15,
                           tile_mm=40, pack="dry_blast", bump=0.6)),
    "plastic_gloss_black": ("Gloss black plastic", "Piano-black polished plastic with a clear coat",
                            dict(color=(0.01, 0.01, 0.012), rough=(0.06, 0.06), coat=0.6)),
    "plastic_worn_black": ("Vulcanized black", "Near-black worn plastic / vulcanized rubber look, fine scratches",
                           dict(color=(0.006, 0.006, 0.007), rough=(0.26, 0.55), bump=0.25, tile_mm=100,
                                pack="worn_plastic", grain=0.3, grain_mm=0.1)),
    "soft_touch_black": ("Soft-touch black", "Rubberised soft-touch coating over hard plastic",
                         dict(color=(0.012, 0.012, 0.014), rough=(0.72, 0.88), spec=0.25, sheen=0.1,
                              grain=0.6, grain_mm=0.08)),
    "rubber_black": ("Rubber / TPE", "Black TPE pads, feet and grips",
                     dict(color=(0.015, 0.015, 0.016), rough=(0.70, 0.80), spec=0.2, sheen=0.15,
                          grain=0.4, grain_mm=0.05)),
    "rubber_dot_grip": ("Dot-grid rubber", "Soft rubber with a regular grid of small domes (wrist rests, grips)",
                        dict(color=(0.012, 0.012, 0.014), rough=(0.50, 0.60), spec=0.4, sheen=0.1,
                             grain=0.25, grain_mm=0.06, dots=dict(pitch_mm=0.9, height_mm=0.18, edge=0.95))),
    "accent_yellow": ("Yellow soft-touch", "Yellow accent with a soft-touch finish",
                      dict(color=(0.95, 0.52, 0.0), rough=(0.32, 0.55), bump=0.2, spec=0.45, sheen=0.1,
                           tile_mm=100, pack="worn_plastic")),
    "accent_orange": ("Orange accent", "Bright orange plastic accent",
                      dict(color=(0.95, 0.30, 0.02), rough=(0.35, 0.45))),
    "aluminum": ("Bead-blasted aluminium", "Silver anodised, bead-blasted",
                 dict(color=(0.91, 0.91, 0.92), metal=1.0, rough=(0.22, 0.32), grain=0.25, grain_mm=0.05)),
    "aluminum_space_grey": ("Space grey aluminium", "Dark anodised, bead-blasted",
                            dict(color=(0.32, 0.32, 0.34), metal=1.0, rough=(0.26, 0.34), grain=0.25, grain_mm=0.05)),
    "steel_brushed": ("Brushed steel", "Screws, shields and hardware",
                      dict(color=(0.7, 0.7, 0.72), metal=1.0, rough=(0.25, 0.55), bump=0.3, tile_mm=40,
                           pack="steel_brushed")),
    "chrome": ("Chrome", "Mirror-polished chrome",
               dict(color=(0.95, 0.95, 0.96), metal=1.0, rough=(0.03, 0.03))),
    "copper_scuffed": ("Scuffed copper", "Copper pads, traces and inlays",
                       dict(color=(0.9, 0.5, 0.35), metal=1.0, rough=(0.25, 0.55), bump=0.15, tile_mm=60,
                            pack="copper_scuffed")),
    "brass": ("Brass / gold", "Threaded inserts, gold-plated pins",
              dict(color=(0.85, 0.58, 0.16), metal=1.0, rough=(0.28, 0.28))),
    "glass": ("Clear glass", "Clear glass or polished acrylic",
              dict(color=(1.0, 1.0, 1.0), rough=(0.0, 0.0), transmission=1.0, ior=1.5, bevel=False)),
    "glass_frosted": ("Frosted glass", "Frosted glass or acrylic",
                      dict(color=(0.95, 0.96, 0.97), rough=(0.35, 0.35), transmission=1.0, ior=1.5, translucent=0.2)),
    "resin_clear": ("Clear resin (frosted)", "Translucent printed resin that lets internal light through",
                    dict(color=(0.92, 0.93, 0.95), rough=(0.38, 0.38), transmission=1.0, ior=1.45, translucent=0.25)),
    "fabric_grey": ("Grey fabric", "Speaker-grille knit fabric",
                    dict(color=(0.18, 0.18, 0.19), rough=(0.9, 0.9), spec=0.1, sheen=0.2, grain=1.0, grain_mm=0.4)),
    "screen_black": ("Screen glass", "Off display under cover glass",
                     dict(color=(0.003, 0.003, 0.004), rough=(0.05, 0.05), coat=1.0, bevel=False)),
    "pcb_green": ("PCB green", "Solder mask",
                  dict(color=(0.01, 0.12, 0.035), rough=(0.35, 0.35))),
    "pcb_black": ("PCB black", "Black solder mask",
                  dict(color=(0.008, 0.008, 0.01), rough=(0.38, 0.38))),
    "led_blue": ("LED (glowing)", "Emissive LED body",
                 dict(color=(0.9, 0.95, 1.0), rough=(0.4, 0.4), emit=(0.65, 0.82, 1.0), emit_strength=40)),
}

PRESET_ITEMS = [(k, v[0], v[1]) for k, v in PRESETS.items()]
_EXTS = ("", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr")
BEVEL_MM = 0.2   # shader bevel (Cycles): rounds CAD edge highlights without touching the mesh


def library_dir():
    """Texture Library folder from the add-on preferences ('' when unset)."""
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        return bpy.path.abspath(getattr(prefs, "texture_library", "") or "")
    except (KeyError, AttributeError):
        return ""


def _find_folder(root, name):
    if not root or not os.path.isdir(root):
        return None
    direct = os.path.join(root, name)
    if os.path.isdir(direct):
        return direct
    try:
        for sub in os.listdir(root):
            cand = os.path.join(root, sub, name)
            if os.path.isdir(cand):
                return cand
    except OSError:
        pass
    return None


def _find_map(folder, stem):
    """Look for stem in the folder and in any resolution subfolder (2K, 4K, ...)."""
    dirs = [folder] + sorted(os.path.join(folder, d) for d in os.listdir(folder)
                             if os.path.isdir(os.path.join(folder, d)))
    for d in dirs:
        for ext in _EXTS:
            p = os.path.join(d, stem + ext)
            if os.path.isfile(p):
                return p
    return None


def pack_maps(pack_key, root=None):
    """{'rough': path, 'height': path, 'color': path} for an installed pack, else {}."""
    pack = PACKS.get(pack_key)
    folder = _find_folder(root if root is not None else library_dir(), pack["folder"]) if pack else None
    if not folder:
        return {}
    out = {}
    for role in ("rough", "height", "color"):
        if pack.get(role):
            path = _find_map(folder, pack[role])
            if path:
                out[role] = path
    return out


def pack_status(root=None):
    """[(pack key, folder name, installed?)] for the preferences panel."""
    return [(k, p["folder"], bool(pack_maps(k, root))) for k, p in PACKS.items()]


def _image(path, noncolor):
    im = bpy.data.images.load(path, check_existing=True)
    im.colorspace_settings.name = "Non-Color" if noncolor else "sRGB"
    return im


def preset_material(key, name=None, color=None, library=None):
    """Build the preset `key` as a material (rebuilt in place if it exists) and return it.

    name: material name (default 'C2B <label>'). color: optional linear RGB override.
    """
    if key not in PRESETS:
        raise KeyError(f"Unknown preset {key!r}. Presets: {', '.join(PRESETS)}")
    label, _desc, s = PRESETS[key]
    s = dict(s)
    if color is not None:
        s["color"] = tuple(color)[:3]
    name = name or PREFIX + label
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    mat["c2b_preset"] = key
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (600, 0)
    b = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(b.outputs["BSDF"], out.inputs["Surface"])

    def put(sock, val):
        if sock in b.inputs:
            b.inputs[sock].default_value = val

    put("Base Color", (*s["color"], 1))
    put("Metallic", s.get("metal", 0.0))
    put("Coat Weight", s.get("coat", 0.0))
    put("Sheen Weight", s.get("sheen", 0.0))
    put("Specular IOR Level", s.get("spec", 0.5))
    if "transmission" in s:
        put("Transmission Weight", s["transmission"])
    if "ior" in s:
        put("IOR", s["ior"])
    if s.get("emit"):
        put("Emission Color", (*s["emit"], 1))
        put("Emission Strength", s.get("emit_strength", 1.0))
    mat.diffuse_color = (*s["color"], 1)   # Solid-view colour
    rlo, rhi = s.get("rough", (0.5, 0.5))
    put("Roughness", (rlo + rhi) / 2)

    # Object-space mm -> scene units, so sizes stay real whatever the unit scale.
    unit = (bpy.context.scene.unit_settings.scale_length or 1.0) if bpy.context.scene else 1.0
    def per_unit(mm):
        return 1.0 / (mm / 1000.0 / unit)

    tc = nt.nodes.new("ShaderNodeTexCoord")
    normal = None
    if s.get("bevel", True) and BEVEL_MM > 0:
        bevel = nt.nodes.new("ShaderNodeBevel")
        bevel.inputs["Radius"].default_value = BEVEL_MM / 1000.0 / unit
        bevel.samples = 12
        normal = bevel.outputs["Normal"]

    maps = pack_maps(s["pack"], library) if s.get("pack") else {}
    mapping = None
    if maps:
        mapping = nt.nodes.new("ShaderNodeMapping")
        k = per_unit(s.get("tile_mm", 40))
        mapping.inputs["Scale"].default_value = (k, k, k)
        nt.links.new(tc.outputs["Object"], mapping.inputs["Vector"])

    def tex(path, noncolor):
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = _image(path, noncolor)
        n.projection = "BOX"
        n.projection_blend = 0.25
        n.interpolation = "Cubic"
        nt.links.new(mapping.outputs["Vector"], n.inputs["Vector"])
        return n

    if maps.get("rough"):
        rm = nt.nodes.new("ShaderNodeMapRange")
        rm.inputs["To Min"].default_value = rlo
        rm.inputs["To Max"].default_value = rhi
        nt.links.new(tex(maps["rough"], True).outputs["Color"], rm.inputs["Value"])
        nt.links.new(rm.outputs["Result"], b.inputs["Roughness"])
    if maps.get("color"):
        nt.links.new(tex(maps["color"], False).outputs["Color"], b.inputs["Base Color"])

    if s.get("grain"):
        nz = nt.nodes.new("ShaderNodeTexNoise")
        nz.noise_dimensions = "3D"
        nz.inputs["Scale"].default_value = per_unit(s.get("grain_mm", 0.15))
        nz.inputs["Detail"].default_value = 3.0
        nz.inputs["Roughness"].default_value = 0.7
        nt.links.new(tc.outputs["Object"], nz.inputs["Vector"])
        gb = nt.nodes.new("ShaderNodeBump")
        gb.inputs["Strength"].default_value = s["grain"]
        gb.inputs["Distance"].default_value = 0.0002 / unit
        nt.links.new(nz.outputs["Fac"], gb.inputs["Height"])
        if normal is not None:
            nt.links.new(normal, gb.inputs["Normal"])
        normal = gb.outputs["Normal"]

    if s.get("dots"):   # grid of domes from object-space X/Z
        d = s["dots"]
        k = 2 * math.pi * per_unit(d["pitch_mm"])
        sep = nt.nodes.new("ShaderNodeSeparateXYZ")
        nt.links.new(tc.outputs["Object"], sep.inputs["Vector"])

        def cosn(axis):
            m = nt.nodes.new("ShaderNodeMath"); m.operation = "MULTIPLY"; m.inputs[1].default_value = k
            nt.links.new(sep.outputs[axis], m.inputs[0])
            c = nt.nodes.new("ShaderNodeMath"); c.operation = "COSINE"
            nt.links.new(m.outputs[0], c.inputs[0])
            return c

        ad = nt.nodes.new("ShaderNodeMath"); ad.operation = "ADD"
        nt.links.new(cosn("X").outputs[0], ad.inputs[0])
        nt.links.new(cosn("Z").outputs[0], ad.inputs[1])
        mr = nt.nodes.new("ShaderNodeMapRange"); mr.interpolation_type = "SMOOTHSTEP"
        mr.inputs["From Min"].default_value = d.get("edge", 1.25)
        mr.inputs["From Max"].default_value = 2.0
        nt.links.new(ad.outputs[0], mr.inputs["Value"])
        db = nt.nodes.new("ShaderNodeBump")
        db.inputs["Distance"].default_value = d["height_mm"] / 1000.0 / unit
        nt.links.new(mr.outputs["Result"], db.inputs["Height"])
        if normal is not None:
            nt.links.new(normal, db.inputs["Normal"])
        normal = db.outputs["Normal"]

    if maps.get("height"):
        bm = nt.nodes.new("ShaderNodeBump")
        bm.inputs["Strength"].default_value = s.get("bump", 0.3)
        bm.inputs["Distance"].default_value = 0.004 / unit
        nt.links.new(tex(maps["height"], True).outputs["Color"], bm.inputs["Height"])
        if normal is not None:
            nt.links.new(normal, bm.inputs["Normal"])
        normal = bm.outputs["Normal"]

    if normal is not None:
        nt.links.new(normal, b.inputs["Normal"])

    if s.get("translucent"):   # frosted: mix in diffuse transmission so light from inside shows through
        tr = nt.nodes.new("ShaderNodeBsdfTranslucent")
        tr.inputs["Color"].default_value = (*s["color"], 1)
        mx = nt.nodes.new("ShaderNodeMixShader")
        mx.inputs["Fac"].default_value = s["translucent"]
        nt.links.new(b.outputs["BSDF"], mx.inputs[1])
        nt.links.new(tr.outputs["BSDF"], mx.inputs[2])
        nt.links.new(mx.outputs["Shader"], out.inputs["Surface"])

    mat["c2b_textured"] = bool(maps)
    return mat


def preset_code(key):
    """Staged code for applying a preset to the current targets."""
    label = PRESETS[key][0]
    return (f"# Studio preset: {label}\n"
            f"mat = preset_material({key!r})\n"
            f"assign_material(mat)\n")


def prompt_list():
    """One line per preset, for Claude's system prompt."""
    return "\n".join(f"  {k}: {v[0]} ({v[1]})" for k, v in PRESETS.items())
