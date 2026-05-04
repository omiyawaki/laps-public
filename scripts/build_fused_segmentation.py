#!/usr/bin/env python3
"""Build a first 2D curb candidate from independent imagery and LiDAR evidence."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import rasterio
import yaml
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from scipy import ndimage, sparse
from scipy.sparse.linalg import spsolve
from shapely.geometry import LineString, MultiLineString, Polygon, mapping, shape
from shapely.ops import transform, unary_union
from skimage.measure import find_contours
from skimage.segmentation import find_boundaries, slic

from build_baseline_geometry import line_from_feature, query_loop_centerlines, stretch_rgb


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def unit_conversion_to_meters(crs: CRS) -> float:
    return float(crs.axis_info[0].unit_conversion_factor)


def meters_to_units(meters: float, unit_to_meter: float) -> float:
    return meters / unit_to_meter


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


def load_ortho(path: Path, downsample: int) -> tuple[np.ndarray, np.ndarray, Any, CRS]:
    with rasterio.open(path) as dataset:
        out_height = int(math.ceil(dataset.height / downsample))
        out_width = int(math.ceil(dataset.width / downsample))
        array = dataset.read(
            out_shape=(dataset.count, out_height, out_width),
            resampling=Resampling.average,
        )
        transform_affine = dataset.transform * dataset.transform.scale(
            dataset.width / out_width,
            dataset.height / out_height,
        )
        crs = CRS.from_user_input(dataset.crs)
    rgb = stretch_rgb(array)
    return array.astype(float), rgb, transform_affine, crs


def normalize(values: np.ndarray, percentiles: tuple[float, float] = (2, 98)) -> np.ndarray:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros_like(values, dtype=float)
    low, high = np.percentile(finite, percentiles)
    if high <= low:
        high = low + 1.0
    return np.clip((values - low) / (high - low), 0.0, 1.0)


def robust_scale_matrix(values: np.ndarray) -> np.ndarray:
    center = np.nanmedian(values, axis=0)
    mad = np.nanmedian(np.abs(values - center), axis=0)
    scale = np.where(mad > 1e-6, 1.4826 * mad, np.nanstd(values, axis=0))
    scale = np.where(scale > 1e-6, scale, 1.0)
    return np.clip((values - center) / scale, -6.0, 6.0)


def fill_nan_nearest(values: np.ndarray, fallback: float = 0.0) -> np.ndarray:
    valid = np.isfinite(values)
    if not np.any(valid):
        return np.full_like(values, fallback, dtype=float)
    indices = ndimage.distance_transform_edt(~valid, return_distances=False, return_indices=True)
    return values[tuple(indices)]


def ortho_features(array: np.ndarray) -> dict[str, np.ndarray]:
    r = array[0]
    g = array[1]
    b = array[2]
    nir = array[3] if array.shape[0] >= 4 else np.zeros_like(r)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    chroma = np.max(array[:3], axis=0) - np.min(array[:3], axis=0)
    ndvi = (nir - r) / (nir + r + 1e-6)
    green_excess = 2.0 * g - r - b
    grayness = luminance - 1.15 * chroma
    return {
        "red": normalize(r),
        "green": normalize(g),
        "blue": normalize(b),
        "nir": normalize(nir),
        "luminance": normalize(luminance),
        "chroma": normalize(chroma),
        "grayness": normalize(grayness),
        "ndvi": normalize(ndvi),
        "green_excess": normalize(green_excess),
    }


def grid_lidar_to_ortho(
    lidar_npz: Path,
    output_shape: tuple[int, int],
    transform_affine: Any,
    ortho_crs: CRS,
    lidar_crs: str,
    resolution_m: float,
) -> dict[str, np.ndarray]:
    data = np.load(lidar_npz)
    transformer = Transformer.from_crs(lidar_crs, ortho_crs, always_xy=True)
    x_native, y_native = transformer.transform(data["x"], data["y"])
    inverse_transform = ~transform_affine
    col_float, row_float = inverse_transform * (x_native, y_native)
    rows = np.floor(row_float).astype(int)
    cols = np.floor(col_float).astype(int)
    height, width = output_shape
    valid = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)
    rows = rows[valid]
    cols = cols[valid]
    z = data["z"][valid].astype(float)
    intensity = data["intensity"][valid].astype(float)
    classification = data["classification"][valid].astype(int)

    density_counts = np.zeros(output_shape, dtype=float)
    np.add.at(density_counts, (rows, cols), 1.0)
    density = density_counts / (resolution_m**2)

    intensity_sum = np.zeros(output_shape, dtype=float)
    intensity_count = np.zeros(output_shape, dtype=float)
    np.add.at(intensity_sum, (rows, cols), intensity)
    np.add.at(intensity_count, (rows, cols), 1.0)
    mean_intensity = intensity_sum / np.maximum(intensity_count, 1.0)
    mean_intensity[intensity_count == 0] = np.nan

    ground = classification == 2
    ground_rows = rows[ground]
    ground_cols = cols[ground]
    ground_z = z[ground]
    min_ground = np.full(output_shape, np.inf, dtype=float)
    np.minimum.at(min_ground, (ground_rows, ground_cols), ground_z)
    min_ground[np.isinf(min_ground)] = np.nan

    elevation = fill_nan_nearest(min_ground, fallback=float(np.nanmedian(z)))
    trend_sigma = max(3.0 / resolution_m, 1.0)
    relief = elevation - ndimage.gaussian_filter(elevation, sigma=trend_sigma)
    gy, gx = np.gradient(elevation, resolution_m, resolution_m)
    elevation_gradient = np.hypot(gx, gy)

    return {
        "density": normalize(density),
        "intensity": normalize(fill_nan_nearest(mean_intensity, fallback=float(np.nanmedian(intensity)))),
        "ground_elevation": normalize(elevation),
        "local_relief": np.clip(relief / 0.25, -1.0, 1.0),
        "elevation_gradient": normalize(elevation_gradient),
        "raw_density": density,
        "raw_ground_elevation": elevation,
        "raw_local_relief": relief,
        "ground_observed": np.isfinite(min_ground).astype(float),
    }


def rasterize_geometry(geometry: Any, output_shape: tuple[int, int], transform_affine: Any, all_touched: bool = True) -> np.ndarray:
    return geometry_mask(
        [mapping(geometry)],
        out_shape=output_shape,
        transform=transform_affine,
        invert=True,
        all_touched=all_touched,
    )


def build_seed_masks(
    centerlines_native: list[LineString],
    loop_polygon_native: Polygon,
    output_shape: tuple[int, int],
    transform_affine: Any,
    unit_to_meter: float,
    road_seed_m: float,
    green_seed_inset_m: float,
) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    road_buffer = unary_union([line.buffer(meters_to_units(road_seed_m, unit_to_meter), cap_style=2) for line in centerlines_native])
    green_seed = loop_polygon_native.buffer(-meters_to_units(green_seed_inset_m, unit_to_meter))
    used_green_inset_m = green_seed_inset_m
    while green_seed.is_empty and used_green_inset_m > 2.0:
        used_green_inset_m -= 1.0
        green_seed = loop_polygon_native.buffer(-meters_to_units(used_green_inset_m, unit_to_meter))
    if green_seed.is_empty:
        raise ValueError("could not build a non-empty green seed from the loop polygon")
    road_mask = rasterize_geometry(road_buffer, output_shape, transform_affine)
    green_mask = rasterize_geometry(green_seed, output_shape, transform_affine)
    green_mask &= ~road_mask
    return road_mask, green_mask, {
        "road_seed_m": road_seed_m,
        "green_seed_inset_m": used_green_inset_m,
    }


def build_superpixels(array: np.ndarray, n_segments: int, compactness: float) -> np.ndarray:
    features = ortho_features(array)
    image = np.dstack(
        [
            features["red"],
            features["green"],
            features["blue"],
            features["nir"],
            features["ndvi"],
        ]
    )
    return slic(
        image,
        n_segments=n_segments,
        compactness=compactness,
        sigma=0.7,
        start_label=0,
        channel_axis=-1,
        enforce_connectivity=True,
    )


def segment_means(segments: np.ndarray, feature_stack: np.ndarray, n_segments: int) -> np.ndarray:
    flat_segments = segments.ravel()
    means = []
    for index in range(feature_stack.shape[2]):
        values = feature_stack[:, :, index].ravel()
        sums = np.bincount(flat_segments, weights=values, minlength=n_segments)
        counts = np.bincount(flat_segments, minlength=n_segments)
        means.append(sums / np.maximum(counts, 1))
    return np.column_stack(means)


def segment_seed_labels(segments: np.ndarray, road_mask: np.ndarray, green_mask: np.ndarray, n_segments: int) -> tuple[np.ndarray, dict[str, int]]:
    flat_segments = segments.ravel()
    counts = np.bincount(flat_segments, minlength=n_segments)
    road_counts = np.bincount(flat_segments, weights=road_mask.ravel().astype(float), minlength=n_segments)
    green_counts = np.bincount(flat_segments, weights=green_mask.ravel().astype(float), minlength=n_segments)
    road_fraction = road_counts / np.maximum(counts, 1)
    green_fraction = green_counts / np.maximum(counts, 1)
    labels = np.full(n_segments, -1, dtype=int)
    labels[(road_fraction >= 0.18) & (road_fraction > green_fraction)] = 0
    labels[(green_fraction >= 0.18) & (green_fraction > road_fraction)] = 1
    return labels, {
        "road_seed_segments": int(np.count_nonzero(labels == 0)),
        "green_seed_segments": int(np.count_nonzero(labels == 1)),
        "unseeded_segments": int(np.count_nonzero(labels < 0)),
    }


def adjacency_pairs(segments: np.ndarray) -> np.ndarray:
    horizontal = np.column_stack(
        [
            segments[:, :-1].ravel(),
            segments[:, 1:].ravel(),
        ]
    )
    vertical = np.column_stack(
        [
            segments[:-1, :].ravel(),
            segments[1:, :].ravel(),
        ]
    )
    pairs = np.vstack([horizontal, vertical])
    pairs = pairs[pairs[:, 0] != pairs[:, 1]]
    pairs.sort(axis=1)
    return np.unique(pairs, axis=0)


def solve_graph_probability(
    region_features: np.ndarray,
    labels: np.ndarray,
    pairs: np.ndarray,
    beta: float,
) -> np.ndarray:
    scaled = robust_scale_matrix(region_features)
    diff = scaled[pairs[:, 0]] - scaled[pairs[:, 1]]
    distance2 = np.sum(diff * diff, axis=1) / max(1, scaled.shape[1])
    weights = np.exp(-beta * distance2) + 1e-4

    n_segments = labels.size
    row = np.concatenate([pairs[:, 0], pairs[:, 1], np.arange(n_segments)])
    col = np.concatenate([pairs[:, 1], pairs[:, 0], np.arange(n_segments)])
    data = np.concatenate([-weights, -weights, np.zeros(n_segments)])
    degree = np.bincount(np.concatenate([pairs[:, 0], pairs[:, 1]]), weights=np.concatenate([weights, weights]), minlength=n_segments)
    data[-n_segments:] = degree
    laplacian = sparse.csr_matrix((data, (row, col)), shape=(n_segments, n_segments))

    seeded = labels >= 0
    unseeded = ~seeded
    probability = labels.astype(float)
    if not np.any(unseeded):
        return probability
    if not np.any(labels == 0) or not np.any(labels == 1):
        raise ValueError("both road and green seeds are required")

    l_uu = laplacian[unseeded][:, unseeded]
    l_us = laplacian[unseeded][:, seeded]
    rhs = -l_us @ probability[seeded]
    solution = spsolve(l_uu, rhs)
    probability[unseeded] = np.clip(solution, 0.0, 1.0)
    probability[labels == 0] = 0.0
    probability[labels == 1] = 1.0
    return probability


def contour_to_lines(
    probability_map: np.ndarray,
    transform_affine: Any,
    loop_polygon_native: Polygon,
    min_length_units: float,
) -> list[LineString]:
    smooth = ndimage.gaussian_filter(probability_map, sigma=0.7)
    lines: list[LineString] = []
    for contour in find_contours(smooth, 0.5):
        if contour.shape[0] < 20:
            continue
        coords = [transform_affine * (float(col), float(row)) for row, col in contour]
        line = LineString(coords)
        if line.length < min_length_units:
            continue
        if not loop_polygon_native.buffer(1.0).contains(line.interpolate(0.5, normalized=True)):
            continue
        if line.coords[0] != line.coords[-1]:
            start = np.array(line.coords[0])
            end = np.array(line.coords[-1])
            if float(np.linalg.norm(start - end)) < 4.0:
                line = LineString(list(line.coords) + [line.coords[0]])
        lines.append(line)
    return sorted(lines, key=lambda item: item.length, reverse=True)


def geojson_feature(geometry: Any, properties: dict[str, Any], src_crs: Any) -> dict[str, Any]:
    geometry_4326 = transform_geometry(geometry, src_crs, "EPSG:4326")
    return {
        "type": "Feature",
        "properties": {**properties, "geojson_crs": "EPSG:4326"},
        "geometry": mapping(geometry_4326),
    }


def write_geojson(path: Path, features: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle, indent=2)
        handle.write("\n")


def polygon_from_closed_line(line: LineString) -> Polygon | None:
    coords = list(line.coords)
    if len(coords) < 4:
        return None
    if coords[0] != coords[-1]:
        return None
    polygon = Polygon(coords)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        return None
    return polygon


def render_overlay(
    rgb: np.ndarray,
    transform_affine: Any,
    loop_polygon_native: Polygon,
    centerlines_native: list[LineString],
    road_mask: np.ndarray,
    green_mask: np.ndarray,
    segments: np.ndarray,
    probability_maps: dict[str, np.ndarray],
    contours: dict[str, LineString | None],
    lidar_layers: dict[str, np.ndarray],
    output_path: Path,
) -> None:
    height, width = road_mask.shape
    left = transform_affine.c
    top = transform_affine.f
    right = left + transform_affine.a * width
    bottom = top + transform_affine.e * height
    extent = [left, right, bottom, top]

    fig, axes = plt.subplots(2, 2, figsize=(14, 14), dpi=180)
    axes = axes.ravel()
    axes[0].imshow(rgb, extent=extent)
    axes[0].imshow(np.ma.masked_where(~road_mask, road_mask), extent=extent, cmap="Blues", alpha=0.25)
    axes[0].imshow(np.ma.masked_where(~green_mask, green_mask), extent=extent, cmap="Greens", alpha=0.25)
    axes[0].imshow(find_boundaries(segments, mode="outer"), extent=extent, cmap="gray", alpha=0.10)
    axes[0].set_title("seeded superpixels")

    axes[1].imshow(rgb, extent=extent)
    colors = {
        "imagery": "#ff4f7b",
        "lidar": "#ffd84d",
        "fused": "#00e0ff",
    }
    for name, line in contours.items():
        if line is None:
            continue
        xs, ys = line.xy
        axes[1].plot(xs, ys, color=colors[name], linewidth=1.8, label=name)
    axes[1].legend(loc="lower right", frameon=True)
    axes[1].set_title("0.5 probability contours")

    fused_map = probability_maps["fused"]
    axes[2].imshow(fused_map, extent=extent, cmap="YlGn", vmin=0, vmax=1)
    axes[2].set_title("fused green probability")

    axes[3].imshow(lidar_layers["raw_local_relief"], extent=extent, cmap="RdBu_r", vmin=-0.25, vmax=0.25)
    axes[3].set_title("LiDAR local relief")

    for ax in axes:
        exterior = loop_polygon_native.exterior
        ax.plot(*exterior.xy, color="#ffffff", linewidth=0.8, linestyle=":", alpha=0.8)
        for line in centerlines_native:
            ax.plot(*line.xy, color="#38d8ff", linewidth=0.9, linestyle="--", alpha=0.8)
        ax.set_aspect("equal")
        ax.set_axis_off()

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--ortho", type=Path, default=PROJECT_ROOT / "data" / "interim" / "ortho" / "example-2024-loop-tight.tif")
    parser.add_argument("--lidar-npz", type=Path, default=PROJECT_ROOT / "data" / "interim" / "lidar" / "fema-2019-las-loop-tight.npz")
    parser.add_argument("--centerline-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "loop-centerlines.geojson")
    parser.add_argument("--output-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-fused-segmentation-candidates.geojson")
    parser.add_argument("--running-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "running-line-2ft-fused-segmentation-candidate.geojson")
    parser.add_argument("--overlay", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "imagery-lidar-segmentation-overlay.png")
    parser.add_argument("--diagnostics", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "imagery-lidar-segmentation-diagnostics.json")
    parser.add_argument("--downsample", type=int, default=2)
    parser.add_argument("--n-segments", type=int, default=5200)
    parser.add_argument("--compactness", type=float, default=0.08)
    parser.add_argument("--road-seed-m", type=float, default=1.25)
    parser.add_argument("--green-seed-inset-m", type=float, default=7.0)
    parser.add_argument("--graph-beta", type=float, default=1.8)
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    offset_m = float(config["measurement"]["intended_offset"]["meters"])
    centerlines_4326 = load_centerlines(args.centerline_geojson, config)
    loop_polygon_4326 = loop_polygon_from_centerlines(centerlines_4326)

    array, rgb, transform_affine, ortho_crs = load_ortho(args.ortho, args.downsample)
    unit_to_meter = unit_conversion_to_meters(ortho_crs)
    resolution_units = abs(float(transform_affine.a))
    resolution_m = resolution_units * unit_to_meter
    output_shape = (array.shape[1], array.shape[2])

    centerlines_native = [transform_geometry(line, "EPSG:4326", ortho_crs) for line in centerlines_4326]
    loop_polygon_native = transform_geometry(loop_polygon_4326, "EPSG:4326", ortho_crs)
    road_mask, green_mask, seed_metadata = build_seed_masks(
        centerlines_native,
        loop_polygon_native,
        output_shape,
        transform_affine,
        unit_to_meter,
        args.road_seed_m,
        args.green_seed_inset_m,
    )

    image_layers = ortho_features(array)
    lidar_layers = grid_lidar_to_ortho(
        args.lidar_npz,
        output_shape,
        transform_affine,
        ortho_crs,
        "EPSG:6347",
        resolution_m,
    )
    segments = build_superpixels(array, args.n_segments, args.compactness)
    n_segments = int(segments.max()) + 1
    labels, label_metadata = segment_seed_labels(segments, road_mask, green_mask, n_segments)
    pairs = adjacency_pairs(segments)

    image_stack = np.dstack(
        [
            image_layers["red"],
            image_layers["green"],
            image_layers["blue"],
            image_layers["nir"],
            image_layers["luminance"],
            image_layers["chroma"],
            image_layers["grayness"],
            image_layers["ndvi"],
            image_layers["green_excess"],
        ]
    )
    lidar_stack = np.dstack(
        [
            lidar_layers["density"],
            lidar_layers["intensity"],
            lidar_layers["ground_elevation"],
            lidar_layers["local_relief"],
            lidar_layers["elevation_gradient"],
            lidar_layers["ground_observed"],
        ]
    )
    feature_stacks = {
        "imagery": image_stack,
        "lidar": lidar_stack,
        "fused": np.dstack([image_stack, lidar_stack]),
    }

    probability_maps: dict[str, np.ndarray] = {}
    contours: dict[str, LineString | None] = {}
    features: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "ortho": str(args.ortho),
        "lidar_npz": str(args.lidar_npz),
        "downsample": int(args.downsample),
        "segmentation_grid_shape": [int(output_shape[0]), int(output_shape[1])],
        "segmentation_resolution_m": float(resolution_m),
        "superpixels": int(n_segments),
        "adjacency_edges": int(pairs.shape[0]),
        "seed_masks": {
            **seed_metadata,
            "road_seed_pixels": int(np.count_nonzero(road_mask)),
            "green_seed_pixels": int(np.count_nonzero(green_mask)),
        },
        "seed_segments": label_metadata,
        "methods": {},
    }

    min_length_units = meters_to_units(120.0, unit_to_meter)
    for name, stack in feature_stacks.items():
        region_features = segment_means(segments, stack, n_segments)
        probability = solve_graph_probability(region_features, labels, pairs, args.graph_beta)
        probability_map = probability[segments]
        probability_maps[name] = probability_map
        candidate_lines = contour_to_lines(probability_map, transform_affine, loop_polygon_native, min_length_units)
        selected_line = candidate_lines[0] if candidate_lines else None
        contours[name] = selected_line
        method_properties = {
            "method": name,
            "candidate_count": int(len(candidate_lines)),
            "selected_length_m": None,
            "selected_closed": False,
        }
        if selected_line is not None:
            method_properties["selected_length_m"] = float(selected_line.length * unit_to_meter)
            method_properties["selected_closed"] = bool(selected_line.coords[0] == selected_line.coords[-1])
            features.append(geojson_feature(selected_line, method_properties, ortho_crs))
        diagnostics["methods"][name] = method_properties

    fused_line = contours["fused"]
    running_features: list[dict[str, Any]] = []
    if fused_line is not None:
        fused_polygon = polygon_from_closed_line(fused_line)
        if fused_polygon is not None:
            running_polygon = fused_polygon.buffer(-meters_to_units(offset_m, unit_to_meter))
            if not running_polygon.is_empty:
                if running_polygon.geom_type == "MultiPolygon":
                    running_polygon = max(running_polygon.geoms, key=lambda item: item.area)
                running_line = running_polygon.exterior
                running_line_measurement = transform_geometry(running_line, ortho_crs, measurement_crs)
                running_length_m = float(running_line_measurement.length)
                running_features.append(
                    geojson_feature(
                        running_line,
                        {
                            "method": "fused",
                            "offset_m": offset_m,
                            "length_m": running_length_m,
                            "length_km": running_length_m / 1000.0,
                            "length_miles": running_length_m / 1609.344,
                        },
                        ortho_crs,
                    )
                )
                diagnostics["fused_running_line"] = {
                    "offset_m": offset_m,
                    "length_m": running_length_m,
                    "length_km": running_length_m / 1000.0,
                    "length_miles": running_length_m / 1609.344,
                }

    write_geojson(args.output_geojson, features)
    write_geojson(args.running_geojson, running_features)
    render_overlay(
        rgb,
        transform_affine,
        loop_polygon_native,
        centerlines_native,
        road_mask,
        green_mask,
        segments,
        probability_maps,
        contours,
        lidar_layers,
        args.overlay,
    )
    write_json(args.diagnostics, diagnostics)

    print(f"wrote {args.output_geojson}")
    print(f"wrote {args.running_geojson}")
    print(f"wrote {args.overlay}")
    print(f"wrote {args.diagnostics}")
    if "fused_running_line" in diagnostics:
        print(f"fused running length: {diagnostics['fused_running_line']['length_m']:.3f} m")


if __name__ == "__main__":
    main()
