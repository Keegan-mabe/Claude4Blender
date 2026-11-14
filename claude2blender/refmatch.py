"""Measured lighting match (ported from Blender Director's refmatch, "basis NNLS v1").

Instead of guessing light intensities from a photo, measure them:

1. Basis render (background Blender, on a temporary copy of your scene): your camera and product
   under 25 soft dome lights (3 rings of 8 + overhead) plus a neutral world, each in its own light
   group, with an object-index mask of the targets and a diffuse-colour pass. Your own lights are
   switched off in the copy only.
2. Fit: any lighting made from those lights is a weighted sum of the passes, so per colour channel a
   non-negative least-squares fit finds the weight of each light that best reproduces the
   reference's shading pattern (product 3x3 grid, left/right/top rim bands, background 3x3 grid).
   The basis is divided by our own surface colour, so a black reference product doesn't just dim
   every light. Exposure puts the product near mid-grey.
3. Result: staged code that builds the lights that matter ("C2B Match L07" ...) as real area lights
   with the solved energy and tint, plus a studio world for the background. Review / Preview /
   Apply / Undo like any change.

It matches lighting direction, balance and colour - not camera angle, props or backdrop geometry.
No match percentage is claimed: the residuals (lower is closer) are reported for you to judge.
"""

import json
import math
import os
import subprocess
import sys
import tempfile
import threading

import bpy

W_PRODUCT, W_RIM, W_BG = 1.0, 1.6, 0.5
# Director used 3x3 + 3 rims + 3x3 = 15 regions for 25 lights: underdetermined, so very different
# lightings fit equally well. 6x6 + 3 rims + 4x4 = 55 regions pins the light direction down.
PRODUCT_GRID, BG_GRID = 6, 4
RIDGE = 1e-4
MATCH_PREFIX = "C2B Match "
RINGS = (12, 38, 66)        # dome elevations in degrees, 8 lights each, plus one overhead
DIST_R, SIZE_R = 5.5, 3.0   # light distance and size in product radii (Director's tuned dome)
BASE_W_AT_HALF_M = 9.0      # basis light watts at 0.5 m (Director)
MIN_SHARE = 0.03            # lights below this share of the strongest one are left out

# ---------------------------------------------------------------- 1. basis render (runs in background Blender)

