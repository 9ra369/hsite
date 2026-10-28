"""Extract individual window regions and their centroids from building textures.

Accepts either a single image or a folder.  When a folder is given, every image
whose file name contains the --match string (default "basecolor") is processed
and one mask/JSON pair is written per texture.

Use --seed to sample the window glass/material with x,y,width,height.  This is
recommended for glass detection because window colours vary substantially
between textures.
"""
from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".tga", ".webp"}


def imread_unicode(path: Path) -> np.ndarray | None:
    """cv2.imread cannot open non-ASCII paths on Windows; decode from bytes."""
    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if not buffer.size:
        return None
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def imwrite_unicode(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    success, buffer = cv2.imencode(path.suffix or ".png", image)
    if not success:
        raise OSError(f"Cannot encode image: {path}")
    buffer.tofile(str(path))


def rectangle(value: str) -> tuple[int, int, int, int]:
    try:
        x, y, width, height = (int(v) for v in value.split(","))
        if width <= 0 or height <= 0:
            raise ValueError
        return x, y, width, height
    except ValueError as exc:
        raise argparse.ArgumentTypeError("seed must be x,y,width,height") from exc


def material_mask(image: np.ndarray, seed: tuple[int, int, int, int] | None, glass_colour: str) -> np.ndarray:
    """Classify sampled glass, or detect dark/cyan glass in the automatic modes."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float32)
    if seed:
        x, y, width, height = seed
        sample = lab[max(0, y):y + height, max(0, x):x + width].reshape(-1, 3)
        if not len(sample):
            raise ValueError("seed lies outside the input image")
        centre = np.median(sample, axis=0)
        distance = np.linalg.norm(sample - centre, axis=1)
        tolerance = max(10.0, float(np.percentile(distance, 90)) * 1.35)
        return (np.linalg.norm(lab - centre, axis=2) <= tolerance).astype(np.uint8) * 255

    # OpenCV hue is 0--179.  Cyan/light-blue glazing is typically 80--112;
    # retaining saturation avoids selecting grey concrete under blue lighting.
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    dark = hsv[:, :, 2] < 105
    cyan = (hsv[:, :, 0] >= 78) & (hsv[:, :, 0] <= 115) & (hsv[:, :, 1] >= 35) & (hsv[:, :, 2] >= 65)
    if glass_colour == "dark":
        candidate = dark
    elif glass_colour == "cyan":
        candidate = cyan
    else:
        candidate = dark | cyan
    return candidate.astype(np.uint8) * 255


def white_frame_mask(image: np.ndarray) -> np.ndarray:
    """Return a cleaned binary mask of bright, low-saturation window frames."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # A white frame is bright but has little saturation.  Closing/dilating makes
    # anti-aliased frame edges continuous before looking for enclosed holes.
    frame = ((hsv[:, :, 1] <= 50) & (hsv[:, :, 2] >= 180)).astype(np.uint8) * 255
    frame = cv2.morphologyEx(frame, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return cv2.dilate(frame, np.ones((3, 3), np.uint8), iterations=1)


def white_frame_interiors(frame: np.ndarray) -> np.ndarray:
    """Return regions fully enclosed by the supplied binary white-frame mask."""
    non_frame = cv2.bitwise_not(frame)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(non_frame, 8)
    enclosed = np.zeros_like(frame)
    height, width = frame.shape
    for label in range(1, count):
        x, y, region_width, region_height, _ = stats[label]
        # The background/wall outside a frame reaches an image edge; discard it.
        touches_edge = x == 0 or y == 0 or x + region_width == width or y + region_height == height
        if not touches_edge:
            enclosed[labels == label] = 255
    return enclosed


def extract(mask: np.ndarray, min_area: int, max_area: int) -> tuple[np.ndarray, list[dict[str, object]]]:
    # Closing fills small texture holes; opening separates isolated pixels/noise.
    clean = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count, labels, stats, centres = cv2.connectedComponentsWithStats(clean, 8)
    output = np.zeros_like(mask)
    windows: list[dict[str, object]] = []
    for label in range(1, count):
        x, y, width, height, area = (int(v) for v in stats[label])
        if not min_area <= area <= max_area:
            continue
        # Ignore extremely thin fragments such as mullions and texture seams.
        ratio = min(width, height) / max(width, height)
        if ratio < 0.08:
            continue
        cx, cy = centres[label]
        output[labels == label] = 255
        windows.append({
            "id": len(windows),
            # Image-centred and UV coordinates are added before JSON output.
            "centroid_pixel": {"x": round(float(cx), 3), "y": round(float(cy), 3)},
            "bbox": {"x": x, "y": y, "width": width, "height": height},
            "area_pixels": area,
        })
    return output, windows


def annotate_coordinates(windows: list[dict[str, object]], image_width: int, image_height: int) -> None:
    """Add image-centred pixel coordinates and UV coordinates to each window."""
    for window in windows:
        pixel = window["centroid_pixel"]
        # Texture pixels use down-positive Y; centroid uses X-right/Y-up.
        window["centroid"] = {
            "x": round(pixel["x"] - image_width / 2.0, 3),
            "y": round(image_height / 2.0 - pixel["y"], 3),
        }
        # UV: +0.5 moves from pixel index to pixel centre, and V is flipped
        # because textures are stored top-down while UV space is bottom-up.
        window["centroid_uv"] = {
            "u": round((pixel["x"] + 0.5) / image_width, 6),
            "v": round(1.0 - (pixel["y"] + 0.5) / image_height, 6),
        }
        bbox = window["bbox"]
        window["size_uv"] = {
            "u": round(bbox["width"] / image_width, 6),
            "v": round(bbox["height"] / image_height, 6),
        }


def collect_images(root: Path, match: str, recursive: bool) -> list[Path]:
    """Return the textures to process; a file input is always accepted as-is."""
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise FileNotFoundError(f"No such file or folder: {root}")
    paths = (root.rglob("*") if recursive else root.glob("*"))
    files = [p for p in paths if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]
    if match:
        needle = match.casefold()
        files = [p for p in files if needle in p.name.casefold()]
    return sorted(files)


def draw_preview(image: np.ndarray, windows: list[dict[str, object]]) -> np.ndarray:
    preview = image.copy()
    for window in windows:
        bbox = window["bbox"]
        x, y, width, height = bbox["x"], bbox["y"], bbox["width"], bbox["height"]
        cx, cy = window["centroid_pixel"]["x"], window["centroid_pixel"]["y"]
        cv2.rectangle(preview, (x, y), (x + width, y + height), (0, 255, 0), 1)
        cv2.circle(preview, (round(cx), round(cy)), 3, (0, 0, 255), -1)
        cv2.putText(preview, str(window["id"]), (x, max(12, y - 3)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
    return preview


def process_image(path: Path, out_dir: Path, args: argparse.Namespace) -> dict[str, object]:
    """Run the full extraction for one texture and write its outputs."""
    image = imread_unicode(path)
    if image is None:
        raise OSError(f"Cannot read image: {path}")
    image_height, image_width = image.shape[:2]

    need_frame = args.detection == "white-frame" or args.frame_preview
    frame = white_frame_mask(image) if need_frame else None
    if args.frame_preview and frame is not None:
        imwrite_unicode(out_dir / f"{path.stem}_frames.png", frame)

    candidate = (white_frame_interiors(frame) if args.detection == "white-frame"
                 else material_mask(image, args.seed, args.glass_colour))
    mask, windows = extract(candidate, args.min_area, args.max_area)
    annotate_coordinates(windows, image_width, image_height)

    mask_path = out_dir / f"{path.stem}_mask.png"
    imwrite_unicode(mask_path, mask)
    if args.debug:
        imwrite_unicode(out_dir / f"{path.stem}_preview.png", draw_preview(image, windows))

    data = {
        "source_image": path.name,
        "source_path": str(path),
        "image_width": image_width,
        "image_height": image_height,
        "detection": args.detection,
        "coordinate_system": {
            "origin": "image_center",
            "units": "pixels",
            "x_axis": "right",
            "y_axis": "up",
        },
        "uv_coordinate_system": {
            "origin": "bottom_left",
            "note": "pixel centre offset applied, V flipped for UV space",
        },
        "windows": windows,
    }
    json_path = out_dir / f"{path.stem}_centroids.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    return {
        "source_image": path.name,
        "source_path": str(path),
        "mask": mask_path.name,
        "centroids": json_path.name,
        "image_width": image_width,
        "image_height": image_height,
        "window_count": len(windows),
    }


EVENT_PREFIX = "@@"


def run(args: argparse.Namespace, log: Callable[[str], None] = print) -> list[dict[str, object]]:
    """Process every matching texture.  Returns one result entry per success."""
    if args.min_area < 1 or args.max_area < args.min_area:
        raise ValueError("area range is invalid")
    root = Path(args.input)
    out_dir = Path(args.out_dir)

    def event(payload: dict) -> None:
        # A front-end reads these lines; humans read the plain ones.
        if getattr(args, "events", False):
            log(EVENT_PREFIX + json.dumps(payload, ensure_ascii=False))

    images = collect_images(root, args.match, args.recursive)
    if not images:
        log(f"No matching image in {root} (match='{args.match}').")
        event({"event": "start", "total": 0, "files": []})
        event({"event": "end", "ok": 0, "total": 0, "windows": 0})
        return []

    log(f"{len(images)} image(s) to process -> {out_dir}")
    event({"event": "start", "total": len(images), "files": [p.name for p in images],
           "out_dir": str(out_dir)})
    results: list[dict[str, object]] = []
    for index, path in enumerate(images, start=1):
        event({"event": "begin", "index": index, "name": path.name})
        try:
            result = process_image(path, out_dir, args)
        except Exception as exc:  # keep the batch alive when one texture fails
            log(f"[{index}/{len(images)}] FAILED {path.name}: {exc}")
            event({"event": "item", "index": index, "name": path.name,
                   "status": "fail", "message": str(exc)})
            if args.traceback:
                log(traceback.format_exc())
            continue
        results.append(result)
        log(f"[{index}/{len(images)}] {path.name}: {result['window_count']} windows")
        event({"event": "item", "index": index, "name": path.name, "status": "ok",
               "windows": result["window_count"], "centroids": result["centroids"],
               "image_width": result["image_width"], "image_height": result["image_height"]})

    if results:
        index_path = out_dir / "windows_index.json"
        index_path.write_text(json.dumps({"textures": results}, indent=2), encoding="utf-8")
        log(f"Wrote {index_path}")
    total = sum(int(r["window_count"]) for r in results)
    log(f"Done: {len(results)}/{len(images)} textures, {total} windows total.")
    event({"event": "end", "ok": len(results), "total": len(images), "windows": total,
           "out_dir": str(out_dir)})
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="texture file, or folder containing textures")
    parser.add_argument("--out-dir", type=Path, default=Path("output/windows"),
                        help="folder for masks, JSON and previews (default: output/windows)")
    parser.add_argument("--match", default="basecolor",
                        help="only process files whose name contains this text; "
                             "pass an empty string to process every image (default: basecolor)")
    parser.add_argument("--recursive", action="store_true", help="also search sub-folders")
    parser.add_argument("--seed", type=rectangle, help="window-material sample: x,y,width,height")
    parser.add_argument("--glass-colour", choices=("auto", "cyan", "dark"), default="auto",
                        help="glass detection only; default detects dark and cyan/light-blue glass")
    parser.add_argument("--detection", choices=("white-frame", "glass"), default="white-frame",
                        help="white-frame uses enclosed white borders; glass uses colour/seed classification")
    parser.add_argument("--min-area", type=int, default=20)
    parser.add_argument("--max-area", type=int, default=100000)
    parser.add_argument("--debug", action="store_true",
                        help="also write <stem>_preview.png with centroid labels")
    parser.add_argument("--frame-preview", action="store_true",
                        help="also write <stem>_frames.png with the detected white frames")
    parser.add_argument("--traceback", action="store_true", help="print full tracebacks on failure")
    parser.add_argument("--events", action="store_true",
                        help="also print machine-readable '@@{...}' progress lines for a front-end")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        run(args)
    except (ValueError, FileNotFoundError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
