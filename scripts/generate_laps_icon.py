#!/usr/bin/env python3
"""Generate the Laps macOS icon from the measured loop geometry."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from pyproj import Transformer
from shapely.geometry import LineString, shape
from shapely.ops import transform


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GEOMETRY = PROJECT_ROOT / "geometry" / "running-line-1m-visual-candidate.geojson"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "apps" / "Laps" / "Assets" / "AppIcon"


def load_line(path: Path) -> LineString:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    for feature in collection["features"]:
        geometry = shape(feature["geometry"])
        if isinstance(geometry, LineString):
            return geometry
    raise ValueError(f"no LineString feature found in {path}")


def transform_geometry(geometry: Any, src_crs: Any, dst_crs: Any) -> Any:
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return transform(transformer.transform, geometry)


def rounded_rect_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def vertical_gradient(size: int, top_rgb: tuple[int, int, int], bottom_rgb: tuple[int, int, int]) -> Image.Image:
    gradient = np.zeros((size, size, 3), dtype=np.uint8)
    for row in range(size):
        t = row / max(size - 1, 1)
        rgb = [round((1.0 - t) * top_rgb[index] + t * bottom_rgb[index]) for index in range(3)]
        gradient[row, :, :] = rgb
    return Image.fromarray(gradient, mode="RGB").convert("RGBA")


def radial_overlay(
    size: int,
    center_xy: tuple[float, float],
    radius: float,
    rgba: tuple[int, int, int, int],
    power: float = 1.8,
) -> Image.Image:
    y, x = np.ogrid[:size, :size]
    cx, cy = center_xy
    distance = np.sqrt((x - cx * size) ** 2 + (y - cy * size) ** 2)
    strength = np.clip(1.0 - distance / (radius * size), 0.0, 1.0) ** power
    overlay = np.zeros((size, size, 4), dtype=np.uint8)
    overlay[..., 0] = rgba[0]
    overlay[..., 1] = rgba[1]
    overlay[..., 2] = rgba[2]
    overlay[..., 3] = np.clip(strength * rgba[3], 0, 255).astype(np.uint8)
    return Image.fromarray(overlay, mode="RGBA")


def chaikin_closed(coords: np.ndarray, iterations: int) -> np.ndarray:
    smoothed = coords
    for _ in range(max(0, iterations)):
        next_coords = np.roll(smoothed, -1, axis=0)
        q = 0.75 * smoothed + 0.25 * next_coords
        r = 0.25 * smoothed + 0.75 * next_coords
        smoothed = np.empty((smoothed.shape[0] * 2, 2), dtype=float)
        smoothed[0::2] = q
        smoothed[1::2] = r
    return smoothed


def path_points(
    line: LineString,
    canvas_size: int,
    padding: int,
    simplify_m: float,
    smooth_iterations: int,
    rotate_deg: float,
) -> list[tuple[int, int]]:
    simplified = line.simplify(simplify_m, preserve_topology=True)
    coords = np.asarray(simplified.coords, dtype=float)
    if np.allclose(coords[0], coords[-1]):
        coords = coords[:-1]
    coords = chaikin_closed(coords, smooth_iterations)

    center = coords.mean(axis=0)
    coords -= center
    if abs(rotate_deg) > 1e-9:
        radians = math.radians(rotate_deg)
        rotation = np.array([[math.cos(radians), -math.sin(radians)], [math.sin(radians), math.cos(radians)]])
        coords = coords @ rotation.T

    mins = coords.min(axis=0)
    maxs = coords.max(axis=0)
    span = np.maximum(maxs - mins, 1e-9)
    scale = (canvas_size - 2.0 * padding) / float(max(span))
    coords = (coords - (mins + maxs) / 2.0) * scale + canvas_size / 2.0
    coords[:, 1] = canvas_size - coords[:, 1]
    return [(int(round(x)), int(round(y))) for x, y in coords]


def draw_line(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], fill: tuple[int, int, int, int], width: int) -> None:
    closed = points + [points[0]]
    try:
        draw.line(closed, fill=fill, width=width, joint="curve")
    except TypeError:
        draw.line(closed, fill=fill, width=width)
    radius = width / 2.0
    for x, y in points:
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def squircle_background(size: int, top_rgb: tuple[int, int, int], bottom_rgb: tuple[int, int, int]) -> Image.Image:
    scale = size / 1024.0
    radius = round(228 * scale)
    background = vertical_gradient(size, top_rgb, bottom_rgb)
    background.alpha_composite(radial_overlay(size, (0.24, 0.16), 0.65, (255, 255, 255, 64), power=2.2))
    background.alpha_composite(radial_overlay(size, (0.84, 0.88), 0.75, (0, 26, 34, 90), power=1.6))
    background.alpha_composite(radial_overlay(size, (0.18, 0.92), 0.55, (34, 207, 149, 30), power=1.8))
    mask = rounded_rect_mask(size, radius)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(Image.composite(background, Image.new("RGBA", (size, size), (0, 0, 0, 0)), mask))
    return canvas


def draw_blurred_line(
    base: Image.Image,
    points: list[tuple[int, int]],
    fill: tuple[int, int, int, int],
    width: int,
    blur: int,
    offset_y: int = 0,
) -> None:
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    shifted = [(x, y + offset_y) for x, y in points]
    draw_line(draw, shifted, fill, width)
    if blur > 0:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(layer)


def render_icon(points: list[tuple[int, int]], size: int, variant: str) -> Image.Image:
    scale = size / 1024.0
    if variant == "liquid-default":
        canvas = squircle_background(size, (38, 204, 153), (13, 111, 136))
    elif variant == "liquid-dark":
        canvas = squircle_background(size, (10, 74, 78), (3, 30, 50))
    elif variant == "liquid-clear-light":
        canvas = squircle_background(size, (226, 246, 240), (178, 218, 224))
    elif variant == "liquid-clear-dark":
        canvas = squircle_background(size, (35, 78, 78), (8, 35, 51))
    elif variant == "liquid-tinted-light":
        canvas = squircle_background(size, (225, 232, 235), (175, 190, 198))
    elif variant == "liquid-tinted-dark":
        canvas = squircle_background(size, (55, 62, 68), (20, 25, 31))
    elif variant == "native-line":
        canvas = squircle_background(size, (30, 171, 128), (20, 88, 115))
    elif variant == "native-field":
        canvas = squircle_background(size, (39, 145, 93), (13, 83, 75))
    elif variant == "native-dark":
        canvas = squircle_background(size, (20, 108, 91), (7, 44, 60))
    else:
        raise ValueError(f"unknown icon variant: {variant}")

    scaled_points = [(round(x * scale), round(y * scale)) for x, y in points]
    line_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    line_draw = ImageDraw.Draw(line_layer)

    if variant.startswith("liquid"):
        if variant in {"liquid-clear-light", "liquid-tinted-light"}:
            outer = (255, 255, 255, 218)
            inner = (24, 114, 98, 180)
            top = (255, 255, 255, 120)
            ambient = (31, 86, 92, 40)
        elif variant in {"liquid-clear-dark", "liquid-tinted-dark"}:
            outer = (238, 255, 250, 218)
            inner = (127, 230, 204, 165)
            top = (255, 255, 255, 95)
            ambient = (0, 0, 0, 75)
        else:
            outer = (247, 255, 251, 238)
            inner = (172, 255, 232, 150)
            top = (255, 255, 255, 100)
            ambient = (0, 35, 44, 58)
        draw_blurred_line(canvas, scaled_points, ambient, max(1, round(76 * scale)), max(1, round(24 * scale)), round(20 * scale))
        draw_line(line_draw, scaled_points, outer, max(1, round(64 * scale)))
        draw_line(line_draw, scaled_points, inner, max(1, round(24 * scale)))
        draw_line(line_draw, [(x, y - round(4 * scale)) for x, y in scaled_points], top, max(1, round(8 * scale)))
    elif variant == "native-line":
        draw_blurred_line(canvas, scaled_points, (0, 31, 43, 95), max(1, round(96 * scale)), max(1, round(16 * scale)), round(18 * scale))
        draw_line(line_draw, scaled_points, (248, 255, 250, 255), max(1, round(70 * scale)))
        draw_line(line_draw, scaled_points, (170, 255, 229, 150), max(1, round(20 * scale)))
        draw_line(line_draw, [(x, y - round(3 * scale)) for x, y in scaled_points], (255, 255, 255, 92), max(1, round(9 * scale)))
    elif variant == "native-field":
        draw_blurred_line(canvas, scaled_points, (0, 35, 28, 70), max(1, round(104 * scale)), max(1, round(18 * scale)), round(16 * scale))
        draw_line(line_draw, scaled_points, (230, 251, 238, 245), max(1, round(78 * scale)))
        draw_line(line_draw, scaled_points, (51, 180, 124, 255), max(1, round(34 * scale)))
        draw_line(line_draw, scaled_points, (224, 255, 245, 130), max(1, round(10 * scale)))
    elif variant == "native-dark":
        draw_blurred_line(canvas, scaled_points, (0, 16, 24, 115), max(1, round(88 * scale)), max(1, round(15 * scale)), round(16 * scale))
        draw_line(line_draw, scaled_points, (238, 255, 249, 255), max(1, round(60 * scale)))
        draw_line(line_draw, scaled_points, (77, 223, 178, 205), max(1, round(16 * scale)))

    canvas.alpha_composite(line_layer)
    return canvas


def svg_path(points: list[tuple[int, int]]) -> str:
    head, *tail = points
    commands = [f"M {head[0]} {head[1]}"]
    commands.extend(f"L {x} {y}" for x, y in tail)
    commands.append("Z")
    return " ".join(commands)


def write_svg(path: Path, points: list[tuple[int, int]], variant: str) -> None:
    d = svg_path(points)
    if variant == "liquid-default":
        stops = [("#26cc99", "0"), ("#0d6f88", "1")]
        shadow_opacity = "0.22"
        outer = "#f7fffb"
        inner = "#acffe8"
        outer_width = "64"
        inner_width = "24"
        inner_opacity = "0.58"
    elif variant == "liquid-dark":
        stops = [("#0a4a4e", "0"), ("#031e32", "1")]
        shadow_opacity = "0.34"
        outer = "#eefffa"
        inner = "#7fe6cc"
        outer_width = "64"
        inner_width = "24"
        inner_opacity = "0.65"
    elif variant == "liquid-clear-light":
        stops = [("#e2f6f0", "0"), ("#b2dae0", "1")]
        shadow_opacity = "0.16"
        outer = "#ffffff"
        inner = "#187262"
        outer_width = "64"
        inner_width = "24"
        inner_opacity = "0.7"
    elif variant == "liquid-clear-dark":
        stops = [("#234e4e", "0"), ("#082333", "1")]
        shadow_opacity = "0.28"
        outer = "#eefffa"
        inner = "#7fe6cc"
        outer_width = "64"
        inner_width = "24"
        inner_opacity = "0.65"
    elif variant == "liquid-tinted-light":
        stops = [("#e1e8eb", "0"), ("#afbec6", "1")]
        shadow_opacity = "0.16"
        outer = "#ffffff"
        inner = "#3f5862"
        outer_width = "64"
        inner_width = "24"
        inner_opacity = "0.62"
    elif variant == "liquid-tinted-dark":
        stops = [("#373e44", "0"), ("#14191f", "1")]
        shadow_opacity = "0.30"
        outer = "#f3f7f8"
        inner = "#9aa8ad"
        outer_width = "64"
        inner_width = "24"
        inner_opacity = "0.62"
    elif variant == "native-line":
        stops = [("#1eab80", "0"), ("#145873", "1")]
        shadow_opacity = "0.34"
        outer = "#f8fffa"
        inner = "#aaffef"
        outer_width = "70"
        inner_width = "20"
        inner_opacity = "0.58"
    elif variant == "native-field":
        stops = [("#27915d", "0"), ("#0d534b", "1")]
        shadow_opacity = "0.26"
        outer = "#e6fbee"
        inner = "#33b47c"
        outer_width = "78"
        inner_width = "34"
        inner_opacity = "1"
    else:
        stops = [("#146c5b", "0"), ("#072c3c", "1")]
        shadow_opacity = "0.42"
        outer = "#eefff9"
        inner = "#4ddfb2"
        outer_width = "60"
        inner_width = "16"
        inner_opacity = "0.8"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="{stops[0][1]}" stop-color="{stops[0][0]}"/>
      <stop offset="{stops[1][1]}" stop-color="{stops[1][0]}"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="1024" height="1024" rx="228" fill="url(#bg)"/>
  <circle cx="246" cy="164" r="430" fill="#ffffff" opacity="0.13"/>
  <circle cx="850" cy="900" r="520" fill="#001a22" opacity="0.22"/>
  <path d="{d}" fill="none" stroke="#001f2b" stroke-opacity="{shadow_opacity}" stroke-width="76" stroke-linecap="round" stroke-linejoin="round" transform="translate(0 20)"/>
  <path d="{d}" fill="none" stroke="{outer}" stroke-width="{outer_width}" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{d}" fill="none" stroke="{inner}" stroke-opacity="{inner_opacity}" stroke-width="{inner_width}" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""
    path.write_text(svg, encoding="utf-8")


def write_icon_composer_layers(output_dir: Path, points: list[tuple[int, int]]) -> None:
    layers_dir = output_dir / "IconComposerLayers"
    layers_dir.mkdir(parents=True, exist_ok=True)
    d = svg_path(points)
    backgrounds = {
        "background-default.svg": ("#26cc99", "#0d6f88"),
        "background-dark.svg": ("#0a4a4e", "#031e32"),
        "background-clear-light.svg": ("#e2f6f0", "#b2dae0"),
        "background-clear-dark.svg": ("#234e4e", "#082333"),
        "background-tinted-light.svg": ("#e1e8eb", "#afbec6"),
        "background-tinted-dark.svg": ("#373e44", "#14191f"),
    }
    for filename, (top, bottom) in backgrounds.items():
        (layers_dir / filename).write_text(
            f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="{top}"/>
      <stop offset="1" stop-color="{bottom}"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="1024" height="1024" fill="url(#bg)"/>
</svg>
""",
            encoding="utf-8",
        )
    layer_specs = {
        "route-glass-fill.svg": ("#ffffff", "0.88", "64"),
        "route-specular.svg": ("#ffffff", "0.45", "8"),
        "route-tint-mask.svg": ("#000000", "1", "64"),
        "route-inner-mask.svg": ("#000000", "1", "24"),
    }
    for filename, (stroke, opacity, width) in layer_specs.items():
        translate = ' transform="translate(0 -4)"' if filename == "route-specular.svg" else ""
        (layers_dir / filename).write_text(
            f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <path d="{d}" fill="none" stroke="{stroke}" stroke-opacity="{opacity}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"{translate}/>
</svg>
""",
            encoding="utf-8",
        )
    (layers_dir / "README.md").write_text(
        """# Icon Composer Layers

These SVGs are generated from `geometry/running-line-1m-visual-candidate.geojson`.

Suggested Icon Composer setup:

- Background: use the matching `background-*.svg`, or recreate the same
  gradient directly in Icon Composer.
- Foreground group: `route-glass-fill.svg` plus `route-specular.svg`.
- Tinted variants: use `route-tint-mask.svg` as the stable monochrome glyph.
- Keep the route shape consistent across default, dark, clear, and tinted
  appearances; only adjust material, opacity, and background tone.

The flattened `.icns` in the parent folder is only the deploy fallback for this
SwiftPM-built macOS app bundle.
""",
        encoding="utf-8",
    )


def candidate_sheet(output_dir: Path, candidates: list[tuple[str, Image.Image]]) -> Path:
    tile = 256
    label_height = 42
    gap = 22
    sheet_width = len(candidates) * tile + (len(candidates) + 1) * gap
    sheet_height = tile + label_height + 2 * gap
    sheet = Image.new("RGBA", (sheet_width, sheet_height), (246, 248, 250, 255))
    draw = ImageDraw.Draw(sheet)
    for index, (name, image) in enumerate(candidates):
        x = gap + index * (tile + gap)
        y = gap
        sheet.alpha_composite(image.resize((tile, tile), Image.Resampling.LANCZOS), (x, y))
        label = name.replace("native-", "")
        draw.text((x + tile / 2, y + tile + 16), label, fill=(32, 38, 42, 255), anchor="mm")
    path = output_dir / "Laps-icon-candidates.png"
    sheet.convert("RGB").save(path)
    return path


def make_iconset(output_dir: Path, master: Image.Image) -> Path | None:
    iconset = output_dir / "Laps.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for size, filename in sizes:
        image = master.resize((size, size), Image.Resampling.LANCZOS)
        image.save(iconset / filename)

    iconutil = shutil.which("iconutil")
    if iconutil is None:
        return None
    icns = output_dir / "Laps.icns"
    subprocess.run([iconutil, "-c", "icns", "-o", str(icns), str(iconset)], check=True)
    return icns


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geometry", type=Path, default=DEFAULT_GEOMETRY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--measurement-crs", default="EPSG:32115")
    parser.add_argument("--simplify-m", type=float, default=0.45)
    parser.add_argument("--smooth-iterations", type=int, default=3)
    parser.add_argument("--rotate-deg", type=float, default=0.0)
    parser.add_argument("--padding-px", type=int, default=164)
    parser.add_argument(
        "--variant",
        choices=[
            "liquid-default",
            "liquid-dark",
            "liquid-clear-light",
            "liquid-clear-dark",
            "liquid-tinted-light",
            "liquid-tinted-dark",
            "native-line",
            "native-field",
            "native-dark",
        ],
        default="liquid-default",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    line_4326 = load_line(args.geometry)
    line_measure = transform_geometry(line_4326, "EPSG:4326", args.measurement_crs)
    points = path_points(line_measure, 1024, args.padding_px, args.simplify_m, args.smooth_iterations, args.rotate_deg)
    master = render_icon(points, 4096, args.variant)

    preview = args.output_dir / "Laps-1024.png"
    svg = args.output_dir / "Laps.svg"
    master.resize((1024, 1024), Image.Resampling.LANCZOS).save(preview)
    write_svg(svg, points, args.variant)
    icns = make_iconset(args.output_dir, master)

    candidates: list[tuple[str, Image.Image]] = []
    candidates_dir = args.output_dir / "Candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    for variant in ["liquid-default", "liquid-dark", "liquid-clear-light", "liquid-clear-dark", "liquid-tinted-light", "liquid-tinted-dark"]:
        image = render_icon(points, 1024, variant)
        image.save(candidates_dir / f"{variant}.png")
        candidates.append((variant, image))
    sheet = candidate_sheet(args.output_dir, candidates)
    write_icon_composer_layers(args.output_dir, points)

    print(f"wrote {preview}")
    print(f"wrote {svg}")
    print(f"wrote {sheet}")
    if icns is not None:
        print(f"wrote {icns}")
    else:
        print("iconutil not found; skipped .icns generation")


if __name__ == "__main__":
    main()
