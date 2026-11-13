"""Generate with Claude, end to end, against a stand-in `claude` CLI (no network, no sign-in needed).

Set C2B_REAL_CLAUDE=1 to run one extra request against the real, signed-in Claude Code CLI.
"""

import json
import os
import stat
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bpy  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
import claude2blender as addon  # noqa: E402

addon.register()
from claude2blender import claude_cli, history, ui  # noqa: E402
sys.path.insert(0, HERE)


class _L:
    """Permissive stand-in layout for drawing the panels headless."""
    def __getattr__(self, name):
        return lambda *a, **k: _L()


class _P:
    def __init__(self):
        self.layout = _L()


def draw_panels():
    for cls in (ui.C2B_PT_main, ui.C2B_PT_apply):
        cls.draw(_P(), bpy.context)

state = bpy.context.window_manager.claude2blender
ok = 0


def check(cond, msg):
    global ok
    assert cond, msg
    ok += 1
    print("  ok:", msg)


FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"args": args, "api_key_seen": "ANTHROPIC_API_KEY" in os.environ, "cwd": os.getcwd()}) + "\n")
mode = open(os.environ["FAKE_CLAUDE_MODE"]).read().strip()
if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)"); sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": mode != "noauth", "authMethod": "claude.ai" if mode != "noauth" else "none"}))
    sys.exit(0 if mode != "noauth" else 1)
def emit(o): print(json.dumps(o), flush=True)
sid = args[args.index("--resume") + 1] if "--resume" in args else "sess-1234"
emit({"type": "system", "subtype": "init", "session_id": sid})
if mode == "noauth":
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login", "session_id": sid}); sys.exit(1)
if mode == "slow":
    time.sleep(30)
