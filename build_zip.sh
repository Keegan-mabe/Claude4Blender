#!/usr/bin/env bash
# Builds dist/claude2blender-<version>.zip, installable from Blender's Preferences.
set -euo pipefail
cd "$(dirname "$0")"
version=$(sed -n 's/^version = "\(.*\)"/\1/p' claude2blender/blender_manifest.toml)
mkdir -p dist
rm -f "dist/claude2blender-$version.zip"
zip -rq "dist/claude2blender-$version.zip" claude2blender -x '*/__pycache__/*' '*.pyc' '.DS_Store'
echo "dist/claude2blender-$version.zip"