BASIS_SCRIPT = r'''
import bpy, json, math, sys
from mathutils import Vector
args = json.loads(sys.argv[sys.argv.index("--") + 1])
sc = bpy.context.scene
vl = bpy.context.view_layer
targets = [bpy.data.objects[n] for n in args["targets"] if n in bpy.data.objects]
for o in sc.objects:                                   # copy only: your own lights off
    if o.type == "LIGHT":
        o.hide_render = True
    o.pass_index = 0
for o in targets:
    o.pass_index = 1
    for ch in o.children_recursive:
        ch.pass_index = 1
pts = [o.matrix_world @ Vector(c) for o in targets for c in o.bound_box] or [Vector()]
lo = Vector([min(p[i] for p in pts) for i in range(3)]); hi = Vector([max(p[i] for p in pts) for i in range(3)])
ctr = (lo + hi) / 2; rad = max((hi - lo).length / 2, 1e-4)
unit = sc.unit_settings.scale_length or 1.0
cam = sc.camera
fwd = Vector((0, 1, 0))
if cam is not None:
    d = ctr - cam.matrix_world.translation; d.z = 0
    if d.length > 1e-6: fwd = d.normalized()
az0 = math.atan2(-fwd.y, -fwd.x)                       # azimuth 0 = toward the camera
dist, size = rad * %(DIST_R)r, rad * %(SIZE_R)r
energy = %(BASE_W)r * (dist * unit / 0.5) ** 2
lights = []
def add(name, elev, az):
    e = math.radians(elev); a = az0 + math.radians(az)
    loc = ctr + Vector((math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e))) * dist
    ld = bpy.data.lights.new(name, "AREA"); ld.size = size; ld.energy = energy
    ob = bpy.data.objects.new(name, ld); sc.collection.objects.link(ob)
    ob.location = loc; ob.rotation_euler = (ctr - loc).to_track_quat("-Z", "Y").to_euler()
    vl.lightgroups.add(name=name); ob.lightgroup = name
    lights.append({"name": name, "location": list(loc), "elevation": elev, "azimuth": az})
k = 0
for elev in %(RINGS)r:
    for az in range(0, 360, 45):
        add("L%%02d" %% k, elev, az); k += 1
add("L%%02d" %% k, 90, 0)
w = bpy.data.worlds.new("basis_world"); w.use_nodes = True
w.node_tree.nodes["Background"].inputs["Color"].default_value = (1, 1, 1, 1)
w.node_tree.nodes["Background"].inputs["Strength"].default_value = 1.0
sc.world = w
vl.lightgroups.add(name="world"); w.lightgroup = "world"
vl.use_pass_object_index = True
vl.use_pass_diffuse_color = True
sc.render.engine = "CYCLES"
sc.cycles.samples = args["samples"]
sc.cycles.use_denoising = True
try:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for kind in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
        try:
            prefs.compute_device_type = kind; prefs.get_devices()
            if any(d.type == kind for d in prefs.devices):
                for d in prefs.devices: d.use = True
                sc.cycles.device = "GPU"; break
        except Exception:
            pass
except Exception:
    pass
r = sc.render
aspect = (r.resolution_y * r.pixel_aspect_y) / max(1, r.resolution_x * r.pixel_aspect_x)
r.resolution_x = args["width"]; r.resolution_y = max(16, int(args["width"] * aspect)); r.resolution_percentage = 100
r.film_transparent = False
sc.view_settings.view_transform = "Standard"; sc.view_settings.look = "None"; sc.view_settings.exposure = 0
im = r.image_settings
try:
    im.media_type = "MULTI_LAYER_IMAGE"
except Exception:
    pass
im.file_format = "OPEN_EXR_MULTILAYER" if "OPEN_EXR_MULTILAYER" in [i.identifier for i in im.bl_rna.properties["file_format"].enum_items] else "OPEN_EXR"
im.color_depth = "32"
r.filepath = args["out"]
bpy.ops.render.render(write_still=True)
json.dump({"center": list(ctr), "radius": rad, "unit": unit, "size": size, "energy": energy,
           "lights": lights, "view_layer": vl.name}, open(args["out"] + ".json", "w"))
print("C2B_BASIS_OK")
''' % {"DIST_R": DIST_R, "SIZE_R": SIZE_R, "BASE_W": BASE_W_AT_HALF_M, "RINGS": RINGS}


def basis_command(blend_copy, out_exr, targets, samples=96, width=640):
    args = json.dumps({"out": out_exr, "targets": list(targets), "samples": int(samples), "width": int(width)})
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return [bpy.app.binary_path, "-b", "--factory-startup", blend_copy, "--python-expr", BASIS_SCRIPT,
            "--", args], kw


# ---------------------------------------------------------------- 2. the fit (numpy only, thread-safe)

def _np():
    import numpy as np
    return np


def srgb_to_linear(c):
    np = _np()
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def read_image(path):
    """(h, w, 3) linear float image, top row first. 8-bit images are linearised from sRGB."""
    import OpenImageIO as oiio
    np = _np()
    inp = oiio.ImageInput.open(path)
    if inp is None:
        raise RuntimeError(f"Could not open {path}: {oiio.geterror()}")
    spec = inp.spec()
    px = inp.read_image(0, 0, 0, spec.nchannels, oiio.FLOAT)
    inp.close()
    px = np.asarray(px, dtype=np.float32).reshape(spec.height, spec.width, spec.nchannels)
    if spec.nchannels == 1:
        px = np.repeat(px, 3, axis=2)
    px = px[:, :, :3]
    is_float = spec.format.basetype in (oiio.FLOAT, oiio.HALF, oiio.DOUBLE)
    return px if is_float else srgb_to_linear(px)


