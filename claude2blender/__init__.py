# Claude2Blender - a native Blender sidebar for working with Claude.
#
# Generate with Claude runs your own signed-in Claude Code CLI in the background
# (claude_cli.py). The Claude Desktop + Blender MCP route remains as a fallback
# (bridge.py). Either way Claude's code is only staged for review: you apply it,
# and every Apply goes into an undo/redo history (history.py).

bl_info = {
    "name": "Claude2Blender",
    "author": "Talus / Claude2Blender",
    "version": (0, 6, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > Claude",
    "description": "Prompt Claude from Blender's sidebar: materials, lighting and composition from references, with review and undo/redo history",
    "category": "Interface",
}

from . import properties, references, bridge, history, claude_cli, operators, ui  # noqa: E402

_modules = (properties, references, bridge, history, claude_cli, operators, ui)


def register():
    for module in _modules:
        module.register()


def unregister():
    for module in reversed(_modules):
        module.unregister()
