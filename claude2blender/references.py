"""Thumbnails for reference images.

Previews are loaded with bpy.utils.previews, which keeps them outside the
.blend file: adding a reference never adds an Image datablock to your project.
"""

import os

import bpy
import bpy.utils.previews

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".exr", ".hdr", ".tga")

_previews = None


def icon_for(path):
    """Icon id for a thumbnail of `path`, or 0 if it can't be loaded."""
    if _previews is None or not path:
        return 0
    path = bpy.path.abspath(path)
    if not os.path.isfile(path):
        return 0
    if path not in _previews:
        try:
            _previews.load(path, path, "IMAGE")
        except Exception:
            return 0
    return _previews[path].icon_id


def is_image(path):
    return path.lower().endswith(IMAGE_EXTENSIONS)


def register():
    global _previews
    _previews = bpy.utils.previews.new()


def unregister():
    global _previews
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
