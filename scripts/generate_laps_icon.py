#!/usr/bin/env python3
"""Generate the Laps Icon Composer source document from measured geometry."""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString, shape
from shapely.ops import transform


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GEOMETRY = PROJECT_ROOT / "geometry" / "running-line-1m-visual-candidate.geojson"
DEFAULT_OUTPUT = PROJECT_ROOT / "apps" / "Laps" / "Assets" / "AppIcon" / "Laps.icon"


def load_line(path: Path) -> LineString:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    for feature in collection["features"]:
        geometry = shape(feature["geometry"])
        if isinstance(geometry, LineString):
            return geometry
    raise ValueError(f"No LineString feature found in {path}")


def transform_geometry(geometry: Any, src_crs: Any, dst_crs: Any) -> Any:
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return transform(transformer.transform, geometry)


def closed_line(line: LineString) -> LineString:
    coords = list(line.coords)
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return LineString(coords)


def resample_closed(line: LineString, count: int) -> np.ndarray:
    line = closed_line(line)
    return np.array(
        [line.interpolate(line.length * index / count).coords[0] for index in range(count)],
        dtype=float,
    )


def circular_gaussian(coords: np.ndarray, sigma: float, radius: int) -> np.ndarray:
    offsets = np.arange(-radius, radius + 1, dtype=float)
    weights = np.exp(-(offsets**2) / (2 * sigma**2))
    weights /= weights.sum()
    smoothed = np.zeros_like(coords)
    for offset, weight in zip(offsets.astype(int), weights, strict=True):
        smoothed += np.roll(coords, offset, axis=0) * weight
    return smoothed


def normalize_points(
    coords: np.ndarray,
    canvas: int,
    padding: float,
    rotate_degrees: float,
    optical_offset: tuple[float, float],
) -> np.ndarray:
    coords = coords - coords.mean(axis=0)
    radians = math.radians(rotate_degrees)
    rotation = np.array(
        [
            [math.cos(radians), -math.sin(radians)],
            [math.sin(radians), math.cos(radians)],
        ]
    )
    coords = coords @ rotation.T
    mins = coords.min(axis=0)
    maxs = coords.max(axis=0)
    scale = (canvas - 2 * padding) / max(float((maxs - mins).max()), 1e-9)
    coords = (coords - (mins + maxs) / 2) * scale
    coords[:, 0] += canvas / 2 + optical_offset[0]
    coords[:, 1] = canvas / 2 - coords[:, 1] + optical_offset[1]
    return coords


def catmull_rom_path(points: np.ndarray, tension: float = 0.72) -> str:
    points = np.asarray(points, dtype=float)
    commands = [f"M {points[0, 0]:.2f} {points[0, 1]:.2f}"]
    count = len(points)
    for index in range(count):
        p0 = points[(index - 1) % count]
        p1 = points[index]
        p2 = points[(index + 1) % count]
        p3 = points[(index + 2) % count]
        c1 = p1 + (p2 - p0) * tension / 6
        c2 = p2 - (p3 - p1) * tension / 6
        commands.append(
            "C "
            f"{c1[0]:.2f} {c1[1]:.2f}, "
            f"{c2[0]:.2f} {c2[1]:.2f}, "
            f"{p2[0]:.2f} {p2[1]:.2f}"
        )
    commands.append("Z")
    return " ".join(commands)


def svg_document(body: str) -> str:
    return f"""<svg width="1024" height="1024" viewBox="0 0 1024 1024" fill="none" xmlns="http://www.w3.org/2000/svg">
{body}
</svg>
"""


