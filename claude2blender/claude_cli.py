"""Generate with Claude: runs your own Claude Code CLI (`claude -p`) in the background.

* Uses the official, unmodified `claude` command you installed and signed in to.
  This module never reads, stores or passes authentication tokens; Claude Code
  handles sign-in itself.
* Claude gets only one tool, Read, limited to the reference image folders, so it
  can look at your images. MCP servers are switched off for these runs
  (--strict-mcp-config), so it cannot reach Blender or run anything.
* The answer comes back as structured JSON (--json-schema). Code is never run:
  it is staged in Review & Apply like any other proposal.
* The process runs on a worker thread; a bpy timer moves its events to the UI,
  so Blender stays responsive. Follow-ups resume the same Claude Code session.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time

import bpy

from . import bridge, history, materials, presets
from .properties import get_state

READABLE_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One short line naming the change, e.g. 'Brushed aluminium on Body'"},
        "code": {"type": "string", "description": "Blender Python to stage for review, or empty if you need more information"},
        "message": {"type": "string", "description": "Technical explanation for the Details section: what you saw, which nodes, assumptions"},
        "material": {
            "type": "object",
            "description": "Summary of the main material you produced. Use empty strings / -1 for non-material changes.",
            "properties": {
                "name": {"type": "string"},
                "base_color_hex": {"type": "string", "description": "sRGB hex of the perceived base colour, e.g. #1A1A1C"},
                "roughness": {"type": "number", "description": "0-1, or -1 if not applicable"},
                "metallic": {"type": "number", "description": "0-1, or -1 if not applicable"},
                "surface": {"type": "string", "description": "Very short: grain/bump/texture, e.g. 'fine bead-blast grain, 0.2 mm'"},
                "match": {"type": "string", "enum": ["close", "approximate", "needs_texture_maps", "n/a"]},
                "limitations": {"type": "string", "description": "One short sentence on what a procedural match cannot reproduce, or empty"},
            },
            "required": ["name", "base_color_hex", "roughness", "metallic", "surface", "match", "limitations"],
            "additionalProperties": False,
        },
        "scene": {
            "type": "object",
            "description": ("Environment/Composition summary, one short phrase per field naming what you set; "
                            "empty string for anything you didn't change (and all empty in Material mode)."),
            "properties": {
                "lighting": {"type": "string", "description": "e.g. 'Large soft key upper left, 3:1 fill, cool rim'"},
                "world": {"type": "string", "description": "e.g. 'Dark grey gradient, 0.3 strength'"},
                "backdrop": {"type": "string", "description": "e.g. 'Light grey sweep, matte'"},
                "camera": {"type": "string", "description": "e.g. '85 mm, 25° above, 3/4 left'"},
                "objects": {"type": "string", "description": "e.g. 'Exploded along Z, 40 mm gaps'"},
                "render": {"type": "string", "description": "e.g. 'AgX, Medium High Contrast, -0.3 EV'"},
                "match": {"type": "string", "enum": ["close", "approximate", "needs_hdri_or_textures", "n/a"]},
                "limitations": {"type": "string", "description": "One short sentence on what can't be matched exactly, or empty"},
            },
            "required": ["lighting", "world", "backdrop", "camera", "objects", "render", "match", "limitations"],
            "additionalProperties": False,
        },
    },
    "required": ["summary", "code", "message", "material", "scene"],
    "additionalProperties": False,
}

COMMON_PROMPT = """You are Claude2Blender, a look-development assistant inside Blender's sidebar, used for
photorealistic product renders of CAD models. You cannot see or change the scene yourself. You get a JSON
description of the request, selection and scene, plus reference images you can view with the Read tool. You
answer with Blender Python that the user previews and applies.

How the code runs:
- In Object Mode, with `bpy`, `request` (the JSON you were given, as a dict), `expand_indices("0-3,7")`,
  `assign_material(mat)` and `c2b` (scene helpers, below) available.
- Make code idempotent: look things up by name and update them (`bpy.data.materials.get(NAME) or ...new(NAME)`),
  so applying a refinement never duplicates materials, lights, cameras or backdrops. Keep names stable across
  refinements.
- Prefer bpy.data over bpy.ops. No file writes, network or subprocesses. Keep it readable and commented.
- Never edit vertices, edges, faces, UVs, modifiers, object scale or dimensions when preserve_geometry is true.
- Never delete existing objects, lights, cameras, worlds or materials.

The answer has `material` and `scene` summaries: fill the one for this mode and leave the other with empty
strings / -1 / "n/a".

Refinements (follow-ups): change only what the user asks and keep every other value exactly as it is. The
request shows the current state (target_materials, scene.layout) after previous changes.