def read_basis(path):
    """{group: (h,w,3)}, product mask (h,w), albedo (h,w,3) or None from the multilayer basis EXR."""
    import OpenImageIO as oiio
    np = _np()
    inp = oiio.ImageInput.open(path)
    if inp is None:
        raise RuntimeError(f"Could not open the basis render: {oiio.geterror()}")
    chans = {}
    sub = 0
    while inp.seek_subimage(sub, 0):
        spec = inp.spec()
        px = np.asarray(inp.read_image(sub, 0, 0, spec.nchannels, oiio.FLOAT), dtype=np.float32)
        px = px.reshape(spec.height, spec.width, spec.nchannels)
        for i, name in enumerate(spec.channelnames):
            chans[name] = px[:, :, i]
        sub += 1
    inp.close()

    def rgb(base):
        return np.stack([chans[base + ".R"], chans[base + ".G"], chans[base + ".B"]], axis=-1)

    groups = {}
    for k in chans:
        if ".Combined_" in k and k.endswith(".R"):
            groups[k.split(".Combined_")[1][:-2]] = rgb(k[:-2])
    idx = next((v for k, v in chans.items() if "IndexOB" in k or "Object Index" in k), None)
    if idx is None or not groups:
        raise RuntimeError("The basis render has no light groups or object-index pass")
    dc = next((k for k in chans if (".DiffCol." in k or ".Diffuse Color." in k) and k.endswith(".R")), None)
    albedo = rgb(dc[:-2]) if dc else None
    return groups, idx > 0.5, albedo


def _fill_spans(m):
    """Count a pixel as product when product pixels enclose it on both its row and its column.

    A shadowed face can be as dark as the background and drop out of the colour mask, which makes
    the product look evenly lit and pulls the fit toward flat front light.
    """
    np = _np()
    h, w = m.shape
    idx_x = np.arange(w)[None, :]
    idx_y = np.arange(h)[:, None]
    any_r, any_c = m.any(axis=1), m.any(axis=0)
    left = np.where(any_r, m.argmax(axis=1), w)[:, None]
    right = np.where(any_r, w - 1 - m[:, ::-1].argmax(axis=1), -1)[:, None]
    top = np.where(any_c, m.argmax(axis=0), h)[None, :]
    bottom = np.where(any_c, h - 1 - m[::-1, :].argmax(axis=0), -1)[None, :]
    return m | ((idx_x >= left) & (idx_x <= right) & (idx_y >= top) & (idx_y <= bottom))


def reference_mask(img, bbox=None):
    return _fill_spans(_reference_mask(img, bbox))


def _reference_mask(img, bbox=None):
    np = _np()
    h, w, _ = img.shape
    if bbox:
        x0, y0, x1, y1 = bbox
        Y0, Y1, X0, X1 = int(y0 * h), int(y1 * h), int(x0 * w), int(x1 * w)
        box = np.zeros((h, w), bool)
        box[Y0:Y1, X0:X1] = True
        pad = int(0.06 * max(h, w))
        ring = np.zeros((h, w), bool)
        ring[max(0, Y0 - pad):min(h, Y1 + pad), max(0, X0 - pad):min(w, X1 + pad)] = True
        ring &= ~box
        g = np.clip(img, 0, None) ** (1 / 2.2)
        samples = g[ring]
        if len(samples) > 800:
            samples = samples[np.random.default_rng(0).choice(len(samples), 800, replace=False)]
        pix = g[box]
        dmin = np.full(len(pix), np.inf, dtype=np.float32)
        for i in range(0, len(samples), 100):
            d = np.linalg.norm(pix[:, None, :] - samples[None, i:i + 100, :], axis=-1).min(axis=1)
            dmin = np.minimum(dmin, d)
        m = np.zeros((h, w), bool)
        m[box] = dmin > 0.07
        return m
    border = np.concatenate([img[0], img[-1], img[:, 0], img[:, -1]])
    med = np.median(border, axis=0)
    spread = np.percentile(np.linalg.norm(border - med, axis=-1), 95)
    d = np.linalg.norm(img - med, axis=-1)
    m = d > max(0.05, spread * 2.0)
    rows = np.where(m.mean(axis=1) > 0.02)[0]
    cols = np.where(m.mean(axis=0) > 0.02)[0]
    core = np.zeros_like(m)
    if len(rows) and len(cols):
        core[rows.min():rows.max() + 1, cols.min():cols.max() + 1] = True
    return m & core