prompt = args[args.index("-p") + 1]
if "--tools" in args and args[args.index("--tools") + 1] == "Read":
    for line in prompt.splitlines():
        if line.startswith("- /"):
            emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": line[2:].split(" (")[0]}}]}})
time.sleep(0.3)
follow = "Follow-up" in prompt
if mode == "nocode":
    out = {"summary": "Question", "code": "", "message": "Which face should be the label?"}
elif mode == "badcode":
    out = {"summary": "Broken", "code": "def (:", "message": "oops"}
else:
    color = "(0.02, 0.15, 0.75, 1)" if not follow else "(0.01, 0.05, 0.30, 1)"
    out = {"summary": "Darker blue on Cube" if follow else "Blue on Cube",
           "code": "m = bpy.data.materials.new('Gen Blue')\nm.diffuse_color = " + color + "\n"
                   "o = bpy.data.objects[request['selection']['active_object']]\n"
                   "o.data.materials.clear(); o.data.materials.append(m)\n",
           "message": "Made it a satin blue."}
emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "StructuredOutput", "input": out}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(out), "structured_output": out, "session_id": sid})
'''

tmp = tempfile.mkdtemp()
fake_dir = os.path.join(tmp, "bin")
os.makedirs(fake_dir)
fake = os.path.join(fake_dir, "claude")
with open(fake, "w") as fh:
    fh.write(FAKE)
os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
log = os.path.join(tmp, "calls.jsonl")
mode_file = os.path.join(tmp, "mode")
os.environ["FAKE_CLAUDE_LOG"] = log
os.environ["FAKE_CLAUDE_MODE"] = mode_file
os.environ["ANTHROPIC_API_KEY"] = "sk-test-should-be-hidden"
claude_cli._login_path = fake_dir  # what a login shell's PATH would give


def set_mode(m):
    open(mode_file, "w").write(m)


def calls():
    return [json.loads(line) for line in open(log)] if os.path.exists(log) else []


# Scene
bpy.ops.mesh.primitive_cube_add()
cube = bpy.context.active_object
cube.select_set(True)
ref = os.path.join(tmp, "blue.png")
img = bpy.data.images.new("ref", 64, 64)
img.pixels = [0.02, 0.15, 0.75, 1.0] * (64 * 64)
img.filepath_raw = ref
img.file_format = "PNG"
img.save()
bpy.data.images.remove(img)
r = state.references.add()
r.filepath = ref
r.role = "MATERIAL"
state.prompt = "Make it blue"

check(claude_cli.find_cli() == fake, "finds the CLI via the login-shell PATH")

# 1. Check Claude Code
set_mode("ok")
claude_cli.start_check(bpy.context)
claude_cli.pump_until_done(30)
check(state.cli_ok and "Signed in" in state.cli_status and "9.9.9" in state.cli_status, "sign-in check reports signed in")

# 2. Generate
images_before = len(bpy.data.images)
mesh_sig = history.geometry_signature(cube.data)
t0 = time.time()
assert bpy.ops.claude2blender.generate() == {"FINISHED"}
check(time.time() - t0 < 1.0, "Generate returns immediately (Claude runs in the background)")
check(claude_cli.busy() and state.gen_status == "CONNECTING", "status shows Connecting")
claude_cli.pump_until_done(30)
check(state.gen_status == "COMPLETED" and state.proposal_pending, "result staged in Review & Apply")
check(state.proposal_summary == "Blue on Cube" and "Gen Blue" in state.proposal_code, "summary and code staged")
check("Gen Blue" not in bpy.data.materials, "nothing ran: code waits for Apply")
check(state.gen_session_id == "sess-1234", "session remembered for follow-ups")
check(state.gen_outcome == "STAGED" and state.gen_title == "Blue on Cube" and state.gen_targets == "Cube"
      and state.gen_elapsed > 0, "compact card: title, target and time recorded")
check(ui._card_outcome(state) == ("Staged for review", "TIME"), "card shows Staged for review")
check(not state.gen_show_details and "satin" in state.gen_message, "Claude's explanation kept, collapsed in Details")
draw_panels()
check(len(bpy.data.images) == images_before, "no image datablocks added to the file")

call = calls()[-1]
a = call["args"]
check(not call["api_key_seen"], "ANTHROPIC_API_KEY hidden so the subscription sign-in is used")
check("--strict-mcp-config" in a and "mcp__*" in a, "MCP servers disabled for the run")
check(a[a.index("--tools") + 1] == "Read" and a[a.index("--allowedTools") + 1] == "Read", "only the Read tool is available")
check(os.path.dirname(ref) in a[a.index("--add-dir") + 1:], "reference image folder granted with --add-dir")
check("--json-schema" in a and json.loads(a[a.index("--json-schema") + 1])["required"] == ["summary", "code", "message", "material", "scene"],
      "structured output schema passed")
prompt = a[a.index("-p") + 1]
check("Make it blue" in prompt and ref in prompt and '"active_object": "Cube"' in prompt, "prompt carries request, image and selection")
check(call["cwd"] == claude_cli.workspace_dir(), "runs in its own workspace, not a project folder")

# 3. Apply through the normal guarded path
bpy.ops.claude2blender.apply_proposal()
check(state.last_apply_ok and cube.data.materials[0].name == "Gen Blue", "Apply works on a generated change")
check(history.entries()[-1]["summary"] == "Blue on Cube", "generated change is in History")
check(state.gen_outcome == "APPLIED" and ui._card_outcome(state)[0] == "Applied", "card switches to Applied after Apply")
draw_panels()
bpy.ops.claude2blender.history_undo()
check(ui._card_outcome(state)[0] == "Undone", "card shows Undone after History Undo")
bpy.ops.claude2blender.history_redo()
check(ui._card_outcome(state)[0] == "Applied", "card shows Applied again after Redo")
check(history.geometry_signature(cube.data) == mesh_sig, "geometry untouched")

# 4. Follow-up resumes the same session
state.prompt = "a bit darker"
bpy.ops.claude2blender.generate()
claude_cli.pump_until_done(30)
a = calls()[-1]["args"]
check("--resume" in a and a[a.index("--resume") + 1] == "sess-1234", "follow-up resumes the conversation")
check("Follow-up" in a[a.index("-p") + 1] and '"history"' in a[a.index("-p") + 1], "follow-up sends history and fresh context")
check(state.proposal_summary == "Darker blue on Cube", "refined proposal staged")
bpy.ops.claude2blender.apply_proposal()
bpy.ops.claude2blender.history_undo()
check(cube.data.materials[0].name == "Gen Blue", "undo/redo history still works with generated changes")

# Discard
set_mode("ok")
bpy.ops.claude2blender.generate()
claude_cli.pump_until_done(30)
bpy.ops.claude2blender.discard_proposal()
check(state.gen_outcome == "DISCARDED", "card shows Discarded")

# 5. New conversation
bpy.ops.claude2blender.new_conversation()
bpy.ops.claude2blender.generate()
claude_cli.pump_until_done(30)
check("--resume" not in calls()[-1]["args"], "New Conversation starts fresh")

# 6. No-code reply, bad code, not signed in, cancel
state.proposal_pending = False
set_mode("nocode")
bpy.ops.claude2blender.generate()
claude_cli.pump_until_done(30)
check(state.gen_status == "COMPLETED" and not state.proposal_pending and "label" in state.gen_message,
      "a question from Claude is shown, nothing staged")
check(state.gen_outcome == "QUESTION", "questions stay visible (not hidden in Details)")
draw_panels()

set_mode("badcode")
bpy.ops.claude2blender.generate()
claude_cli.pump_until_done(30)
check(state.gen_status == "FAILED" and "syntax" in state.gen_detail and not state.proposal_pending, "invalid code is refused")

set_mode("noauth")
bpy.ops.claude2blender.generate()
claude_cli.pump_until_done(30)
check(state.gen_status == "FAILED" and "/login" in state.gen_detail, "not signed in is explained")
draw_panels()
claude_cli.start_check(bpy.context)
claude_cli.pump_until_done(30)
check(not state.cli_ok and "Not signed in" in state.cli_status, "sign-in check reports not signed in")

set_mode("slow")
bpy.ops.claude2blender.generate()
time.sleep(0.5)
claude_cli._pump()
check(state.gen_status in ("CONNECTING", "PROCESSING"), "status shows progress while running")
bpy.ops.claude2blender.cancel_generate()
claude_cli.pump_until_done(10)
check(state.gen_status == "FAILED" and state.gen_detail == "Cancelled" and not claude_cli.busy(), "Cancel stops Claude")

# 7. Missing CLI
claude_cli._login_path = "/nonexistent"
saved_path = os.environ["PATH"]
os.environ["PATH"] = "/usr/bin:/bin"
if claude_cli.find_cli() is None:
    try:
        bpy.ops.claude2blender.generate()
    except RuntimeError:
        pass
    check(state.gen_status == "FAILED" and "not found" in state.gen_detail, "missing CLI is explained")
os.environ["PATH"] = saved_path

# 8. Optional: the real Claude Code CLI
if os.environ.get("C2B_REAL_CLAUDE"):
    os.environ.pop("ANTHROPIC_API_KEY", None)
    claude_cli._login_path = None
    import shutil
    claude_cli._login_path = os.path.dirname(shutil.which("claude", path=saved_path) or "")
    bpy.ops.claude2blender.new_conversation()
    state.prompt = "Match the reference: make the cube that blue, satin finish"
    state.proposal_pending = False
    bpy.ops.claude2blender.generate()
    claude_cli.pump_until_done(240)
    print("   real:", state.gen_status, "|", state.gen_detail, "|", state.proposal_summary)
    print("   real message:", state.gen_message[:200])
    check(state.gen_status == "COMPLETED" and state.proposal_pending, "real Claude Code: change staged for review")
    compile(state.proposal_code, "<real>", "exec")
    bpy.ops.claude2blender.apply_proposal()
    check(state.last_apply_ok, "real Claude Code: generated change applies cleanly")
    state.prompt = "Now make it glossier"
    bpy.ops.claude2blender.generate()
    claude_cli.pump_until_done(240)
    print("   real follow-up:", state.gen_status, "|", state.proposal_summary)
    check(state.gen_status == "COMPLETED" and state.proposal_pending, "real Claude Code: follow-up refines")

addon.unregister()
print(f"\nAll {ok} generate checks passed on Blender {bpy.app.version_string}")
