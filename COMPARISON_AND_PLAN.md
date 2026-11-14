# Claude2Blender vs Blender Director: comparison and merge plan

Sources: this repo (Claude2Blender v0.5, ~7k lines), `C:\Users\bentr\Blender Director`
(split out of KeyShot Director, v0.5.0, Blender 5.3), and the chat history of the
"Claude integration with Blender" session (textures, lighting, CMF, Final Touch / Mule idea).

## 1. What each one is

| | Claude2Blender (this repo) | Blender Director |
|---|---|---|
| Core idea | Sidebar where you prompt Claude, **review code, then Apply**, with full Undo/Redo | Production pipeline: real CAD in, stills/films/relit looks out |
| Claude connection | Add-on spawns `claude -p` (your sign-in), Read-only tool, structured JSON `{summary, code, message}` | Localhost live link (`director.mjs ping/notes/shots/style/run/exec/save`) plus headless `blender -b` scripts Claude runs |
| Safety model | Strong: staged code, Preview with Original/Proposed, Preserve Geometry, Allowed Changes, change-record History | Light: edit passes create **versioned models** with review shots and a report; `exec` over the live link is trusted |
| Input | Prompt + selection (objects/faces) + reference photos with roles | Brief (notes pinned to parts, hero cameras), reference photo for lighting |
| Materials | Claude invents a procedural Principled shader per selection; `assign_material` targets faces/objects exactly | Preset library (`MATERIALS` dict, `MAT_<preset>` names from Fusion, `director_materials.json` rules), Poliigon texture packs, tuned CMF for the SpaceMouse |
| Lighting | Claude builds lights/world/backdrop via `c2b` helpers, estimates from one photo | Dark / High-key / Gradient styles, **Finish** (one capture then live per-light sliders, grade, bloom, A/B, push lights back), **ref-match via light basis + NNLS** (measured, not guessed) |
| Camera/composition | Match reference angle and exploded arrangement (estimated) | Named shots (hero, front, profile, top, detail), turntable, exploded still and animation, push-in / light-sweep / assemble motion |
| Output | Viewport preview only | Production pack: 5 shots x 3 formats, cut-outs, trio sheet, video, quality gates, contact sheet; 4K/8K stills |
| Tests | 7 headless test files, real-Claude runs | Spikes and example scenes, fewer automated tests |
| Install | Blender 4.2+ extension zip, macOS/Linux tested | Blender 5.3 extension zip (bundled engine), Windows, needs Node for the live link |

## 2. Overlap

* Both are Blender extensions with an N-panel, both are driven by Claude Code and both are
  meant to avoid API keys.
* Both do reference-photo lighting match, but by different methods (Claude eyeballing vs measured
  light basis + NNLS).
* Both do materials and exploded layouts, but Claude2Blender is open-ended and Director is
  preset-driven.

## 3. Where each is stronger

**Claude2Blender wins on**
* Trust and control: nothing runs until Apply, exact undo, scoped permissions, geometry guard.
* Targeting: per-face material assignment, linked-duplicate handling, parent-safe moves.
* Generality: works on any scene without a naming convention.
* Test coverage and docs.

**Director wins on**
* Render quality: tuned studio looks, Poliigon-based injection-moulded plastic, vulcanized rubber,
  copper PCB, translucent resin, bevel/edge softening, render passes.
* Real-time feel: live Rendered viewport (DLSS on 5.3), live link so you do not export a brief each time.
* Finishing: live Finish sliders and A/B, one-button production pack, film clips.
* Measured lighting match, which fixes Claude2Blender's stated weakness ("intensities depend on exposure").

## 4. Lessons from the Director chats worth carrying over

* Textures took the most iteration. What worked: real PBR packs (worn plastic, scuffed copper,
  vulcanized rubber) over pure procedural, scaled up, darker than first guess. Keep a
  `materials/` library of texture packs plus presets.
* CMF thinking per part: soft-touch coating on grips/wrist rest, hard ABS shell, TPE pads or standoffs
  on the base, resin for light-transmitting parts, copper only for PCB-inlaid parts, steel for the rest.
* Soften edges with a bevel in render, but true edge softness belongs in CAD (Fusion fillets).
* Work should feel like CLI/MCP: no manual "export brief" step. This is the live-link requirement.
* Exploded views: spread by part height/axis, animate with an Apple-style ease, and verify the frame.
* Long-term vision: **Mule-style sketch/edit base + Final Touch-style sliders + prompt-driven lighting
  in one tool.** Mule may open-source later; language choice (Rust vs Python) was left as
  "use my plan if better". Python inside Blender is the pragmatic answer for now.
* Known Director gaps: texture packs are referenced from the author's Downloads folder, DLSS needs
  NVIDIA 620+, part classification is SpaceMouse-specific.

## 5. Recommended direction: Claude2Blender as the shell, Director as the engine

Keep Claude2Blender's panel, safety model and History as the front door, and plug Director's
rendering engine in as capabilities. Reasons: the safety layer is the hardest part to retrofit,
while Director's pieces are mostly self-contained scripts with env-var interfaces.

### Phase 1: Merge the low-risk wins (small)
1. Add Director's **material library + `director_materials.json` rules** as `materials.py` data;
   expose a "Preset" dropdown in Material mode that skips Claude for known parts.
2. Ship texture packs inside the add-on folder (or a configurable library path) so presets do not
   depend on `Downloads`.
3. Add **studio styles** (Dark / High-key / Gradient) as one-click Environment presets that create
   the usual `C2B` lights, world and backdrop.

### Phase 2: Measured lighting (medium)
4. Port **ref-match (light basis + NNLS)**. Output becomes a staged proposal, so it goes through
   Review, Preview, Allowed Changes and History like everything else.