def _erode(m, r):
    np = _np()
    out = m.copy()
    for dy in range(-r, r + 1, max(1, r // 2)):
        for dx in range(-r, r + 1, max(1, r // 2)):
            out &= np.roll(np.roll(m, dy, 0), dx, 1)
    return out


def regions(mask):
    np = _np()
    h, w = mask.shape
    ys, xs = np.where(mask)
    if not len(ys):
        raise RuntimeError("No product found in the image")
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    yy, xx = np.mgrid[0:h, 0:w]
    out = []
    n = PRODUCT_GRID
    for i in range(n):
        for j in range(n):
            cell = ((yy >= y0 + (y1 - y0) * i // n) & (yy < y0 + (y1 - y0) * (i + 1) // n)
                    & (xx >= x0 + (x1 - x0) * j // n) & (xx < x0 + (x1 - x0) * (j + 1) // n))
            out.append((f"prod_{i}_{j}", cell & mask, W_PRODUCT))
    r = max(2, int((x1 - x0) * 0.02))
    edge = mask & ~_erode(mask, r)
    cx = (x0 + x1) / 2
    out.append(("rim_left", edge & (xx < x0 + (x1 - x0) * 0.33), W_RIM))
    out.append(("rim_right", edge & (xx > x0 + (x1 - x0) * 0.67), W_RIM))
    out.append(("rim_top", edge & (yy < y0 + (y1 - y0) * 0.35) & (np.abs(xx - cx) < (x1 - x0) * 0.4), W_RIM))
    bg = ~mask
    n = BG_GRID
    for i in range(n):
        for j in range(n):
            cell = (yy >= h * i // n) & (yy < h * (i + 1) // n) & (xx >= w * j // n) & (xx < w * (j + 1) // n)
            out.append((f"bg_{i}_{j}", cell & bg, W_BG))
    return out


def features(img, regs):
    np = _np()
    pm = np.zeros(img.shape[:2], bool)
    for name, m, _ in regs:
        if name.startswith("prod"):
            pm |= m
    prod = img[pm].mean(axis=0) if pm.any() else np.zeros(3)
    return np.array([img[m].mean(axis=0) if m.sum() > 8 else prod for _, m, _ in regs])


def nnls_cd(A, b, ridge=RIDGE, sweeps=2000):
    """min ||A w - b||^2 + ridge ||w||^2 with w >= 0, by coordinate descent."""
    np = _np()
    n = A.shape[1]
    w = np.zeros(n)
    r = b - A @ w
    col2 = (A * A).sum(axis=0) + ridge
    for _ in range(sweeps):
        delta = 0.0
        for j in range(n):
            old = w[j]
            new = max(0.0, old + (A[:, j] @ r - ridge * old) / col2[j])
            if new != old:
                r -= A[:, j] * (new - old)
                w[j] = new
                delta = max(delta, abs(new - old))
        if delta < 1e-7:
            break
    return w


def _resize_to(img, h, w):
    """Nearest-neighbour resample (region means don't need more)."""
    np = _np()
    ys = (np.arange(h) * img.shape[0] / h).astype(int)
    xs = (np.arange(w) * img.shape[1] / w).astype(int)
    return img[ys][:, xs]


def solve(basis_exr, reference, bbox=None):
    """Fit the basis to the reference. Returns a result dict (lights, world, background, residuals)."""
    np = _np()
    meta = json.load(open(basis_exr + ".json"))
    groups, bmask, albedo = read_basis(basis_exr)
    names = sorted(g for g in groups if g.startswith("L"))
    shade = {g: (groups[g] / np.maximum(albedo, 0.02)) if albedo is not None else groups[g] for g in names}
    ref = read_image(reference)
    rmask = reference_mask(ref, bbox)
    if rmask.sum() < 50 and bbox is None:
        # Uneven sets (floor gradients, hot spots) defeat the border-colour mask: retry with the product
        # assumed in the middle of the frame, background sampled around it.
        rmask = reference_mask(ref, (0.12, 0.08, 0.88, 0.92))
    if rmask.sum() < 50:
        raise RuntimeError("Could not find the product in the reference. Try a reference with a plain background")
    bregs, rregs = regions(bmask), regions(rmask)
    weights = np.array([wt for _, _, wt in bregs])
    F = np.stack([features(shade[g], bregs) for g in names], axis=-1)
    Fraw = np.stack([features(groups[g], bregs) for g in names], axis=-1)
    R = features(ref, rregs).copy()
    R_abs = R.copy()
    prod_rows = [i for i, (n, _, _) in enumerate(bregs) if n.startswith("prod")]
    obj_rows = [i for i, (n, _, _) in enumerate(bregs) if not n.startswith("bg")]
    bg_rows = [i for i, (n, _, _) in enumerate(bregs) if n.startswith("bg")]
    all_on = F.sum(axis=-1)
    R[obj_rows] *= all_on[prod_rows].mean() / max(R[prod_rows].mean(), 1e-6)
    R[bg_rows] *= all_on[bg_rows].mean() / max(R[bg_rows].mean(), 1e-6)

    Wt = np.sqrt(weights)[:, None]
    wrgb = np.zeros((len(names), 3))
    for c in range(3):
        wrgb[:, c] = nnls_cd(F[:, c, :] * Wt, R[:, c] * Wt[:, 0])
    fit = np.einsum("rcl,lc->rc", F, wrgb)
    residuals = {}
    for grp in ("prod", "rim", "bg"):
        rows = [i for i, (n, _, _) in enumerate(bregs) if n.startswith(grp)]
        residuals[grp] = round(float(np.linalg.norm(fit[rows] - R[rows]) / max(np.linalg.norm(R[rows]), 1e-9)), 3)

    pred = np.einsum("rcl,lc->rc", Fraw, wrgb)
    world = groups.get("world")
    Fw = features(world, bregs) if world is not None else None
    ww = np.zeros(3)
    total = 0.0
    for _ in range(3):
        prod_lin = pred[prod_rows].mean() + ((Fw[prod_rows] * ww).mean() if Fw is not None else 0.0)
        total = math.log2(0.22 / max(float(prod_lin), 1e-6))
        if Fw is None:
            break
        target = R_abs[bg_rows].mean(axis=0) / (2 ** total)
        ww = np.clip((target - pred[bg_rows].mean(axis=0)) / np.maximum(Fw[bg_rows].mean(axis=0), 1e-6), 0, 4.0)
    scale = 2 ** total                      # exposure folded into the lights: your colour management is untouched

    by_name = {l["name"]: l for l in meta["lights"]}
    peak = float(wrgb.max()) or 1.0
    lights = []
    for g, w in zip(names, wrgb):
        k = float(w.max())
        if k < MIN_SHARE * peak:
            continue
        info = by_name[g]
        lights.append({"name": g, "location": info["location"], "elevation": info["elevation"],
                       "azimuth": info["azimuth"], "energy": round(meta["energy"] * k * scale, 4),
                       "color": [round(float(x / k), 4) for x in w], "share": round(k / peak, 3)})
    lights.sort(key=lambda l: -l["share"])
    top_rows = [i for i, (n, _, _) in enumerate(bregs) if n.startswith("bg_0_")]
    bot_rows = [i for i, (n, _, _) in enumerate(bregs) if n.startswith(f"bg_{BG_GRID - 1}_")]
    wk = float(ww.max())
    return {
        "center": meta["center"], "radius": meta["radius"], "size": meta["size"],
        "lights": lights, "lights_total": len(names),
        "world": {"strength": round(wk * scale, 4), "color": [round(float(x / wk), 4) if wk > 0 else 1.0 for x in ww]},
        "background": {"top": [round(float(x), 4) for x in R_abs[top_rows].mean(axis=0)],
                       "bottom": [round(float(x), 4) for x in R_abs[bot_rows].mean(axis=0)]},
        "residuals": residuals,
        "reference": reference,
    }


# ---------------------------------------------------------------- 3. the staged change

def _side(az):
    az %= 360
    names = {0: "front", 45: "front right", 90: "right", 135: "back right", 180: "back",
             225: "back left", 270: "left", 315: "front left"}
    return names.get(int(round(az / 45.0)) * 45 % 360, f"{az:.0f} deg")


def describe(result):
    parts = []
    for l in result["lights"][:3]:
        where = "overhead" if l["elevation"] >= 90 else f"{_side(l['azimuth'])}, {'high' if l['elevation'] > 50 else 'mid' if l['elevation'] > 25 else 'low'}"
        parts.append(where)
    r = result["residuals"]
    return (f"{len(result['lights'])} lights; strongest: " + "; ".join(parts)
            + f". Residual product {r['prod']:.2f}, rims {r['rim']:.2f}, background {r['bg']:.2f} (lower is closer)")


def match_code(result):
    """Python staged for Review / Preview / Apply."""
    lines = ["# Measured lighting match (basis NNLS) from " + os.path.basename(result["reference"]),
             "# Residuals (lower is closer): " + json.dumps(result["residuals"]),
             f"center = {tuple(round(v, 5) for v in result['center'])}",
             "used = set()"]
    for l in result["lights"]:
        name = MATCH_PREFIX + l["name"]
        loc = tuple(round(v, 5) for v in l["location"])
        lines.append(f"used.add({name!r}); c2b.light({name!r}, 'AREA', location={loc}, target=center, "
                     f"energy={l['energy']}, color={tuple(l['color'])}, size={round(result['size'], 5)})")
    lines += [
        "for o in bpy.data.objects:   # earlier match lights this one doesn't use are hidden, never deleted",
        f"    if o.type == 'LIGHT' and o.name.startswith({MATCH_PREFIX!r}) and o.name not in used:",
        "        c2b.retire(o)",
    ]
    bg, wd = result["background"], result["world"]
    cam_col = tuple(round((a + b) / 2, 4) for a, b in zip(bg["top"], bg["bottom"]))
    lines.append(f"c2b.studio_world(top={tuple(wd['color'])}, strength={wd['strength']}, "
                 f"camera_color={cam_col}, camera_strength=1.0)")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- running it from the panel

class MatchJob:
    """Basis render in a background Blender, then the fit, on a worker thread. Poll `done`."""

    def __init__(self, blend_copy, workdir, reference, targets, samples=96, width=640, bbox=None):
        self.blend_copy, self.workdir, self.reference = blend_copy, workdir, reference
        self.targets, self.samples, self.width, self.bbox = targets, samples, width, bbox
        self.status = "Rendering the light basis…"
        self.result = None
        self.error = None
        self.done = False
        self.proc = None
        self.cancelled = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _run(self):
        try:
            exr = os.path.join(self.workdir, "basis.exr")
            cmd, kw = basis_command(self.blend_copy, exr, self.targets, self.samples, self.width)
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, **kw)
            out, _ = self.proc.communicate(timeout=1800)
            if self.cancelled:
                raise RuntimeError("Cancelled")
            if "C2B_BASIS_OK" not in out or not os.path.isfile(exr):
                tail = "\n".join(l for l in out.splitlines() if "Error" in l or "Traceback" in l)[-400:]
                raise RuntimeError("The basis render failed. " + tail)
            self.status = "Fitting lights to the reference…"
            self.result = solve(exr, self.reference, self.bbox)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
        finally:
            self.done = True


def start_match(context, reference, targets, samples=96, width=640, bbox=None):
    """Save a temporary copy of the scene and start a MatchJob on it. Your file is not changed."""
    if context.scene.camera is None:
        raise RuntimeError("Add a camera first: the match is measured through the scene camera")
    workdir = tempfile.mkdtemp(prefix="c2b_match_")
    copy = os.path.join(workdir, "scene_copy.blend")
    bpy.ops.wm.save_as_mainfile(filepath=copy, copy=True, check_existing=False)
    return MatchJob(copy, workdir, reference, targets, samples, width, bbox).start()
