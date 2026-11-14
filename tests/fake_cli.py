"""Install a stand-in `claude` CLI for the tests, in the form each OS really uses.

macOS / Linux: an executable script named `claude` with a python3 shebang.
Windows: an npm-style `claude.cmd` shim pointing at a script under node_modules, exactly like
`npm install -g @anthropic-ai/claude-code` lays it out (there the script is cli.js run by node; here
it is cli.py, which the add-on runs with Blender's Python). This exercises the same shim resolution
the add-on uses for a real npm install, so arguments never pass through cmd.exe.

Returns the path `find_cli()` should report.
"""

import os
import stat
import sys

SHIM = """@ECHO off
SETLOCAL
SET dp0=%~dp0
"{python}" "%dp0%\\node_modules\\fake-claude\\cli.py" %*
"""


def install(bindir, source):
    os.makedirs(bindir, exist_ok=True)
    if os.name == "nt":
        pkg = os.path.join(bindir, "node_modules", "fake-claude")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "cli.py"), "w", encoding="utf-8") as fh:
            fh.write(source)
        shim = os.path.join(bindir, "claude.cmd")
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write(SHIM.format(python=sys.executable))
        return shim
    path = os.path.join(bindir, "claude")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(source)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path