If the request is unclear or impossible within the rules, return empty code and ask briefly in message."""

MATERIAL_PROMPT = """Material mode:
- ALWAYS put materials on the targets with `assign_material(mat)`. It applies to exactly the selected objects
  (Object Mode) or exactly the selected faces (Edit Mode), and leaves every other face and object unchanged.
  Do not assign materials any other way.
- `mat = bpy.data.materials.get(NAME) or bpy.data.materials.new(NAME)`, then `mat.use_nodes = True`, clear and
  rebuild the node tree, then `assign_material(mat)`. Give the material a clear, specific name (e.g.
  "Anodised Aluminium - Graphite").

Material matching from photographs (when there are reference images):
- View every reference with Read. Analyse: base colour (compensate for lighting and white balance; Base Color
  is linear RGB, convert from sRGB), roughness and how it varies, metallic vs dielectric, specular/IOR, coat,
  sheen, transmission or subsurface if visible, anisotropy for brushed metals, surface grain or bump scale,
  and any visible pattern (wood grain, weave, speckle, knurling).
- Build a physically plausible Principled BSDF setup for product rendering. Reproduce micro-surface detail with
  procedural textures (Noise, Voronoi, Wave, Musgrave/Noise FBM) driving Bump and roughness variation, sized
  in real-world units: scale from the object's dimensions, and use Object or Generated coordinates with a
  Mapping node (CAD meshes often have no usable UVs).
- Only use the photo itself as an Image Texture when a visible pattern can't be approximated procedurally,
  and then use Box projection with Object coordinates. Load it with bpy.data.images.load(path, check_existing=True).
- Be honest about the match: set material.match to "needs_texture_maps" when an exact photographic match
  (e.g. a specific wood figure, print, or logo) would need scanned/PBR texture maps, and say so briefly in
  material.limitations. Never claim a perfect match.
- Refinements: `target_materials` holds the current node trees.

Studio presets: `preset_material(key, name=None, color=None)` returns a tuned, ready-to-render material
(rebuilt in place by name; uses the user's texture packs when installed, procedural grain otherwise). When the
request clearly matches one (e.g. "soft-touch black", "brushed steel screws", "PCB copper"), start from it:
`assign_material(preset_material("soft_touch_black", color=(...)))`, or build on its node tree. Keys:
""" + presets.prompt_list()

SCENE_HELPERS_PROMPT = """Scene helpers (`c2b`), all in world space, metres, +Z up, angles in radians:
- c2b.light(name, kind="AREA"|"SPOT"|"POINT"|"SUN", location=(x,y,z), target=point_or_object, energy=W,
  color="#RRGGBB", size=m, size_y=m, shape="RECTANGLE"|"DISK"|"ELLIPSE"|"SQUARE", spread=rad,
  spot_size=rad, spot_blend=0-1, angle=rad (sun)) -> creates or updates that light (no duplicates).
- c2b.studio_style("dark"|"highkey"|"gradient", strength=1.0) -> a tuned studio look in one call (lights named
  "C2B Style ...", world, floor/sweep, and for gradient a glowing wall), placed around the targets relative to
  the scene camera. A good starting point when the request names a look like these; then refine with c2b.light.
- Lights named "C2B Match L00".."L24" come from the panel's measured lighting match (fitted to the reference
  with a light basis). When the user refines a measured match ("softer", "warmer key"), adjust those lights
  by name rather than replacing them.
- c2b.studio_world(top="#hex", bottom="#hex", strength=1.0, camera_color=None|"#hex", camera_strength=1.0,
  horizon=0.5, name="C2B Studio World") -> procedural gradient world, assigned to the scene. camera_color lets
  the visible background differ from the light the world gives.
- c2b.backdrop(name="C2B Backdrop", kind="sweep"|"floor", width, depth, height, curve, location, color="#hex",
  roughness) -> infinity-cove sweep or floor under the targets, matte material; refinements resize it.
- c2b.exposure(value=EV, look="Medium High Contrast", view_transform="AgX"|"Filmic"|"Standard",
  film_transparent=bool)
- c2b.camera(name="C2B Camera", location, target, lens=mm, sensor_width=mm, make_active=True, shift_x, shift_y,
  dof_target=obj_or_distance, fstop, use_existing=camera_obj) -> creates/updates a camera. To adjust the user's
  existing camera pass use_existing=bpy.data.objects[NAME]. Never changes the output resolution.
- c2b.frame(camera, objects, direction=(x,y,z) from target to camera, margin=1.15) -> fits objects in frame.
- c2b.place(obj, location=(x,y,z), rotation=Euler/Matrix/Quaternion) -> world-space move/rotate that respects
  parents; never touches scale or parenting. c2b.look_at(obj, target, roll=0).
