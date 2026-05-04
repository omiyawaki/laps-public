#!/usr/bin/env python3
"""Generate app track thumbnails from ortho imagery and measured geometry."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageEnhance
from pyproj import Transformer
from rasterio.windows import from_bounds
from shapely.geometry import LineString, shape
from shapely.ops import transform as transform_geometry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ORTHO = PROJECT_ROOT / "data" / "interim" / "ortho" / "example-2024-loop-tight.tif"
DEFAULT_GEOMETRY = PROJECT_ROOT / "geometry" / "running-line-1m-visual-candidate.geojson"
DEFAULT_OUTPUT = PROJECT_ROOT / "apps" / "Laps" / "Sources" / "Laps" / "Resources" / "TrackThumbnails" / "example-loop-1m.png"


def load_line_feature(path: Path) -> LineString:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    for feature in collection["features"]:
        geometry = shape(feature["geometry"])
        if isinstance(geometry, LineString):
            return geometry
    raise ValueError(f"no LineString feature found in {path}")


def expanded_bounds(bounds: tuple[float, float, float, float], margin: float, aspect_ratio: float) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = bounds
    minx -= margin
    miny -= margin
    maxx += margin
    maxy += margin
    width = maxx - minx
    height = maxy - miny
    current_ratio = width / height
    if current_ratio < aspect_ratio:
        extra = (height * aspect_ratio - width) / 2.0
        minx -= extra
        maxx += extra
    elif current_ratio > aspect_ratio:
        extra = (width / aspect_ratio - height) / 2.0
        miny -= extra
        maxy += extra
    return minx, miny, maxx, maxy


def read_crop(dataset: rasterio.io.DatasetReader, bounds: tuple[float, float, float, float]) -> tuple[np.ndarray, rasterio.Affine]:
    raster_bounds = dataset.bounds
    minx, miny, maxx, maxy = bounds
    minx = max(minx, raster_bounds.left)
    miny = max(miny, raster_bounds.bottom)
    maxx = min(maxx, raster_bounds.right)
    maxy = min(maxy, raster_bounds.top)
    window = from_bounds(minx, miny, maxx, maxy, dataset.transform).round_offsets().round_lengths()
    array = dataset.read([1, 2, 3], window=window)
    transform = dataset.window_transform(window)
    return np.moveaxis(array, 0, -1), transform


def line_pixels(line: LineString, transform: rasterio.Affine, max_step: float = 1.0) -> list[tuple[float, float]]:
    point_count = max(2, int(math.ceil(line.length / max_step)))
    inverse = ~transform
    pixels = []
    for index in range(point_count + 1):
        point = line.interpolate(line.length * index / point_count)
        col, row = inverse * (point.x, point.y)
        pixels.append((float(col), float(row)))
    return pixels


def render_thumbnail(
    image_array: np.ndarray,
    line_points: list[tuple[float, float]],
    output_size: tuple[int, int],
) -> Image.Image:
    image = Image.fromarray(image_array, mode="RGB")
    image = ImageEnhance.Color(image).enhance(1.08)
    image = ImageEnhance.Contrast(image).enhance(1.08)
    scale_x = output_size[0] / image.width
    scale_y = output_size[1] / image.height
    image = image.resize(output_size, Image.Resampling.LANCZOS).convert("RGBA")
    scaled_line = [(x * scale_x, y * scale_y) for x, y in line_points]

    overlay = Image.new("RGBA", output_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.line(scaled_line, fill=(18, 24, 28, 190), width=15, joint="curve")
    draw.line(scaled_line, fill=(255, 205, 64, 255), width=10, joint="curve")
    draw.line(scaled_line, fill=(38, 207, 236, 230), width=3, joint="curve")
    return Image.alpha_composite(image, overlay).convert("RGB")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ortho", type=Path, default=DEFAULT_ORTHO)
    parser.add_argument("--geometry", type=Path, default=DEFAULT_GEOMETRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--margin-units", type=float, default=34.0, help="Crop margin in source raster units.")
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=500)
    args = parser.parse_args()

    line_4326 = load_line_feature(args.geometry)
    with rasterio.open(args.ortho) as dataset:
        to_ortho = Transformer.from_crs("EPSG:4326", dataset.crs, always_xy=True)
        line_ortho = transform_geometry(to_ortho.transform, line_4326)
        crop_bounds = expanded_bounds(line_ortho.bounds, args.margin_units, args.width / args.height)
        image_array, crop_transform = read_crop(dataset, crop_bounds)
        points = line_pixels(line_ortho, crop_transform)

    thumbnail = render_thumbnail(image_array, points, (args.width, args.height))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    thumbnail.save(args.output, optimize=True)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()

