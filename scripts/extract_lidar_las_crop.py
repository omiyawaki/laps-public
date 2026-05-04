#!/usr/bin/env python3
"""Extract and render the FEMA 2019 LAS tile around the Example Loop."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import laspy
import matplotlib.pyplot as plt
import numpy as np
import yaml
from pyproj import CRS, Transformer
from scipy import ndimage
from shapely import contains_xy
from shapely.geometry import LineString, Polygon, shape
from shapely.ops import transform

from build_baseline_geometry import line_from_feature, query_loop_centerlines


PROJECT_ROOT = Path(__file__).resolve().parents[1]

CLASS_NAMES = {
    0: "created, never classified",
    1: "unclassified",
    2: "ground",
    3: "low vegetation",
    4: "medium vegetation",
    5: "high vegetation",
    6: "building",
    7: "low point/noise",
    9: "water",
    17: "bridge deck",
    18: "high noise",
}


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def transform_geometry(geometry: Any, src_crs: Any, dst_crs: Any) -> Any:
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return transform(transformer.transform, geometry)


def horizontal_crs(crs: CRS) -> CRS:
    if crs.is_compound:
        return crs.sub_crs_list[0]
    return crs


def load_centerlines(path: Path, config: dict[str, Any]) -> list[LineString]:
    label_order = {"Loop N": 0, "Loop S": 1}
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            collection = json.load(handle)
        features = sorted(
            collection["features"],
            key=lambda feature: label_order.get(feature.get("properties", {}).get("Label"), 99),
        )
        return [shape(feature["geometry"]) for feature in features]

    features = sorted(
        query_loop_centerlines(config),
        key=lambda feature: label_order.get(feature["attributes"]["Label"], 99),
    )
    return [line_from_feature(feature) for feature in features]


def loop_polygon_from_centerlines(centerlines: list[LineString]) -> Polygon:
    if len(centerlines) != 2:
        raise ValueError(f"expected exactly two loop centerlines, found {len(centerlines)}")
    polygon = Polygon(list(centerlines[0].coords) + list(reversed(centerlines[1].coords)))
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        raise ValueError("loop centerlines did not form a usable polygon")
    return polygon


def collect_las_crop(
    las_path: Path,
    crop_geometry: Any,
    chunk_size: int,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    zs: list[np.ndarray] = []
    intensities: list[np.ndarray] = []
    classifications: list[np.ndarray] = []
    return_numbers: list[np.ndarray] = []
    numbers_of_returns: list[np.ndarray] = []
    total_seen = 0
    minx, miny, maxx, maxy = crop_geometry.bounds

    with laspy.open(las_path) as reader:
        header = reader.header
        las_crs = header.parse_crs()
        for points in reader.chunk_iterator(chunk_size):
            total_seen += len(points)
            x = np.asarray(points.x)
            y = np.asarray(points.y)
            bbox_mask = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
            if not np.any(bbox_mask):
                continue
            bbox_indices = np.flatnonzero(bbox_mask)
            inside_mask = contains_xy(crop_geometry, x[bbox_indices], y[bbox_indices])
            if not np.any(inside_mask):
                continue
            selected = bbox_indices[inside_mask]
            xs.append(x[selected])
            ys.append(y[selected])
            zs.append(np.asarray(points.z)[selected])
            intensities.append(np.asarray(points.intensity)[selected])
            classifications.append(np.asarray(points.classification)[selected])
            return_numbers.append(np.asarray(points.return_number)[selected])
            numbers_of_returns.append(np.asarray(points.number_of_returns)[selected])

    if not xs:
        raise ValueError("no LAS points intersected the crop geometry")

    arrays = {
        "x": np.concatenate(xs),
        "y": np.concatenate(ys),
        "z": np.concatenate(zs),
        "intensity": np.concatenate(intensities),
        "classification": np.concatenate(classifications),
        "return_number": np.concatenate(return_numbers),
        "number_of_returns": np.concatenate(numbers_of_returns),
    }
    metadata = {
        "tile_point_count": int(header.point_count),
        "total_points_seen": int(total_seen),
        "las_crs": str(las_crs),
        "point_format": str(header.point_format),
        "version": str(header.version),
        "scale": [float(value) for value in header.scales],
        "offset": [float(value) for value in header.offsets],
        "tile_bounds": {
            "xmin": float(header.mins[0]),
            "ymin": float(header.mins[1]),
            "zmin": float(header.mins[2]),
            "xmax": float(header.maxs[0]),
            "ymax": float(header.maxs[1]),
            "zmax": float(header.maxs[2]),
        },
    }
    return arrays, metadata


def grid_indices(x: np.ndarray, y: np.ndarray, bounds: tuple[float, float, float, float], resolution: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    left, bottom, right, top = bounds
    width = int(np.ceil((right - left) / resolution))
    height = int(np.ceil((top - bottom) / resolution))
    col = np.floor((x - left) / resolution).astype(int)
    row = np.floor((top - y) / resolution).astype(int)
    valid = (row >= 0) & (row < height) & (col >= 0) & (col < width)
    return row[valid], col[valid], valid, height, width


def point_count_grid(x: np.ndarray, y: np.ndarray, bounds: tuple[float, float, float, float], resolution: float) -> np.ndarray:
    row, col, _valid, height, width = grid_indices(x, y, bounds, resolution)
    grid = np.zeros((height, width), dtype=float)
    np.add.at(grid, (row, col), 1.0)
    return grid


def mean_grid(
    x: np.ndarray,
    y: np.ndarray,
    values: np.ndarray,
    bounds: tuple[float, float, float, float],
    resolution: float,
) -> np.ndarray:
    row, col, valid, height, width = grid_indices(x, y, bounds, resolution)
    valid_values = values[valid].astype(float)
    sums = np.zeros((height, width), dtype=float)
    counts = np.zeros((height, width), dtype=float)
    np.add.at(sums, (row, col), valid_values)
    np.add.at(counts, (row, col), 1.0)
    out = sums / np.maximum(counts, 1.0)
    out[counts == 0] = np.nan
    return out


def min_grid(
    x: np.ndarray,
    y: np.ndarray,
    values: np.ndarray,
    bounds: tuple[float, float, float, float],
    resolution: float,
) -> np.ndarray:
    row, col, valid, height, width = grid_indices(x, y, bounds, resolution)
    valid_values = values[valid].astype(float)
    out = np.full((height, width), np.inf, dtype=float)
    np.minimum.at(out, (row, col), valid_values)
    out[np.isinf(out)] = np.nan
    return out


def fill_nan_nearest(values: np.ndarray) -> np.ndarray:
    valid = np.isfinite(values)
    if not np.any(valid):
        return np.zeros_like(values, dtype=float)
    indices = ndimage.distance_transform_edt(~valid, return_distances=False, return_indices=True)
    return values[tuple(indices)]


def normalize(values: np.ndarray, percentiles: tuple[float, float] = (2, 98)) -> np.ndarray:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros_like(values, dtype=float)
    low, high = np.percentile(finite, percentiles)
    if high <= low:
        high = low + 1.0
    return np.clip((values - low) / (high - low), 0.0, 1.0)


def hillshade(elevation: np.ndarray, resolution: float, azimuth_deg: float = 315.0, altitude_deg: float = 45.0) -> np.ndarray:
    gy, gx = np.gradient(elevation, resolution, resolution)
    slope = np.pi / 2.0 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    azimuth = np.deg2rad(azimuth_deg)
    altitude = np.deg2rad(altitude_deg)
    shaded = np.sin(altitude) * np.sin(slope) + np.cos(altitude) * np.cos(slope) * np.cos(azimuth - aspect)
    return np.clip((shaded + 1.0) / 2.0, 0.0, 1.0)


def plot_centerlines(ax: Any, centerlines: list[LineString], src_crs: Any, dst_crs: Any) -> None:
    for line in centerlines:
        native = transform_geometry(line, src_crs, dst_crs)
        xs, ys = native.xy
        ax.plot(xs, ys, color="#40d9ff", linewidth=0.9, linestyle="--", alpha=0.9)


def render_preview(
    arrays: dict[str, np.ndarray],
    centerlines_4326: list[LineString],
    horizontal_las_crs: CRS,
    crop_bounds: tuple[float, float, float, float],
    output_path: Path,
    grid_resolution_m: float,
    local_trend_m: float,
) -> dict[str, Any]:
    x = arrays["x"]
    y = arrays["y"]
    z = arrays["z"]
    intensity = arrays["intensity"].astype(float)
    classification = arrays["classification"].astype(int)
    ground_mask = classification == 2
    ground_source = "classification 2 ground" if np.count_nonzero(ground_mask) >= 20 else "all returns"
    elevation_x = x[ground_mask] if ground_source.startswith("classification") else x
    elevation_y = y[ground_mask] if ground_source.startswith("classification") else y
    elevation_z = z[ground_mask] if ground_source.startswith("classification") else z

    density = point_count_grid(x, y, crop_bounds, grid_resolution_m) / (grid_resolution_m**2)
    elevation_min = min_grid(elevation_x, elevation_y, elevation_z, crop_bounds, grid_resolution_m)
    intensity_mean = mean_grid(x, y, intensity, crop_bounds, grid_resolution_m)

    elevation_filled = fill_nan_nearest(elevation_min)
    shade = hillshade(elevation_filled, grid_resolution_m)
    trend_sigma = max(local_trend_m / grid_resolution_m, 1.0)
    local_relief = elevation_filled - ndimage.gaussian_filter(elevation_filled, sigma=trend_sigma)

    left, bottom, right, top = crop_bounds
    extent = [left, right, bottom, top]
    fig, axes = plt.subplots(2, 2, figsize=(12, 12), dpi=180)
    axes = axes.ravel()

    density_image = axes[0].imshow(np.sqrt(density), cmap="viridis", extent=extent)
    axes[0].set_title("all-return density")
    plt.colorbar(density_image, ax=axes[0], fraction=0.035, pad=0.02, label="sqrt(points / m2)")

    elevation_image = 0.65 * normalize(elevation_filled) + 0.35 * shade
    axes[1].imshow(elevation_image, cmap="terrain", extent=extent)
    axes[1].set_title(f"lowest elevation ({ground_source})")

    relief_image = axes[2].imshow(local_relief, cmap="RdBu_r", extent=extent, vmin=-0.25, vmax=0.25)
    axes[2].set_title(f"local relief after {local_trend_m:g} m trend removal")
    plt.colorbar(relief_image, ax=axes[2], fraction=0.035, pad=0.02, label="m")

    intensity_image = axes[3].imshow(normalize(intensity_mean), cmap="gray", extent=extent)
    axes[3].set_title("mean LAS intensity")
    plt.colorbar(intensity_image, ax=axes[3], fraction=0.035, pad=0.02)

    for ax in axes:
        plot_centerlines(ax, centerlines_4326, "EPSG:4326", horizontal_las_crs)
        ax.set_aspect("equal")
        ax.set_axis_off()

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)

    finite_elevation = elevation_min[np.isfinite(elevation_min)]
    return {
        "grid_resolution_m": float(grid_resolution_m),
        "ground_source": ground_source,
        "filled_elevation_cell_fraction": float(finite_elevation.size / elevation_min.size),
        "elevation_min_m": float(np.min(finite_elevation)),
        "elevation_max_m": float(np.max(finite_elevation)),
        "elevation_range_m": float(np.max(finite_elevation) - np.min(finite_elevation)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--centerline-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "loop-centerlines.geojson")
    parser.add_argument("--las", type=Path, default=PROJECT_ROOT / "data" / "raw" / "lidar" / "fema_2019" / "18TXX000000.las")
    parser.add_argument("--output-npz", type=Path, default=PROJECT_ROOT / "data" / "interim" / "lidar" / "fema-2019-las-loop-tight.npz")
    parser.add_argument("--output-png", type=Path, default=PROJECT_ROOT / "build" / "lidar" / "fema-2019-las-loop-tight.png")
    parser.add_argument("--metadata", type=Path, default=PROJECT_ROOT / "build" / "lidar" / "fema-2019-las-loop-tight.json")
    parser.add_argument("--margin-m", type=float, default=18.0)
    parser.add_argument("--grid-resolution-m", type=float, default=0.5)
    parser.add_argument("--local-trend-m", type=float, default=3.0)
    parser.add_argument("--chunk-size", type=int, default=1_000_000)
    args = parser.parse_args()

    config = load_yaml(args.config)
    centerlines_4326 = load_centerlines(args.centerline_geojson, config)
    loop_polygon_4326 = loop_polygon_from_centerlines(centerlines_4326)

    with laspy.open(args.las) as reader:
        las_crs = CRS.from_user_input(reader.header.parse_crs())
    las_horizontal_crs = horizontal_crs(las_crs)
    loop_polygon_native = transform_geometry(loop_polygon_4326, "EPSG:4326", las_horizontal_crs)
    crop_geometry = loop_polygon_native.buffer(args.margin_m)
    crop_bounds = crop_geometry.bounds

    arrays, las_metadata = collect_las_crop(args.las, crop_geometry, args.chunk_size)
    args.output_npz.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.output_npz, **arrays)

    preview_metadata = render_preview(
        arrays,
        centerlines_4326,
        las_horizontal_crs,
        crop_bounds,
        args.output_png,
        args.grid_resolution_m,
        args.local_trend_m,
    )

    class_counter = Counter(int(value) for value in arrays["classification"])
    class_counts = {
        f"{class_id}: {CLASS_NAMES.get(class_id, 'unknown')}": int(count)
        for class_id, count in sorted(class_counter.items())
    }
    point_count = int(arrays["x"].size)
    ground_count = int(np.count_nonzero(arrays["classification"].astype(int) == 2))
    crop_area_m2 = float(crop_geometry.area)
    metadata = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_las": str(args.las),
        "output_npz": str(args.output_npz),
        "output_png": str(args.output_png),
        "source": {
            "collection": "FEMA 2019",
            "tile": "18TXX000000.las",
            "direct_download": "https://gisdata.ny.gov/elevation/LIDAR/FEMA_2019/ExampleCounty/18TXX000000.las",
            "index_layer": "https://elevation.its.ny.gov/arcgis/rest/services/LAS_Indexes/FeatureServer/7",
            "nominal_point_spacing_m": 0.7,
            "type": "LAS point cloud",
        },
        "crop": {
            "margin_m": float(args.margin_m),
            "area_m2": crop_area_m2,
            "bounds": {
                "xmin": float(crop_bounds[0]),
                "ymin": float(crop_bounds[1]),
                "xmax": float(crop_bounds[2]),
                "ymax": float(crop_bounds[3]),
            },
        },
        "las": las_metadata,
        "points": {
            "count": point_count,
            "density_per_m2": float(point_count / crop_area_m2),
            "mean_spacing_m": float(np.sqrt(crop_area_m2 / point_count)),
            "ground_count": ground_count,
            "ground_density_per_m2": float(ground_count / crop_area_m2),
            "ground_mean_spacing_m": float(np.sqrt(crop_area_m2 / ground_count)) if ground_count else None,
            "classification_counts": class_counts,
        },
        "preview": preview_metadata,
        "assessment": (
            "The LAS crop is dense enough to inspect as an independent source. "
            "Whether it can resolve the curb depends on visible local-relief or intensity breaks; "
            "the 1 m DEM should be treated as terrain context only."
        ),
    }
    write_json(args.metadata, metadata)
    print(f"wrote {args.output_npz}")
    print(f"wrote {args.output_png}")
    print(f"wrote {args.metadata}")
    print(f"crop points: {point_count:,} ({point_count / crop_area_m2:.2f} pts/m2)")
    print(f"ground points: {ground_count:,} ({ground_count / crop_area_m2:.2f} pts/m2)")


if __name__ == "__main__":
    main()