- c2b.explode(objects, axis=(0,0,1), spacing=m, center=None, order=None) -> spreads parts along an axis around
  their middle, keeping their order. Pass top-level parts; children follow their parents.
- c2b.bounds(objects) -> dict(min, max, center, size, radius) Vectors. c2b.targets() -> request targets.
  c2b.movable() -> objects you may move. c2b.retire(obj) -> hide a light/helper you no longer need.
- c2b.color("#hex") -> linear RGB.
request["permissions"] lists what you may change (allowed / not_allowed / movable). Anything outside it is
rolled back automatically, so stay inside it and say in message if the reference needs more (e.g. "allow
Camera to match the angle"). request["scene"]["layout"] has world-space positions, bounding boxes, parents,
lights, cameras, world and render settings."""

ENVIRONMENT_PROMPT = """Environment mode: recreate the reference's lighting and environment as a real, editable
Blender scene (never a picture pasted into the background).
- View every reference with Read. Analyse: key light direction (from highlights and shadow direction) and its
  softness (shadow edge width -> light size relative to distance), fill and rim/kicker lights and ratios,
  shadow density and contact shadows, background colours/gradient/vignette and whether a backdrop surface or
  floor is visible, overall brightness and contrast, specular reflections (the shape of highlight cards, e.g.
  long strip softboxes), and atmosphere (haze, colour temperature).
- Recreate it with c2b.light (prefer large AREA lights for studio softboxes; strip lights are thin rectangles),
  c2b.studio_world, c2b.backdrop and c2b.exposure. Place lights relative to c2b.bounds(c2b.targets()) so they
  scale with the product: distance 2-5x the product size. Use descriptive names: "C2B Key", "C2B Fill",
  "C2B Rim", "C2B Backdrop". Reuse those names on refinements.
- Energy: area light watts scale with distance squared. Calibrate to the reference's exposure: for a ~0.3 m
  product with the key ~1 m away (AgX, 0 EV), a normal key is 20-60 W, fill 0.2-0.5x the key, rim 0.3-1x.
  Overexposure is the most common mistake; if the reference has dark areas, keep them dark.
- Works in EEVEE and Cycles: use light size for softness (no engine-specific hacks), keep the render engine
  and output resolution unchanged, and don't change the camera unless Camera is allowed.
- If an exact match needs a specific HDRI, photographed environment or texture, build the closest procedural
  approximation and say so in scene.limitations (scene.match = "needs_hdri_or_textures")."""

COMPOSITION_PROMPT = """Composition mode: recreate the reference's camera and arrangement with the EXISTING objects.
- View every reference with Read. Analyse: camera elevation and azimuth (which faces are visible, how
  verticals converge), perspective strength -> focal length (strong convergence = wide, flat = long lens),
  framing and margins, the product's orientation, and for exploded views the explode axis, order and gap
  sizes relative to the parts, plus visual balance.
- Identify each part in the reference with the scene objects by name, size (bbox_size) and position. Never add
  replacement or proxy objects. Move only request["permissions"]["movable"] objects, by location/rotation
  (c2b.place / c2b.explode). Keep scale, mesh data and parenting as they are; children follow parents.
- For the camera, adjust the scene's active camera with c2b.camera(use_existing=...) if there is one,
  otherwise create "C2B Camera"; then c2b.frame / c2b.look_at to frame the targets. Keep the resolution.
- Positions are estimates; say briefly in message how you mapped parts, and refine on follow-ups ("increase
  the spacing", "rotate the camera slightly left" = orbit around the target, keeping distance)."""


def system_prompt(mode):
    mode = (mode or "material").lower()
    parts = [COMMON_PROMPT]
    if mode == "material":
        parts.append(MATERIAL_PROMPT)
    else:
        parts.append(SCENE_HELPERS_PROMPT)
        parts.append(ENVIRONMENT_PROMPT if mode == "environment" else COMPOSITION_PROMPT)
    return "\n\n".join(parts)


SYSTEM_PROMPT = system_prompt("material")  # kept for older callers

AUTH_HINTS = ("login", "log in", "not logged", "authenticat", "api key", "oauth", "credential", "unauthorized")


# ---------------------------------------------------------------- preferences

def prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


_login_path = None
IS_WINDOWS = os.name == "nt"
# Windows: keep each run from flashing a console window.
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
# CreateProcess limits a command line to 32767 characters; longer prompts go through stdin.
WIN_ARG_LIMIT = 30000


def login_shell_path():
    """PATH as your Terminal sees it (apps opened from the Dock get a minimal PATH).

    macOS/Linux only. Windows apps already get the user's PATH, and there is no login shell to ask.
    """
    global _login_path
    if _login_path is None:
        _login_path = ""
        if IS_WINDOWS:
            return _login_path
        shell = os.environ.get("SHELL") or ("/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/bash")
        try:
            out = subprocess.run([shell, "-lic", "printf '%s' \"$PATH\""], capture_output=True, text=True,
                                 timeout=10, stdin=subprocess.DEVNULL).stdout
            _login_path = out.strip().splitlines()[-1] if out.strip() else ""
        except Exception:
            pass
    return _login_path


def find_cli(configured=""):
    """The official `claude` CLI: the path set in preferences, else PATH, else the usual install spots."""
    if configured:
        path = os.path.expanduser(configured)
        return path if os.path.isfile(path) else None
    search = os.pathsep.join(p for p in (login_shell_path(), os.environ.get("PATH", "")) if p)
    found = shutil.which("claude", path=search)
    if found:
        return found
    home = os.path.expanduser("~")
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        spots = (os.path.join(home, ".local", "bin", "claude.exe"),       # native installer (install.ps1)
                 os.path.join(home, ".claude", "local", "claude.exe"),
                 os.path.join(appdata, "npm", "claude.cmd"))             # npm install -g
    else:
        spots = (f"{home}/.local/bin/claude", f"{home}/.claude/local/claude", "/opt/homebrew/bin/claude",
                 "/usr/local/bin/claude", f"{home}/.npm-global/bin/claude")
    for path in spots:
        if os.path.isfile(path):
            return path
    return None


_SHIM_TARGET = re.compile(r'"%(?:~)?dp0%?\\?([^"%]+\.(?:js|mjs|cjs|py))"', re.IGNORECASE)


def _resolve_shim(path):
    """The script behind an npm-style .cmd shim, e.g. node_modules\\@anthropic-ai\\claude-code\\cli.js."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(8192)
    except OSError:
        return None
    base = os.path.dirname(path)
    for rel in _SHIM_TARGET.findall(text):
        target = os.path.normpath(os.path.join(base, rel.lstrip("\\/")))
        if os.path.isfile(target):
            return target
    return None


def _interpreter_for(script):
    ext = os.path.splitext(script)[1].lower()
    if ext == ".py":
        return sys.executable   # Blender's own Python
    base = os.path.dirname(script)
    for cand in (os.path.join(base, "node.exe"), os.path.join(os.path.dirname(base), "node.exe")):
        if os.path.isfile(cand):
            return cand
    return shutil.which("node")


def launch_command(cli):
    """argv prefix that starts the CLI.

    macOS/Linux: the executable itself. Windows: a .exe runs directly; a .cmd/.bat shim (npm) is
    resolved to `node cli.js`, because cmd.exe re-parses arguments and would mangle the multi-line
    prompt and JSON. Plain .js/.py scripts get their interpreter.
    """
    ext = os.path.splitext(cli)[1].lower()
    if IS_WINDOWS and ext in (".cmd", ".bat"):
        script = _resolve_shim(cli)
        if script:
            interp = _interpreter_for(script)
            if interp:
                return [interp, script]
        return [cli]
    if ext in (".js", ".mjs", ".cjs", ".py") and (IS_WINDOWS or not os.access(cli, os.X_OK)):
        interp = _interpreter_for(cli)
        if interp:
            return [interp, cli]
    return [cli]


def child_env(ignore_api_key=True):
    env = dict(os.environ)
    path = login_shell_path()
    if path:
        env["PATH"] = path + os.pathsep + env.get("PATH", "")
    if ignore_api_key:
        # In -p mode Claude Code always prefers ANTHROPIC_API_KEY over your subscription sign-in.
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def workspace_dir():
    path = bpy.utils.user_resource("CONFIG", path=os.path.join("claude2blender", "claude_workspace"), create=True)
    return path


# ---------------------------------------------------------------- jobs

class Job:
    def __init__(self, kind, args, env, cwd, timeout):
        self.kind = kind
        self.args = args
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.events = queue.Queue()
        self.proc = None
        self.stdin_text = None   # prompt sent on stdin instead of argv (Windows length limit)
        self.launch = list(args[:1]) if args else []   # argv prefix that starts the CLI
        self.cancelled = False
        self.started = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _run(self):
        prepare = getattr(self, "prepare", None)
        if prepare is not None:
            try:
                prepare(self)
            except Exception as exc:
                self.events.put(("error", f"Could not prepare the request: {exc}"))
                self.events.put(("done", None))
                return
            if self.cancelled:
                self.events.put(("cancelled", None))
                self.events.put(("done", None))
                return
        try:
            self.proc = subprocess.Popen(self.args, cwd=self.cwd, env=self.env,
                                         stdin=subprocess.PIPE if self.stdin_text is not None else subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                         encoding="utf-8", errors="replace", **_NO_WINDOW)
            if self.stdin_text is not None:
                self.proc.stdin.write(self.stdin_text)
                self.proc.stdin.close()
        except OSError as exc:
            self.events.put(("error", f"Could not start Claude Code: {exc}"))
            self.events.put(("done", None))
            return
        stderr_lines = []
        threading.Thread(target=lambda: stderr_lines.extend(self.proc.stderr), daemon=True).start()
        killer = threading.Timer(self.timeout, self._timeout)
        killer.start()
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if self.kind == "generate":
                    try:
                        self.events.put(("json", json.loads(line)))
                    except ValueError:
                        pass
                else:
                    self.events.put(("line", line))
            rc = self.proc.wait()
        finally:
            killer.cancel()
        if self.kind == "check":
            try:
                out = subprocess.run([*self.launch, "--version"], capture_output=True, text=True, timeout=20,
                                     stdin=subprocess.DEVNULL, env=self.env, cwd=self.cwd,
                                     encoding="utf-8", errors="replace", **_NO_WINDOW).stdout.strip()
                self.events.put(("version", out))
            except Exception:
                pass
        if self.cancelled:
            self.events.put(("cancelled", None))
        self.events.put(("stderr", "".join(stderr_lines)[-2000:]))
        self.events.put(("done", rc))

    def _timeout(self):
        self.events.put(("error", f"Claude took longer than {int(self.timeout / 60)} min and was stopped"))
        self.cancel()


_job = None


def busy():
    return _job is not None and _job.thread.is_alive()


def cancel():
    if _job is not None:
        _job.cancel()


# ---------------------------------------------------------------- building the request

MAX_IMAGE_BYTES = 3_500_000
MAX_IMAGE_DIM = 2048

CONVERT_SCRIPT = r"""
import bpy, sys
argv = sys.argv[sys.argv.index("--") + 1:]
src, dst, maxdim = argv[0], argv[1], int(argv[2])
img = bpy.data.images.load(src)
w, h = img.size
s = min(1.0, maxdim / max(w, h, 1))
if s < 1.0:
    img.scale(max(1, int(w * s)), max(1, int(h * s)))
img.file_format = "JPEG" if dst.lower().endswith(".jpg") else "PNG"
img.filepath_raw = dst
img.save()
print("C2B_OK", img.size[0], img.size[1])
"""


def _converter_command(src, dst):
    """A separate Blender process converts/resizes images, so the UI never waits on decoding."""
    binary = bpy.app.binary_path
    if binary and os.path.basename(binary).lower().startswith("blender"):
        return [binary, "-b", "--factory-startup", "--python-expr", CONVERT_SCRIPT, "--", src, dst, str(MAX_IMAGE_DIM)]
    import sys
    return [sys.executable, "-c", CONVERT_SCRIPT, "--", src, dst, str(MAX_IMAGE_DIM)]  # bpy-module builds


def plan_images(request):
    """Decide which file Claude reads for each reference. Returns (paths, conversions) - no decoding here."""
    paths, conversions = [], []
    cache = os.path.join(workspace_dir(), "reference_cache")
    os.makedirs(cache, exist_ok=True)
    for ref in request["references"]:
        path = ref["path"]
        if not ref["exists"]:
            continue
        readable = path.lower().endswith(READABLE_IMAGE_EXTS)
        if readable and os.path.getsize(path) <= MAX_IMAGE_BYTES:
            paths.append(path)
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        ext = ".jpg" if readable else ".png"
        target = os.path.join(cache, f"{abs(hash(path)) % 10**8}_{stem}{ext}")
        ref["readable_copy"] = target
        paths.append(target)
        if not os.path.isfile(target) or os.path.getmtime(target) < os.path.getmtime(path):
            conversions.append((path, target))
    return paths, conversions


def run_conversions(conversions, events=None, env=None):
    """Worker thread: convert/resize references. Returns paths that failed."""
    failed = []
    for src, dst in conversions:
        if events is not None:
            events.put(("status", f"Preparing {os.path.basename(src)}…"))
        try:
            r = subprocess.run(_converter_command(src, dst), capture_output=True, text=True, timeout=120,
                               stdin=subprocess.DEVNULL, env=env, **_NO_WINDOW)
            if "C2B_OK" not in r.stdout or not os.path.isfile(dst):
                failed.append(src)
        except Exception:
            failed.append(src)
    return failed


def _history_brief():
    return [{"summary": e["summary"], "state": e["state"].lower()} for e in history.entries()[-5:]]


def build_prompt(request, images, follow_up):
    lines = []
    if follow_up:
        lines.append("Follow-up in our ongoing conversation. Refine your previous result as asked below.")
    lines.append(f"User prompt: {request['prompt'] or '(none: propose something sensible for the mode)'}")
    lines.append(f"Mode: {request['mode_label']}. {request['mode_guidance']}")
    if images:
        lines.append("Reference images (view each with the Read tool before answering):")
        by_path = {r.get("readable_copy", r["path"]): r for r in request["references"]}
        for path in images:
            ref = by_path.get(path, {})
            note = f" Note: {ref['note']}" if ref.get("note") else ""
            lines.append(f"- {path} (use as: {ref.get('role', 'reference')}).{note}")
    lines.append("Context (this is the `request` dict your code receives):")
    lines.append("```json\n" + json.dumps(request, indent=1) + "\n```")
    return "\n".join(lines)


def start_generate(context):
    """Snapshot the request on the main thread and start `claude -p` in the background."""
    global _job
    state = get_state(context)
    p = prefs()
    cli = find_cli(p.claude_path if p else "")
    if not cli:
        raise RuntimeError("Claude Code CLI not found. Install it, or set its path in the add-on preferences.")

    follow_up = bool(state.gen_follow_up and state.gen_session_id)
    request, _text = bridge.send_request(context, copy_to_clipboard=False)
    request["scene"]["objects"] = bridge.scene_overview()
    request["history"] = _history_brief()
    if request["mode"] == "material":
        request["target_materials"] = materials.target_materials(request)
    if state.proposal_pending:
        request["pending_proposal"] = {"summary": state.proposal_summary, "code": state.proposal_code,
                                       "previewing": history.preview_entry() is not None}
    images, conversions = plan_images(request)
    model = p.model.strip() if p else ""
    session = state.gen_session_id if follow_up else ""

    def prepare(job):
        failed = run_conversions(conversions, job.events, job.env)
        usable = [path for path in images if os.path.isfile(path)]
        if failed:
            job.events.put(("note", f"Could not read {len(failed)} reference image(s)"))
        prompt = build_prompt(request, usable, follow_up)
        launch = launch_command(cli)
        job.launch = launch
        args = [*launch, "-p", prompt,
                "--output-format", "stream-json", "--verbose",
                "--json-schema", json.dumps(RESULT_SCHEMA),
                "--append-system-prompt", system_prompt(request["mode"]),
                "--strict-mcp-config", "--disallowedTools", "mcp__*",
                "--max-turns", str(max(4, len(usable) + 4))]
        if usable:
            args += ["--tools", "Read", "--allowedTools", "Read",
                     "--add-dir", *sorted({os.path.dirname(i) for i in usable})]
        else:
            args += ["--tools", ""]
        if model:
            args += ["--model", model]
        if session:
            args += ["--resume", session]
        if IS_WINDOWS and len(subprocess.list2cmdline(args)) > WIN_ARG_LIMIT:
            # Too long for a Windows command line: `claude -p` reads the prompt from stdin instead.
            args.pop(len(launch) + 1)
            job.stdin_text = prompt
        job.args = args

    timeout = (p.timeout_minutes if p else 5) * 60
    _job = Job("generate", None, child_env(p.ignore_api_key if p else True), workspace_dir(), timeout)
    _job.prepare = prepare
    _job.mode = request["mode"]
    state.gen_status = "CONNECTING"
    state.gen_detail = "Starting Claude Code…"
    state.gen_targets = _targets_phrase(request)
    state.gen_title = ""
    state.gen_outcome = "NONE"
    state.gen_show_details = False
    state.gen_message = ""
    state.gen_started = time.time()
    state.gen_request_id = request["request_id"]
    _job.start()
    _ensure_timer()
    return _job


def _targets_phrase(request):
    sel = request["selection"]
    if sel["faces"]:
        parts = [f"{f['object']} ({f['selected_faces']} faces)" for f in sel["faces"]]
    else:
        parts = [o["name"] for o in sel["objects"]]
    if not parts:
        return "Scene"
    return ", ".join(parts[:2]) + (f" +{len(parts) - 2}" if len(parts) > 2 else "")


def start_check(context):
    """Background check: is the CLI there, and is it signed in?"""
    global _job
    state = get_state(context)
    p = prefs()
    cli = find_cli(p.claude_path if p else "")
    if not cli:
        state.cli_status = "Claude Code CLI not found"
        state.cli_ok = False
        return None
    launch = launch_command(cli)
    script = [*launch, "auth", "status"]
    _job = Job("check", script, child_env(p.ignore_api_key if p else True), workspace_dir(), 60)
    _job.launch = launch
    state.cli_status = "Checking Claude Code…"
    _job.start()
    _ensure_timer()
    return _job


# ---------------------------------------------------------------- main-thread event pump

_STATUS_LABELS = {"system": "Connected to Claude", "assistant": "Claude is working…"}


def _handle_generate_event(state, kind, data):
    if kind == "status":
        state.gen_detail = data
    elif kind == "note":
        _job.note = data
    elif kind == "json":
        t = data.get("type")
        if t == "system" and data.get("subtype") == "init":
            state.gen_status = "PROCESSING"
            state.gen_detail = "Claude is thinking…"
            if data.get("session_id"):
                state.gen_session_id = data["session_id"]
        elif t == "assistant":
            state.gen_status = "PROCESSING"
            for block in data.get("message", {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "Read":
                    name = os.path.basename(block.get("input", {}).get("file_path", ""))
                    state.gen_detail = f"Looking at {name}…"
                elif block.get("type") == "tool_use" and block.get("name") == "StructuredOutput":
                    state.gen_detail = "Writing the change…"
                elif block.get("type") == "text" and block.get("text", "").strip():
                    state.gen_detail = "Claude is writing…"
        elif t == "result":
            _job.result = data
    elif kind == "error":
        _job.error = data
    elif kind == "stderr":
        _job.stderr = data


def _finish_generate(state):
    result = getattr(_job, "result", None)
    error = getattr(_job, "error", None)
    stderr = getattr(_job, "stderr", "")
    elapsed = time.time() - _job.started
    if _job.cancelled and not error:
        state.gen_status = "FAILED"
        state.gen_detail = "Cancelled"
        return
    if result and result.get("session_id"):
        state.gen_session_id = result["session_id"]
    if error or not result or result.get("is_error") or result.get("subtype") != "success":
        text = error or (result or {}).get("result") or stderr.strip() or "Claude Code exited without a result"
        if any(h in text.lower() for h in AUTH_HINTS):
            text = "Claude Code isn't signed in. In Terminal run `claude`, then `/login`. " + text[:200]
        state.gen_status = "FAILED"
        state.gen_detail = text[:300]
        return
    out = result.get("structured_output")
    if not isinstance(out, dict):
        try:
            out = json.loads(result.get("result", ""))
        except ValueError:
            out = {"summary": "", "code": "", "message": result.get("result", "")}
    code = (out.get("code") or "").strip()
    _set_material_summary(state, out.get("material") or {})
    _set_scene_summary(state, out.get("scene") or {})
    if getattr(_job, "note", ""):
        if getattr(_job, "mode", "material") == "material":
            state.mat_limits = (_job.note + ". " + state.mat_limits).strip()
        else:
            state.scene_limits = (_job.note + ". " + state.scene_limits).strip()
            state.scene_match = state.scene_match if state.scene_match not in ("", "close", "n/a") else "approximate"
    state.gen_message = (out.get("message") or "").strip()
    state.gen_title = (out.get("summary") or "").strip()
    state.gen_elapsed = elapsed
    state.gen_show_details = False
    state.gen_turns += 1
    if code and history.preview_entry() is not None:
        # A new result replaces the one being previewed: take the old preview back first.
        from .operators import _set_material_view
        try:
            history.end_preview(keep=False)
        except history.HistoryError:
            pass
        _set_material_view(bpy.context, False)
    if code:
        try:
            bridge._api.stage_proposal(code, state.gen_title or "Change from Claude",
                                       request_id=state.gen_request_id, _from_generate=True)
        except SyntaxError as exc:
            state.gen_status = "FAILED"
            state.gen_detail = f"Claude's code has a syntax error (line {exc.lineno}). Ask a follow-up to fix it."
            return
        state.gen_status = "COMPLETED"
        state.gen_outcome = "STAGED"
        state.gen_detail = "Staged for review"
        state.status = ""
    else:
        state.gen_status = "COMPLETED"
        state.gen_outcome = "QUESTION"
        state.gen_detail = "Claude needs your input"


def _set_material_summary(state, mat):
    state.mat_name = (mat.get("name") or "").strip()
    rgb = materials.hex_to_srgb(mat.get("base_color_hex", ""))
    state.mat_has_swatch = rgb is not None
    if rgb:
        state.mat_swatch = rgb
    parts = []
    for key, label in (("roughness", "Roughness"), ("metallic", "Metallic")):
        value = mat.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            parts.append(f"{label} {value:.2f}")
    state.mat_line = " · ".join(parts)
    state.mat_surface = (mat.get("surface") or "").strip()
    state.mat_match = mat.get("match") or ""
    state.mat_limits = (mat.get("limitations") or "").strip()


SCENE_FIELDS = (("lighting", "LIGHT_AREA"), ("world", "WORLD"), ("backdrop", "MESH_PLANE"),
                ("camera", "CAMERA_DATA"), ("objects", "OBJECT_DATA"), ("render", "SCENE"))


def _set_scene_summary(state, sc):
    lines = []
    for key, icon in SCENE_FIELDS:
        text = (sc.get(key) or "").strip().replace("|", "/").replace("\n", " ")
        if text:
            lines.append(f"{icon}|{text}")
    state.scene_lines = "\n".join(lines)
    state.scene_match = sc.get("match") or ""
    state.scene_limits = (sc.get("limitations") or "").strip()


def scene_summary_lines(state):
    out = []
    for line in state.scene_lines.splitlines():
        icon, _, text = line.partition("|")
        out.append((icon, text))
    return out


def _finish_check(state, lines):
    text = "\n".join(lines)
    try:
        info = json.loads(text)
    except ValueError:
        info = {}
    version = getattr(_job, "version", "")
    if info.get("loggedIn"):
        method = info.get("authMethod", "")
        state.cli_ok = True
        state.cli_status = f"Signed in ({method}) · {version or 'Claude Code'}"
    elif info:
        state.cli_ok = False
        state.cli_status = "Not signed in: open Terminal, run `claude`, then `/login`"
    else:
        state.cli_ok = False
        state.cli_status = (text or "Could not read Claude Code status")[:200]


def _pump():
    global _job
    if _job is None:
        return None
    state = get_state()
    lines = getattr(_job, "lines", None)
    if lines is None:
        lines = _job.lines = []
    done = False
    while True:
        try:
            kind, data = _job.events.get_nowait()
        except queue.Empty:
            break
        if kind == "done":
            done = True
            break
        if _job.kind == "generate":
            _handle_generate_event(state, kind, data)
        elif kind == "line":
            lines.append(data)
        elif kind == "version":
            _job.version = data
    if done:
        if _job.kind == "generate":
            _finish_generate(state)
        else:
            _finish_check(state, lines)
        _job = None
        bridge._redraw()
        return None
    bridge._redraw()
    return 0.25


def _ensure_timer():
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, first_interval=0.1)


def pump_until_done(timeout=600):
    """For tests and scripts: drive the event pump without Blender's event loop."""
    end = time.time() + timeout
    while _job is not None and time.time() < end:
        _pump()
        time.sleep(0.05)
    return _job is None


def wrap(text, width):
    out = []
    for para in text.splitlines() or [""]:
        out.extend(textwrap.wrap(para, max(20, width)) or [""])
    return out


# ---------------------------------------------------------------- preferences UI

class C2B_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    claude_path: bpy.props.StringProperty(
        name="Claude Code CLI",
        description="Path to the `claude` command. Leave empty to find it automatically",
        subtype="FILE_PATH",
    )
    model: bpy.props.StringProperty(
        name="Model",
        description="Optional model alias or name passed to --model (empty = Claude Code's default)",
    )
    timeout_minutes: bpy.props.IntProperty(name="Time limit (min)", default=5, min=1, max=30)
    ignore_api_key: bpy.props.BoolProperty(
        name="Use my Claude subscription sign-in",
        description=("Hide ANTHROPIC_API_KEY from Claude Code for these runs. In -p mode Claude Code "
                     "otherwise always uses that key (billed separately) instead of your sign-in"),
        default=True,
    )

    texture_library: bpy.props.StringProperty(
        name="Texture Library",
        description=("Folder holding texture packs for the studio presets (e.g. worn-old-plastic-bl, "
                     "Copper-scuffed_bl, Poliigon folders). Presets without their pack use procedural grain"),
        subtype="DIR_PATH",
    )

    def draw(self, context):
        layout = self.layout
        state = get_state(context)
        layout.prop(self, "claude_path")
        found = find_cli(self.claude_path)
        layout.label(text=f"Using: {found}" if found else "Claude Code CLI not found", icon="CHECKMARK" if found else "ERROR")
        row = layout.row()
        row.prop(self, "model")
        row.prop(self, "timeout_minutes")
        layout.prop(self, "ignore_api_key")
        row = layout.row()
        row.operator("claude2blender.check_cli", icon="FILE_REFRESH")
        if state.cli_status:
            row.label(text=state.cli_status)
        box = layout.box()
        box.prop(self, "texture_library")
        col = box.column(align=True)
        col.scale_y = 0.8
        for _key, folder, ok in presets.pack_status(bpy.path.abspath(self.texture_library)):
            col.label(text=f"{folder}: {'found' if ok else 'not found (procedural fallback)'}",
                      icon="CHECKMARK" if ok else "DOT")


def register():
    bpy.utils.register_class(C2B_Preferences)


def unregister():
    cancel()
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
    bpy.utils.unregister_class(C2B_Preferences)
