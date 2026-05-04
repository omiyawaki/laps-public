#!/usr/bin/env python3
"""Refine the piecewise curb candidate against original imagery and LiDAR evidence."""

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
from scipy import ndimage
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import transform

from build_fused_segmentation import (
    build_seed_masks,
    grid_lidar_to_ortho,
    load_centerlines,
    load_ortho,
    loop_polygon_from_centerlines,
    meters_to_units,
    ortho_features,
    transform_geometry,
    unit_conversion_to_meters,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def load_selected_line(path: Path, fallback_penalty: float | None = None) -> tuple[LineString, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    fallback: tuple[LineString, dict[str, Any]] | None = None
    for feature in collection["features"]:
        properties = dict(feature.get("properties", {}))
        geometry = shape(feature["geometry"])
        if not isinstance(geometry, LineString):
            continue
        if properties.get("selected"):
            return geometry, properties
        if fallback_penalty is not None and abs(float(properties.get("segment_penalty", -999.0)) - fallback_penalty) < 1e-9:
            fallback = (geometry, properties)
    if fallback is not None:
        return fallback
    raise ValueError(f"no selected line found in {path}")


def signed_area(coords: np.ndarray) -> float:
    x = coords[:, 0]
    y = coords[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - y * np.roll(x, -1)))


def resample_closed_line(line: LineString, spacing_units: float) -> tuple[np.ndarray, float]:
    total_length = float(line.length)
    n_samples = max(64, int(round(total_length / spacing_units)))
    distances = np.linspace(0.0, total_length, n_samples, endpoint=False)
    points = [line.interpolate(float(distance)) for distance in distances]
    coords = np.array([[point.x, point.y] for point in points], dtype=float)
    return coords, total_length / n_samples


def closed_tangents_normals(coords: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    tangent = np.roll(coords, -1, axis=0) - np.roll(coords, 1, axis=0)
    norm = np.linalg.norm(tangent, axis=1)
    tangent = tangent / np.maximum(norm[:, None], 1e-9)
    normal = np.column_stack([-tangent[:, 1], tangent[:, 0]])
    return tangent, normal


def normalize(values: np.ndarray, percentiles: tuple[float, float] = (2, 98)) -> np.ndarray:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros_like(values, dtype=float)
    low, high = np.percentile(finite, percentiles)
    if high <= low:
        high = low + 1.0
    return np.clip((values - low) / (high - low), 0.0, 1.0)


def feature_stack_from_sources(
    ortho_array: np.ndarray,
    lidar_layers: dict[str, np.ndarray],
) -> tuple[np.ndarray, list[str]]:
    image = ortho_features(ortho_array)
    layers = [
        image["red"],
        image["green"],
        image["blue"],
        image["nir"],
        image["luminance"],
        image["chroma"],
        image["grayness"],
        image["ndvi"],
        image["green_excess"],
        lidar_layers["density"],
        lidar_layers["intensity"],
        lidar_layers["ground_elevation"],
        lidar_layers["local_relief"],
        lidar_layers["elevation_gradient"],
        lidar_layers["ground_observed"],
    ]
    names = [
        "red",
        "green",
        "blue",
        "nir",
        "luminance",
        "chroma",
        "grayness",
        "ndvi",
        "green_excess",
        "lidar_density",
        "lidar_intensity",
        "lidar_ground_elevation",
        "lidar_local_relief",
        "lidar_elevation_gradient",
        "lidar_ground_observed",
    ]
    return np.dstack(layers).astype(float), names


def build_green_score(
    feature_stack: np.ndarray,
    road_mask: np.ndarray,
    green_mask: np.ndarray,
    feature_names: list[str],
) -> tuple[np.ndarray, dict[str, Any]]:
    road_values = feature_stack[road_mask]
    green_values = feature_stack[green_mask]
    pooled = np.vstack([road_values, green_values])
    road_center = np.nanmedian(road_values, axis=0)
    green_center = np.nanmedian(green_values, axis=0)
    pooled_center = np.nanmedian(pooled, axis=0)
    mad = np.nanmedian(np.abs(pooled - pooled_center), axis=0)
    scale = np.where(mad > 1e-6, 1.4826 * mad, np.nanstd(pooled, axis=0))
    scale = np.where(scale > 1e-6, scale, 1.0)
    weights = np.ones(feature_stack.shape[2], dtype=float)
    for index, name in enumerate(feature_names):
        if name.startswith("lidar_"):
            weights[index] = 0.55
        if name in {"ndvi", "green_excess", "grayness"}:
            weights[index] = 1.35
    road_distance = np.mean(weights * np.clip(((feature_stack - road_center) / scale) ** 2, 0.0, 9.0), axis=2)
    green_distance = np.mean(weights * np.clip(((feature_stack - green_center) / scale) ** 2, 0.0, 9.0), axis=2)
    score = road_distance - green_distance
    score = ndimage.gaussian_filter(score, sigma=1.0)
    finite = score[np.isfinite(score)]
    center = float(np.median(finite))
    spread = float(np.percentile(finite, 90) - np.percentile(finite, 10))
    if spread <= 1e-6:
        spread = 1.0
    score = np.clip((score - center) / spread, -4.0, 4.0)
    diagnostics = {
        "feature_names": feature_names,
        "road_seed_pixels": int(np.count_nonzero(road_mask)),
        "green_seed_pixels": int(np.count_nonzero(green_mask)),
        "road_center": road_center.tolist(),
        "green_center": green_center.tolist(),
        "feature_scale": scale.tolist(),
        "feature_weights": weights.tolist(),
        "score_seed_median": {
            "road": float(np.median(score[road_mask])),
            "green": float(np.median(score[green_mask])),
        },
    }
    return score, diagnostics


def bilinear_sample(grid: np.ndarray, inverse_transform: Any, points: np.ndarray) -> np.ndarray:
    cols, rows = inverse_transform * (points[:, 0], points[:, 1])
    height, width = grid.shape
    x0 = np.floor(cols).astype(int)
    y0 = np.floor(rows).astype(int)
    valid = (x0 >= 0) & (x0 < width - 1) & (y0 >= 0) & (y0 < height - 1)
    output = np.full(points.shape[0], np.nan, dtype=float)
    if not np.any(valid):
        return output
    dx = cols[valid] - x0[valid]
    dy = rows[valid] - y0[valid]
    top = grid[y0[valid], x0[valid]] * (1.0 - dx) + grid[y0[valid], x0[valid] + 1] * dx
    bottom = grid[y0[valid] + 1, x0[valid]] * (1.0 - dx) + grid[y0[valid] + 1, x0[valid] + 1] * dx
    output[valid] = top * (1.0 - dy) + bottom * dy
    return output


def contrast_for_offsets(
    coords: np.ndarray,
    normals: np.ndarray,
    offsets_units: np.ndarray,
    probes_units: np.ndarray,
    score: np.ndarray,
    gradient: np.ndarray,
    inverse_transform: Any,
) -> tuple[np.ndarray, np.ndarray]:
    unary = np.zeros((coords.shape[0], offsets_units.size), dtype=float)
    contrast = np.zeros_like(unary)
    for state_index, offset in enumerate(offsets_units):
        candidate = coords + normals * offset
        inner_values: list[np.ndarray] = []
        outer_values: list[np.ndarray] = []
        for probe in probes_units:
            inner_values.append(bilinear_sample(score, inverse_transform, candidate + normals * probe))
            outer_values.append(bilinear_sample(score, inverse_transform, candidate - normals * probe))
        inner = np.nanmean(np.vstack(inner_values), axis=0)
        outer = np.nanmean(np.vstack(outer_values), axis=0)
        local_contrast = np.nan_to_num(inner - outer, nan=-2.0, posinf=-2.0, neginf=-2.0)
        boundary_gradient = np.nan_to_num(bilinear_sample(gradient, inverse_transform, candidate), nan=0.0)
        contrast[:, state_index] = local_contrast
        unary[:, state_index] = -local_contrast - 0.18 * boundary_gradient
    return unary, contrast


def solve_offset_path(
    unary: np.ndarray,
    offsets_m: np.ndarray,
    smooth_weight: float,
    offset_prior_weight: float,
) -> np.ndarray:
    n, states = unary.shape
    pairwise = smooth_weight * (offsets_m[:, None] - offsets_m[None, :]) ** 2
    prior = offset_prior_weight * offsets_m * offsets_m
    best_total = np.inf
    best_path: np.ndarray | None = None
    for start_state in range(states):
        dp = np.full((n, states), np.inf, dtype=float)
        prev = np.full((n, states), -1, dtype=int)
        dp[0, start_state] = unary[0, start_state] + prior[start_state]
        for index in range(1, n):
            transition = dp[index - 1][:, None] + pairwise
            prev[index] = np.argmin(transition, axis=0)
            dp[index] = unary[index] + prior + np.min(transition, axis=0)
        closing = pairwise[:, start_state]
        end_state = int(np.argmin(dp[-1] + closing))
        total = float(dp[-1, end_state] + closing[end_state])
        if total < best_total:
            path = np.empty(n, dtype=int)
            path[-1] = end_state
            for index in range(n - 1, 0, -1):
                path[index - 1] = prev[index, path[index]]
            best_total = total
            best_path = path
    if best_path is None:
        raise ValueError("failed to solve offset path")
    return offsets_m[best_path]


def fourier_lowpass_periodic(values: np.ndarray, spacing_m: float, cutoff_wavelength_m: float, order: int = 8) -> np.ndarray:
    frequencies = np.fft.fftfreq(values.size, d=spacing_m)
    cutoff_frequency = 1.0 / cutoff_wavelength_m
    with np.errstate(divide="ignore", invalid="ignore"):
        weights = 1.0 / np.sqrt(1.0 + (np.abs(frequencies) / cutoff_frequency) ** (2 * order))
    weights[0] = 1.0
    return np.fft.ifft(np.fft.fft(values) * weights).real


def polygon_from_closed_line(line: LineString) -> Polygon | None:
    coords = np.asarray(line.coords, dtype=float)
    if np.linalg.norm(coords[0] - coords[-1]) > 1e-6:
        coords = np.vstack([coords, coords[0]])
    polygon = Polygon(coords)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        return None
    if polygon.geom_type == "MultiPolygon":
        polygon = max(polygon.geoms, key=lambda item: item.area)
    return polygon


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


def plot_line(ax: Any, line: LineString, src_crs: Any, dst_crs: Any, **kwargs: Any) -> None:
    native = transform_geometry(line, src_crs, dst_crs)
    xs, ys = native.xy
    ax.plot(xs, ys, **kwargs)


def render_overlay(
    output_path: Path,
    rgb: np.ndarray,
    score: np.ndarray,
    transform_affine: Any,
    ortho_crs: CRS,
    input_line: LineString,
    refined_line: LineString,
    running_line: LineString | None,
    offsets_raw_m: np.ndarray,
    offsets_filtered_m: np.ndarray,
    contrast: np.ndarray,
    spacing_m: float,
) -> None:
    height, width = score.shape
    left = transform_affine.c
    top = transform_affine.f
    right = left + transform_affine.a * width
    bottom = top + transform_affine.e * height
    extent = [left, right, bottom, top]
    fig, axes = plt.subplots(2, 2, figsize=(14, 14), dpi=180)
    axes = axes.ravel()

    axes[0].imshow(score, extent=extent, cmap="PiYG", vmin=-1.5, vmax=1.5)
    axes[0].set_title("road-vs-green data score")

    axes[1].imshow(rgb, extent=extent)
    axes[1].plot(*input_line.xy, color="#ff4f7b", linewidth=1.0, alpha=0.55, label="piecewise input")
    axes[1].plot(*refined_line.xy, color="#00e0ff", linewidth=2.0, label="data-aware refined curb")
    if running_line is not None:
        axes[1].plot(*running_line.xy, color="#ffd84d", linewidth=1.7, label="2 ft running line")
    axes[1].set_title("data-aware boundary refinement")
    axes[1].legend(loc="lower right", frameon=True, fontsize=8)

    arclength = np.arange(offsets_raw_m.size) * spacing_m
    axes[2].plot(arclength, offsets_raw_m, color="#9e9ac8", linewidth=0.9, label="DP offsets")
    axes[2].plot(arclength, offsets_filtered_m, color="#54278f", linewidth=1.4, label="low-pass offsets")
    axes[2].axhline(0.0, color="#222222", linewidth=0.8, alpha=0.5)
    axes[2].set_xlabel("arclength (m)")
    axes[2].set_ylabel("normal offset from piecewise input (m)")
    axes[2].set_title("normal displacement field")
    axes[2].grid(True, alpha=0.25)
    axes[2].legend(frameon=False)

    chosen_contrast = np.max(contrast, axis=1)
    axes[3].plot(arclength, chosen_contrast, color="#238b45", linewidth=1.0)
    axes[3].set_xlabel("arclength (m)")
    axes[3].set_ylabel("inside - outside score")
    axes[3].set_title("best local data contrast")
    axes[3].grid(True, alpha=0.25)

    for ax in axes[:2]:
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
    parser.add_argument("--input-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-piecewise-curvature-candidates.geojson")
    parser.add_argument("--output-curb-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-data-aware-candidate.geojson")
    parser.add_argument("--output-running-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "running-line-2ft-data-aware-candidate.geojson")
    parser.add_argument("--overlay", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "data-aware-boundary-refinement.png")
    parser.add_argument("--diagnostics", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "data-aware-boundary-diagnostics.json")
    parser.add_argument("--downsample", type=int, default=2)
    parser.add_argument("--sample-spacing-m", type=float, default=0.45)
    parser.add_argument("--max-offset-m", type=float, default=0.9)
    parser.add_argument("--offset-step-m", type=float, default=0.1)
    parser.add_argument("--probe-distances-m", nargs="+", type=float, default=[0.45, 0.75, 1.05, 1.35])
    parser.add_argument("--smooth-weight", type=float, default=4.5)
    parser.add_argument("--offset-prior-weight", type=float, default=0.35)
    parser.add_argument("--offset-filter-wavelength-m", type=float, default=18.0)
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    offset_m = float(config["measurement"]["intended_offset"]["meters"])
    ortho_array, rgb, transform_affine, ortho_crs = load_ortho(args.ortho, args.downsample)
    unit_to_meter = unit_conversion_to_meters(ortho_crs)
    resolution_m = abs(float(transform_affine.a)) * unit_to_meter
    output_shape = (ortho_array.shape[1], ortho_array.shape[2])

    centerlines_4326 = load_centerlines(args.centerline_geojson, config)
    loop_polygon_4326 = loop_polygon_from_centerlines(centerlines_4326)
    centerlines_native = [transform_geometry(line, "EPSG:4326", ortho_crs) for line in centerlines_4326]
    loop_polygon_native = transform_geometry(loop_polygon_4326, "EPSG:4326", ortho_crs)
    road_mask, green_mask, seed_metadata = build_seed_masks(
        centerlines_native,
        loop_polygon_native,
        output_shape,
        transform_affine,
        unit_to_meter,
        road_seed_m=1.25,
        green_seed_inset_m=7.0,
    )
    lidar_layers = grid_lidar_to_ortho(args.lidar_npz, output_shape, transform_affine, ortho_crs, "EPSG:6347", resolution_m)
    feature_stack, feature_names = feature_stack_from_sources(ortho_array, lidar_layers)
    green_score, score_diagnostics = build_green_score(feature_stack, road_mask, green_mask, feature_names)
    gy, gx = np.gradient(green_score, resolution_m, resolution_m)
    gradient = normalize(np.hypot(gx, gy))

    input_line_4326, input_properties = load_selected_line(args.input_geojson)
    input_line_native = transform_geometry(input_line_4326, "EPSG:4326", ortho_crs)
    coords, spacing_units = resample_closed_line(input_line_native, meters_to_units(args.sample_spacing_m, unit_to_meter))
    if signed_area(coords) < 0:
        coords = coords[::-1]
    _tangents, normals = closed_tangents_normals(coords)
    inverse_transform = ~transform_affine
    offsets_m = np.arange(-args.max_offset_m, args.max_offset_m + args.offset_step_m * 0.5, args.offset_step_m)
    offsets_units = meters_to_units(offsets_m, unit_to_meter)
    probes_units = meters_to_units(np.asarray(args.probe_distances_m, dtype=float), unit_to_meter)
    unary, contrast = contrast_for_offsets(coords, normals, offsets_units, probes_units, green_score, gradient, inverse_transform)
    raw_offsets_m = solve_offset_path(unary, offsets_m, args.smooth_weight, args.offset_prior_weight)
    filtered_offsets_m = fourier_lowpass_periodic(raw_offsets_m, args.sample_spacing_m, args.offset_filter_wavelength_m)
    refined_coords = coords + normals * meters_to_units(filtered_offsets_m, unit_to_meter)[:, None]
    refined_line_native = LineString(np.vstack([refined_coords, refined_coords[0]]))

    refined_line_measurement = transform_geometry(refined_line_native, ortho_crs, measurement_crs)
    polygon = polygon_from_closed_line(refined_line_measurement)
    running_line: LineString | None = None
    if polygon is not None:
        running_polygon = polygon.buffer(-offset_m)
        if not running_polygon.is_empty:
            if running_polygon.geom_type == "MultiPolygon":
                running_polygon = max(running_polygon.geoms, key=lambda item: item.area)
            running_line = LineString(running_polygon.exterior.coords)

    curb_properties = {
        "method": "data_aware_boundary_refinement",
        "source": "piecewise_curvature_selected",
        "input_properties": input_properties,
        "length_m": float(refined_line_measurement.length),
        "length_km": float(refined_line_measurement.length / 1000.0),
        "length_miles": float(refined_line_measurement.length / 1609.344),
        "mean_offset_m": float(np.mean(filtered_offsets_m)),
        "rms_offset_m": float(np.sqrt(np.mean(filtered_offsets_m * filtered_offsets_m))),
        "max_abs_offset_m": float(np.max(np.abs(filtered_offsets_m))),
    }
    write_geojson(args.output_curb_geojson, [geojson_feature(refined_line_measurement, curb_properties, measurement_crs)])
    running_features = []
    if running_line is not None:
        running_features.append(
            geojson_feature(
                running_line,
                {
                    **curb_properties,
                    "offset_m": offset_m,
                    "length_m": float(running_line.length),
                    "length_km": float(running_line.length / 1000.0),
                    "length_miles": float(running_line.length / 1609.344),
                },
                measurement_crs,
            )
        )
    write_geojson(args.output_running_geojson, running_features)

    running_line_native = transform_geometry(running_line, measurement_crs, ortho_crs) if running_line is not None else None
    render_overlay(
        args.overlay,
        rgb,
        green_score,
        transform_affine,
        ortho_crs,
        input_line_native,
        refined_line_native,
        running_line_native,
        raw_offsets_m,
        filtered_offsets_m,
        contrast,
        args.sample_spacing_m,
    )
    diagnostics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_geojson": str(args.input_geojson),
        "input_properties": input_properties,
        "ortho": str(args.ortho),
        "lidar_npz": str(args.lidar_npz),
        "downsample": int(args.downsample),
        "resolution_m": float(resolution_m),
        "seed_metadata": seed_metadata,
        "score_model": score_diagnostics,
        "refinement": {
            "sample_spacing_m": float(args.sample_spacing_m),
            "max_offset_m": float(args.max_offset_m),
            "offset_step_m": float(args.offset_step_m),
            "probe_distances_m": [float(value) for value in args.probe_distances_m],
            "smooth_weight": float(args.smooth_weight),
            "offset_prior_weight": float(args.offset_prior_weight),
            "offset_filter_wavelength_m": float(args.offset_filter_wavelength_m),
            "raw_offset_mean_m": float(np.mean(raw_offsets_m)),
            "raw_offset_rms_m": float(np.sqrt(np.mean(raw_offsets_m * raw_offsets_m))),
            "raw_offset_max_abs_m": float(np.max(np.abs(raw_offsets_m))),
            "filtered_offset_mean_m": float(np.mean(filtered_offsets_m)),
            "filtered_offset_rms_m": float(np.sqrt(np.mean(filtered_offsets_m * filtered_offsets_m))),
            "filtered_offset_max_abs_m": float(np.max(np.abs(filtered_offsets_m))),
            "mean_best_contrast": float(np.mean(np.max(contrast, axis=1))),
        },
        "curb": curb_properties,
        "running_line": running_features[0]["properties"] if running_features else None,
    }
    write_json(args.diagnostics, diagnostics)
    print(f"wrote {args.output_curb_geojson}")
    print(f"wrote {args.output_running_geojson}")
    print(f"wrote {args.overlay}")
    print(f"wrote {args.diagnostics}")
    print(f"refined curb length: {curb_properties['length_m']:.3f} m")
    if running_line is not None:
        print(f"refined running length: {running_line.length:.3f} m")
    print(f"offset rms: {curb_properties['rms_offset_m']:.3f} m")


if __name__ == "__main__":
    main()