def background_svg(appearance: str) -> str:
    if appearance == "dark":
        stops = [
            ("#0B2A36", "0"),
            ("#143D42", "0.46"),
            ("#08141F", "1"),
        ]
        light_opacity = "0.18"
        field_opacity = "0.14"
        depth_opacity = "0.38"
    elif appearance == "tinted":
        stops = [
            ("#EEF3F2", "0"),
            ("#CAD8D7", "0.52"),
            ("#8EA1A4", "1"),
        ]
        light_opacity = "0.26"
        field_opacity = "0.16"
        depth_opacity = "0.16"
    else:
        stops = [
            ("#F3F7EC", "0"),
            ("#70C9B0", "0.48"),
            ("#0B7188", "1"),
        ]
        light_opacity = "0.42"
        field_opacity = "0.20"
        depth_opacity = "0.24"

    return svg_document(
        f"""  <defs>
    <linearGradient id="bg" x1="118" y1="70" x2="922" y2="950" gradientUnits="userSpaceOnUse">
      <stop stop-color="{stops[0][0]}" offset="{stops[0][1]}"/>
      <stop stop-color="{stops[1][0]}" offset="{stops[1][1]}"/>
      <stop stop-color="{stops[2][0]}" offset="{stops[2][1]}"/>
    </linearGradient>
    <radialGradient id="upperLight" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(282 190) rotate(42) scale(650 510)">
      <stop stop-color="#FFFFFF" stop-opacity="{light_opacity}"/>
      <stop offset="0.64" stop-color="#FFFFFF" stop-opacity="0.07"/>
      <stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="field" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(374 682) rotate(-17) scale(620 290)">
      <stop stop-color="#D9E9C4" stop-opacity="{field_opacity}"/>
      <stop offset="1" stop-color="#D9E9C4" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="lowerDepth" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(826 846) rotate(28) scale(470 420)">
      <stop stop-color="#021923" stop-opacity="{depth_opacity}"/>
      <stop offset="1" stop-color="#021923" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1024" height="1024" fill="url(#bg)"/>
  <rect width="1024" height="1024" fill="url(#upperLight)"/>
  <rect width="1024" height="1024" fill="url(#field)"/>
  <rect width="1024" height="1024" fill="url(#lowerDepth)"/>"""
    )


def route_base_svg(path_d: str, appearance: str) -> str:
    if appearance == "dark":
        outer = "#DDF7EF"
        mid_a = "#77D8C3"
        mid_b = "#2D91A4"
        center = "#174A55"
        edge = "#F8FFFC"
        shadow = "#001118"
        shadow_opacity = "0.30"
    elif appearance == "tinted":
        outer = "#F4F7F7"
        mid_a = "#879EA0"
        mid_b = "#596C73"
        center = "#D9E0E0"
        edge = "#FFFFFF"
        shadow = "#38484D"
        shadow_opacity = "0.20"
    else:
        outer = "#F3FFF8"
        mid_a = "#A9F0D8"
        mid_b = "#1D8C9D"
        center = "#DFFBF0"
        edge = "#FFFFFF"
        shadow = "#073443"
        shadow_opacity = "0.22"

    return svg_document(
        f"""  <defs>
    <linearGradient id="routeCore" x1="224" y1="184" x2="820" y2="826" gradientUnits="userSpaceOnUse">
      <stop stop-color="{mid_a}"/>
      <stop offset="0.55" stop-color="{center}"/>
      <stop offset="1" stop-color="{mid_b}"/>
    </linearGradient>
  </defs>
  <path d="{path_d}" stroke="{shadow}" stroke-opacity="{shadow_opacity}" stroke-width="138" stroke-linecap="round" stroke-linejoin="round" transform="translate(0 18)"/>
  <path d="{path_d}" stroke="{outer}" stroke-width="112" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{path_d}" stroke="url(#routeCore)" stroke-width="74" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{path_d}" stroke="{edge}" stroke-opacity="0.58" stroke-width="10" stroke-linecap="round" stroke-linejoin="round" transform="translate(-7 -9)"/>"""
    )


def route_lane_svg(path_d: str, appearance: str) -> str:
    if appearance == "dark":
        lane = "#0E3B45"
        line = "#A9F8E1"
        opacity = "0.72"
    elif appearance == "tinted":
        lane = "#60757A"
        line = "#F9FFFF"
        opacity = "0.66"
    else:
        lane = "#127B76"
        line = "#EFFFF8"
        opacity = "0.72"

    return svg_document(
        f"""  <path d="{path_d}" stroke="{lane}" stroke-opacity="{opacity}" stroke-width="26" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="{path_d}" stroke="{line}" stroke-opacity="0.74" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>"""
    )


def route_glint_svg(path_d: str, appearance: str) -> str:
    opacity = "0.72" if appearance != "dark" else "0.58"
    return svg_document(
        f"""  <path d="{path_d}" stroke="#FFFFFF" stroke-opacity="{opacity}" stroke-width="15" stroke-linecap="round" stroke-linejoin="round" transform="translate(-10 -13)"/>"""
    )


