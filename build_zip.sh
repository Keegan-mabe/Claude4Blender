#!/usr/bin/env bash
# Builds dist/claude2blender-<version>.zip, installable from Blender's Preferences.
# Uses Blender's own extension builder when Blender is found (works on macOS, Linux and Windows/Git Bash),
# else falls back to `zip`. Set BLENDER=/path/to/blender to choose the binary.
set -euo pipefail
cd "$(dirname "$0")"
version=$(sed -n 's/^version = "\(.*\)"/\1/p' claude2blender/blender_manifest.toml)
out="dist/claude2blender-$version.zip"
mkdir -p dist
rm -f "$out"

blender="${BLENDER:-}"
if [ -z "$blender" ]; then
  for c in blender /Applications/Blender.app/Contents/MacOS/Blender \
           "/c/Program Files/Blender Foundation/Blender 5.3/blender.exe" \
           "/c/Program Files/Blender Foundation/Blender 4.5/blender.exe" \
           "/c/Program Files/Blender Foundation/Blender 4.2/blender.exe"; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then blender="$c"; break; fi
  done
fi

if [ -n "$blender" ]; then
  "$blender" --factory-startup --command extension build --source-dir claude2blender --output-filepath "$out"
elif command -v zip >/dev/null 2>&1; then
  zip -rq "$out" claude2blender -x '*/__pycache__/*' '*.pyc' '.DS_Store'
else
  echo "Need Blender or zip to build" >&2; exit 1
fi
echo "$out"