5. Feed the measured result to Claude as context for follow-ups ("softer key") instead of asking
   Claude to guess intensities. Closes the README roadmap item on self-correction.

### Phase 3: Finish and Pack (medium to large)
6. **Finish**: one capture, then live sliders per light, grade, bloom, A/B. Record into History
   as one change when pushed back to the scene.
7. **Pack**: shots x formats, cut-outs, trio sheet, contact sheet, quality gates. Runs in background
   `blender -b`, never mutates the open file.
8. Rendered before/after thumbnails next to the reference (README roadmap).

### Phase 4: Live link and motion (large)
9. Replace the one-shot `claude -p` loop with an optional persistent session plus a **localhost live
   link** so Claude can read notes, take shots, and stage edits without exporting a brief. Keep the
   no-auto-run rule: `exec` becomes `stage_proposal`.
10. Motion library: push-in, light-sweep, assemble, turntable, exploded animation.
11. Director's **edit loop** (versioned models + review shots + report) maps onto History entries.

### Phase 5: Product direction
12. Sketch-on-model notes (Director Brief already has this) toward the Mule / Final Touch hybrid.

### Animation workflow (saas-animation v2 skill, bundled in `skills/saas-animation/`)

Phase 4's motion work follows this skill rather than inventing its own process:

* **Storyboard first, always.** 5 to 10 numbered shots with time ranges, filled into
  `templates/storyboard.html`, approved by you before any animation render. Model check shots and
  storyboard stills are fine before approval.
* **One continuous curve per move** (`scripts/blender/tracks.py`, `mono()` never overshoots). No
  zoom in then out, no direction changes, fast then slow for longer.
* **Every shot transforms into the next**, product dead centre, something always moving.
* **One key light**, nearly black world, 70 to 90 mm lens, shallow depth of field.
* **Render plan:** test at 1280 wide on about 15 frames, final at 1920 with 24 samples plus motion
  blur, upscale to 4K. Transparent background so it composites onto black, footage or text.
  Re-render only changed frames.
* Sound after the picture is approved. Flat silhouette mode is available as a style.

How it maps onto the add-on: Director's motion clips (push-in, light sweep, assemble, exploded)
become storyboard shots; the panel gets a Storyboard step that renders numbered stills for approval
before the background animation render starts.

## Progress

* **Phase 1, items 1 and 2: done (2026-11-13).** `claude2blender/presets.py` holds 23 studio presets
  ported from Director (white dry-blast ABS, vulcanized/worn black, soft-touch, TPE, dot-grid rubber,
  yellow accent, brushed steel, scuffed copper, brass, clear resin, PCB green/black, LED, glass,
  aluminium and more). Material mode has a preset dropdown and **Use Preset**, which stages the change
  for Preview/Apply/Undo like any Claude change. Claude can call `preset_material(key)` too.
  Texture packs are found under the **Texture Library** folder in the add-on preferences, otherwise
  procedural grain is used. Test: `tests/test_presets.py`.
* **Phase 1 item 3: done.** Dark / High-key / Gradient studio styles (`c2b.studio_style`, **Use Style**
  in Environment mode), camera-relative and product-scaled. Test: `tests/test_styles.py`.
* **Windows support: done.** CLI discovery for the native installer and npm, `.cmd` shims run as
  `node cli.js`, stdin for long prompts, no console flash; test stand-in is per-OS (`tests/fake_cli.py`).
  The full suite (8 files) passes on Windows 11 / Blender 5.3. macOS behaviour is unchanged but was
  not re-run here.
* **Phase 1 is complete.**
* **Phase 2: done (items 4 and 5).** `claude2blender/refmatch.py`: basis render in a background
  Blender on a scene copy, NNLS fit, result staged as real `C2B Match` area lights + world, refined by
  Claude by name. Changes from Director's v1: OpenImageIO instead of OpenEXR (works on Blender 5.1
  too), a 6x6 product grid and 4x4 background grid instead of 3x3/3x3 (Director's 15 regions for 25
  lights were underdetermined: on the test reference its strongest light came out back-right, the
  finer grid finds the front-left key), a centred-box fallback and gap filling for the reference mask,
  and exposure folded into light energy rather than a compositor look.
  Known limit: a key between dome rings tends to land in the lower ring.
* **Windows, end to end:** a real Generate through `claude.exe` succeeded (2026-11-13).
* **Next:** Phase 3, Finish (live per-light sliders) and Pack (shots x formats). Item 8 (rendered
  before/after next to the reference) builds on the match render here.

## 6. Risks and decisions

* **Blender versions:** Claude2Blender targets 4.2+, Director targets 5.3 (DLSS, new video API such as
  `media_type = "VIDEO"`). Decide whether features degrade gracefully on 4.2 or the merged add-on
  requires 5.x.
* **Node dependency:** the live link uses Node. Port it to a Python socket server inside Blender to
  keep one install.
* **Auth:** keep Claude2Blender's CLI-only, subscription-sign-in design. Do not adopt the Agent SDK.
* **Windows:** Director is built and tested on Windows, Claude2Blender is not. The merge gets
  Windows coverage for free, but check the CLI discovery code.
* **Repo layout:** Director already lives in its own repo. Either import it as a subfolder
  (`claude2blender/engine/`) or keep it separate and vendor a pinned copy at build time.
* **Licensing of texture packs** (Poliigon, Gumroad Final Touch) must be checked before bundling anything.

## 7. Suggested first step

Phase 1, item 1 and 2: it is a day of work, it makes materials visibly better right away, and it
does not touch the safety model.