def icon_json() -> dict[str, Any]:
    return {
        "fill": "system-light",
        "groups": [
            {
                "name": "Laps Icon",
                "shadow": {
                    "kind": "layer-color",
                    "opacity": 0.34,
                },
                "specular": True,
                "translucency": {
                    "enabled": True,
                    "value": 0.10,
                },
                "blur-material": None,
                "layers": [
                    {
                        "name": "Route Glint",
                        "glass": True,
                        "hidden": False,
                        "image-name-specializations": [
                            {"value": "04-route-glint.svg"},
                            {"appearance": "dark", "value": "04-route-glint-dark.svg"},
                            {"appearance": "tinted", "value": "04-route-glint-tinted.svg"},
                        ],
                        "position": {"scale": 1, "translation-in-points": [0, 0]},
                    },
                    {
                        "name": "Route Lane",
                        "glass": False,
                        "hidden": False,
                        "image-name-specializations": [
                            {"value": "03-route-lane.svg"},
                            {"appearance": "dark", "value": "03-route-lane-dark.svg"},
                            {"appearance": "tinted", "value": "03-route-lane-tinted.svg"},
                        ],
                        "position": {"scale": 1, "translation-in-points": [0, 0]},
                    },
                    {
                        "name": "Measured Loop",
                        "glass": True,
                        "hidden": False,
                        "image-name-specializations": [
                            {"value": "02-route-base.svg"},
                            {"appearance": "dark", "value": "02-route-base-dark.svg"},
                            {"appearance": "tinted", "value": "02-route-base-tinted.svg"},
                        ],
                        "position": {"scale": 1, "translation-in-points": [0, 0]},
                    },
                    {
                        "name": "Background",
                        "glass": False,
                        "hidden": False,
                        "image-name-specializations": [
                            {"value": "01-background.svg"},
                            {"appearance": "dark", "value": "01-background-dark.svg"},
                            {"appearance": "tinted", "value": "01-background-tinted.svg"},
                        ],
                        "position": {"scale": 1, "translation-in-points": [0, 0]},
                    },
                ],
            }
        ],
        "supported-platforms": {
            "circles": ["watchOS"],
            "squares": "shared",
        },
    }


def write_icon_document(output: Path, path_d: str) -> None:
    if output.exists():
        shutil.rmtree(output)
    assets = output / "Assets"
    assets.mkdir(parents=True)

    for appearance, suffix in [("default", ""), ("dark", "-dark"), ("tinted", "-tinted")]:
        (assets / f"01-background{suffix}.svg").write_text(background_svg(appearance), encoding="utf-8")
        (assets / f"02-route-base{suffix}.svg").write_text(route_base_svg(path_d, appearance), encoding="utf-8")
        (assets / f"03-route-lane{suffix}.svg").write_text(route_lane_svg(path_d, appearance), encoding="utf-8")
        (assets / f"04-route-glint{suffix}.svg").write_text(route_glint_svg(path_d, appearance), encoding="utf-8")

    (output / "icon.json").write_text(json.dumps(icon_json(), indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geometry", type=Path, default=DEFAULT_GEOMETRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--measurement-crs", default="EPSG:32115")
    parser.add_argument("--samples", type=int, default=58)
    parser.add_argument("--smooth-sigma", type=float, default=1.15)
    parser.add_argument("--smooth-radius", type=int, default=4)
    parser.add_argument("--padding", type=float, default=118)
    parser.add_argument("--rotate-degrees", type=float, default=-1.5)
    parser.add_argument("--offset-x", type=float, default=-2)
    parser.add_argument("--offset-y", type=float, default=-8)
    args = parser.parse_args()

    line_4326 = load_line(args.geometry)
    line_measure = transform_geometry(line_4326, "EPSG:4326", args.measurement_crs)
    coords = resample_closed(line_measure, args.samples)
    coords = circular_gaussian(coords, args.smooth_sigma, args.smooth_radius)
    points = normalize_points(
        coords,
        canvas=1024,
        padding=args.padding,
        rotate_degrees=args.rotate_degrees,
        optical_offset=(args.offset_x, args.offset_y),
    )
    path_d = catmull_rom_path(points)
    write_icon_document(args.output, path_d)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
