"""Paste this into Houdini's Python SOP (not Python Snippet) to create points.

Points are placed directly in UV space: the bottom-left corner of the image is
the origin (0, 0, 0) and the top-right corner is (1, 1, 0), so this geometry
lines up with a Vertex Split + flatten of the model.  Feed it to input0 of
project_windows.vex to move the points onto the real 3D surface.

JSON_PATH may be a single *_centroids.json, or the output folder produced by
extract_windows.py.  When it is a folder, every *_centroids.json inside is
loaded and the points are tagged with the texture they came from.
"""
import glob
import json
import os

import hou

# Change these for the project.  Use forward slashes on Windows.
JSON_PATH = r"D:/Projects/Work/output/windows"
TEXTURE_FILTER = ""   # e.g. "buildingA" to load only that texture's JSON

# houdini_window_tool.py adds jsonpath / texturefilter parameters to the SOP it
# builds.  When they exist they win, so the same file works pasted by hand and
# driven from the tool.
_node = hou.pwd()
if _node is not None and _node.parm("jsonpath") is not None:
    JSON_PATH = _node.evalParm("jsonpath")
    TEXTURE_FILTER = _node.evalParm("texturefilter") if _node.parm("texturefilter") else ""


def collect_json_files(path):
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*_centroids.json")))
    else:
        files = [path]
    if TEXTURE_FILTER:
        needle = TEXTURE_FILTER.casefold()
        files = [f for f in files if needle in os.path.basename(f).casefold()]
    if not files:
        raise hou.NodeError("No *_centroids.json found at: {}".format(path))
    return files


def build_window_points():
    geo = hou.pwd().geometry()
    if geo is None:
        raise hou.NodeError("Run this code inside a Python SOP within a Geometry network, not at /obj level.")
    geo.clear()

    # Spec 5.1.  width/height are normalised sizes, not corrected centres,
    # so they are a plain division with no pixel-centre offset.
    width_uv_attribute = geo.addAttrib(hou.attribType.Point, "width_uv", 0.0)
    height_uv_attribute = geo.addAttrib(hou.attribType.Point, "height_uv", 0.0)
    width_px_attribute = geo.addAttrib(hou.attribType.Point, "width_px", 0.0)
    height_px_attribute = geo.addAttrib(hou.attribType.Point, "height_px", 0.0)
    id_attribute = geo.addAttrib(hou.attribType.Point, "window_id", 0)
    area_attribute = geo.addAttrib(hou.attribType.Point, "window_area", 0.0)
    pixel_attribute = geo.addAttrib(hou.attribType.Point, "window_pixel_centroid", (0.0, 0.0))
    # Keeps the UV coordinate available after the projection overwrites P.
    uv_attribute = geo.addAttrib(hou.attribType.Point, "uv", (0.0, 0.0, 0.0))
    texture_attribute = geo.addAttrib(hou.attribType.Point, "texture_name", "")
    # Several textures share the 0..1 UV range, so split on this before
    # projecting when more than one building is loaded at a time.
    index_attribute = geo.addAttrib(hou.attribType.Point, "texture_index", 0)

    for texture_index, json_file in enumerate(collect_json_files(JSON_PATH)):
        with open(json_file, encoding="utf-8") as handle:
            data = json.load(handle)
        texture_name = data.get("source_image", os.path.basename(json_file))
        image_width = float(data["image_width"])
        image_height = float(data["image_height"])

        for window in data["windows"]:
            # centroid_uv already carries the +0.5 pixel-centre offset and the
            # V flip; do not apply either again here.
            uv = window["centroid_uv"]
            bbox = window["bbox"]
            point = geo.createPoint()
            point.setPosition(hou.Vector3(uv["u"], uv["v"], 0.0))
            point.setAttribValue(uv_attribute, (uv["u"], uv["v"], 0.0))
            point.setAttribValue(width_uv_attribute, bbox["width"] / image_width)
            point.setAttribValue(height_uv_attribute, bbox["height"] / image_height)
            point.setAttribValue(width_px_attribute, float(bbox["width"]))
            point.setAttribValue(height_px_attribute, float(bbox["height"]))
            point.setAttribValue(id_attribute, window["id"])
            point.setAttribValue(area_attribute, float(window["area_pixels"]))
            pixel = window["centroid_pixel"]
            point.setAttribValue(pixel_attribute, (pixel["x"], pixel["y"]))
            point.setAttribValue(texture_attribute, texture_name)
            point.setAttribValue(index_attribute, texture_index)


build_window_points()
