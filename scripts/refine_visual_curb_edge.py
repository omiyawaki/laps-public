#!/usr/bin/env python3
"""Refine the curb candidate with a narrow visual curb-edge objective."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import yaml
from pyproj import CRS, Transformer
from scipy import ndimage
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import transform

from build_fused_segmentation import (
    build_seed_masks,
    load_centerlines,
    load_ortho,
    loop_polygon_from_centerlines,
    meters_to_units,
    ortho_features,
    transform_geometry,
    unit_conversion_to_meters,
)
from refine_data_aware_boundary import build_green_score, bilinear_sample


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEET_TO_METERS = 0.3048


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def write_geojson(path: Path, features: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle, indent=2)
        handle.write("\n")


def offset_slug(offset_m: float, offset_feet: float) -> str:
    if abs(offset_m - round(offset_m)) < 1e-9:
        return f"{int(round(offset_m))}m"
    if abs(offset_feet - round(offset_feet)) < 1e-9:
        return f"{int(round(offset_feet))}ft"
    if abs(offset_m * 10.0 - round(offset_m * 10.0)) < 1e-9:
        return f"{offset_m:g}m".replace(".", "p")
    return f"{offset_feet:g}ft".replace(".", "p")


def load_line_feature(path: Path, selected_ok: bool = True) -> tuple[LineString, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    fallback: tuple[LineString, dict[str, Any]] | None = None
    for feature in collection["features"]:
        geometry = shape(feature["geometry"])
        if not isinstance(geometry, LineString):
            continue
        props = dict(feature.get("properties", {}))
        if selected_ok and props.get("selected") is True:
            return geometry, props
        if fallback is None:
            fallback = (geometry, props)
    if fallback is None:
        raise ValueError(f"no LineString feature found in {path}")
    return fallback


def normalize(values: np.ndarray, percentiles: tuple[float, float] = (2, 98)) -> np.ndarray:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros_like(values, dtype=float)
    low, high = np.percentile(finite, percentiles)
    if high <= low:
        high = low + 1.0
    return np.clip((values - low) / (high - low), 0.0, 1.0)


def signed_area(coords: np.ndarray) -> float:
    x = coords[:, 0]
    y = coords[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - y * np.roll(x, -1)))


def resample_closed_line(line: LineString, spacing_m: float) -> tuple[np.ndarray, np.ndarray, float]:
    length = float(line.length)
    n_samples = max(128, int(round(length / spacing_m)))
    stations = np.linspace(0.0, length, n_samples, endpoint=False)
    points = [line.interpolate(float(station)) for station in stations]
    coords = np.array([[point.x, point.y] for point in points], dtype=float)
    return coords, stations, length / n_samples


def closed_normals(coords: np.ndarray) -> np.ndarray:
    tangent = np.roll(coords, -1, axis=0) - np.roll(coords, 1, axis=0)
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1)[:, None], 1e-9)
    left_normal = np.column_stack([-tangent[:, 1], tangent[:, 0]])
    orientation = 1.0 if signed_area(coords) >= 0 else -1.0
    return left_normal * orientation


def transform_points(points: np.ndarray, transformer: Transformer) -> np.ndarray:
    x, y = transformer.transform(points[:, 0], points[:, 1])
    return np.column_stack([x, y])


def sample_measurement_grid(
    grid: np.ndarray,
    points_measure: np.ndarray,
    measurement_to_ortho: Transformer,
    inverse_transform: Any,
) -> np.ndarray:
    points_ortho = transform_points(points_measure, measurement_to_ortho)
    return bilinear_sample(grid, inverse_transform, points_ortho)


def build_visual_grids(ortho_array: np.ndarray, image_score: np.ndarray, resolution_m: float) -> dict[str, np.ndarray]:
    image = ortho_features(ortho_array)
    luminance = image["luminance"]
    grayness = image["grayness"]
    green_excess = image["green_excess"]
    ndvi = image["ndvi"]
    curb_light = normalize(0.55 * grayness + 0.35 * luminance - 0.25 * green_excess - 0.10 * ndvi)
    score_grad = ndimage.gaussian_gradient_magnitude(image_score, sigma=max(0.35 / resolution_m, 0.8))
    light_grad = ndimage.gaussian_gradient_magnitude(curb_light, sigma=max(0.25 / resolution_m, 0.6))
    edge_strength = normalize(0.65 * normalize(score_grad) + 0.35 * normalize(light_grad))
    return {
        "image_score": image_score,
        "curb_light": curb_light,
        "luminance": luminance,
        "grayness": grayness,
        "green_excess": green_excess,
        "edge_strength": edge_strength,
    }


def candidate_points(coords: np.ndarray, normals: np.ndarray, offsets_m: np.ndarray) -> np.ndarray:
    return coords[:, None, :] + normals[:, None, :] * offsets_m[None, :, None]


def sample_candidates(
    grid: np.ndarray,
    coords: np.ndarray,
    normals: np.ndarray,
    offsets_m: np.ndarray,
    normal_shift_m: float,
    measurement_to_ortho: Transformer,
    inverse_transform: Any,
) -> np.ndarray:
    points = candidate_points(coords, normals, offsets_m) + normals[:, None, :] * normal_shift_m
    flat = points.reshape(-1, 2)
    sampled = sample_measurement_grid(grid, flat, measurement_to_ortho, inverse_transform)
    return sampled.reshape(coords.shape[0], offsets_m.size)


def build_unary_scores(
    visual_grids: dict[str, np.ndarray],
    coords: np.ndarray,
    normals: np.ndarray,
    offsets_m: np.ndarray,
    measurement_to_ortho: Transformer,
    inverse_transform: Any,
) -> tuple[np.ndarray, dict[str, Any]]:
    probes_inside = [0.25, 0.45, 0.70, 0.95]
    probes_outside = [-0.25, -0.45, -0.70, -0.95]
    image_score = visual_grids["image_score"]
    curb_light = visual_grids["curb_light"]
    edge_strength = visual_grids["edge_strength"]
    green_excess = visual_grids["green_excess"]

    center_score = sample_candidates(image_score, coords, normals, offsets_m, 0.0, measurement_to_ortho, inverse_transform)
    inside_score = np.nanmean(
        np.stack([sample_candidates(image_score, coords, normals, offsets_m, probe, measurement_to_ortho, inverse_transform) for probe in probes_inside]),
        axis=0,
    )
    outside_score = np.nanmean(
        np.stack([sample_candidates(image_score, coords, normals, offsets_m, probe, measurement_to_ortho, inverse_transform) for probe in probes_outside]),
        axis=0,
    )
    region_contrast = inside_score - outside_score
    near_gradient = (
        sample_candidates(image_score, coords, normals, offsets_m, 0.22, measurement_to_ortho, inverse_transform)
        - sample_candidates(image_score, coords, normals, offsets_m, -0.22, measurement_to_ortho, inverse_transform)
    )
    center_light = sample_candidates(curb_light, coords, normals, offsets_m, 0.0, measurement_to_ortho, inverse_transform)
    flank_light = 0.5 * (
        sample_candidates(curb_light, coords, normals, offsets_m, 0.35, measurement_to_ortho, inverse_transform)
        + sample_candidates(curb_light, coords, normals, offsets_m, -0.35, measurement_to_ortho, inverse_transform)
    )
    ridge = center_light - flank_light
    edge = sample_candidates(edge_strength, coords, normals, offsets_m, 0.0, measurement_to_ortho, inverse_transform)
    inside_green = np.nanmean(
        np.stack([sample_candidates(green_excess, coords, normals, offsets_m, probe, measurement_to_ortho, inverse_transform) for probe in probes_inside]),
        axis=0,
    )
    outside_green = np.nanmean(
        np.stack([sample_candidates(green_excess, coords, normals, offsets_m, probe, measurement_to_ortho, inverse_transform) for probe in probes_outside]),
        axis=0,
    )
    green_direction = inside_green - outside_green

    raw = (
        1.45 * region_contrast
        + 0.85 * near_gradient
        + 0.70 * edge
        + 0.45 * ridge
        + 0.30 * center_light
        + 0.25 * green_direction
    )
    raw = np.nan_to_num(raw, nan=-4.0, posinf=-4.0, neginf=-4.0)
    median = np.median(raw, axis=1, keepdims=True)
    spread = np.percentile(raw, 90, axis=1, keepdims=True) - np.percentile(raw, 10, axis=1, keepdims=True)
    confidence = np.clip(spread[:, 0] / 0.75, 0.20, 1.0)
    z = (raw - median) / np.maximum(spread, 0.20)
    scores = np.clip(z, -4.0, 4.0) * confidence[:, None]
    diagnostics = {
        "probe_inside_m": probes_inside,
        "probe_outside_m": probes_outside,
        "score_terms": {
            "region_contrast": 1.45,
            "near_gradient": 0.85,
            "edge_strength": 0.70,
            "curb_light_ridge": 0.45,
            "center_curb_light": 0.30,
            "green_direction": 0.25,
        },
        "mean_offset_score_spread": float(np.mean(spread)),
        "median_offset_score_spread": float(np.median(spread)),
        "mean_confidence": float(np.mean(confidence)),
    }
    return scores, diagnostics


def solve_second_order_offsets(
    scores: np.ndarray,
    offsets_m: np.ndarray,
    first_diff_weight: float,
    second_diff_weight: float,
    weak_prior_weight: float,
    roll_start: int,
) -> np.ndarray:
    if roll_start:
        scores_work = np.roll(scores, -roll_start, axis=0)
    else:
        scores_work = scores
    n, states = scores_work.shape
    unary = -scores_work + weak_prior_weight * offsets_m[None, :] ** 2
    first = first_diff_weight * (offsets_m[:, None] - offsets_m[None, :]) ** 2
    dp = unary[0, :, None] + unary[1, None, :] + first
    previous = np.zeros((n, states, states), dtype=np.int16)
    offset_a = offsets_m[:, None]
    offset_b = offsets_m[None, :]
    for index in range(2, n):
        new_dp = np.empty((states, states), dtype=float)
        for curr_state, offset_c in enumerate(offsets_m):
            transition = (
                dp
                + first_diff_weight * (offset_b - offset_c) ** 2
                + second_diff_weight * (offset_c - 2.0 * offset_b + offset_a) ** 2
            )
            best_prev = np.argmin(transition, axis=0)
            new_dp[:, curr_state] = transition[best_prev, np.arange(states)] + unary[index, curr_state]
            previous[index, :, curr_state] = best_prev
        dp = new_dp

    prev_state, curr_state = np.unravel_index(int(np.argmin(dp)), dp.shape)
    path = np.empty(n, dtype=int)
    path[-1] = curr_state
    path[-2] = prev_state
    for index in range(n - 1, 1, -1):
        prior_state = int(previous[index, path[index - 1], path[index]])
        path[index - 2] = prior_state
    offsets = offsets_m[path]
    if roll_start:
        offsets = np.roll(offsets, roll_start)
    return offsets


def fourier_lowpass_periodic(values: np.ndarray, spacing_m: float, cutoff_wavelength_m: float, order: int = 6) -> np.ndarray:
    frequencies = np.fft.fftfreq(values.size, d=spacing_m)
    cutoff = 1.0 / cutoff_wavelength_m
    with np.errstate(divide="ignore", invalid="ignore"):
        weights = 1.0 / np.sqrt(1.0 + (np.abs(frequencies) / cutoff) ** (2 * order))
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


def sample_at_station(stations: np.ndarray, values: np.ndarray, station_m: float) -> float:
    spacing = float(stations[1] - stations[0])
    lap_length = float(stations[-1] + spacing)
    station = station_m % lap_length
    return float(np.interp(station, np.r_[stations, lap_length], np.r_[values, values[0]]))


def plot_line(ax: Any, line: LineString, src_crs: Any, dst_crs: Any, **kwargs: Any) -> None:
    native = transform_geometry(line, src_crs, dst_crs)
    xs, ys = native.xy
    ax.plot(xs, ys, **kwargs)


def render_overlay(
    output_path: Path,
    rgb: np.ndarray,
    transform_affine: Any,
    ortho_crs: CRS,
    measurement_crs: CRS,
    input_line_4326: LineString,
    refined_line_4326: LineString,
    refined_line_measure: LineString,
    running_line_4326: LineString | None,
    stations: np.ndarray,
    raw_offsets_m: np.ndarray,
    filtered_offsets_m: np.ndarray,
    score_at_path: np.ndarray,
    flagged_stations_m: list[float],
) -> None:
    height, width = rgb.shape[:2]
    left = transform_affine.c
    top = transform_affine.f
    right = left + transform_affine.a * width
    bottom = top + transform_affine.e * height
    extent = [left, right, bottom, top]
    fig = plt.figure(figsize=(15, 14), dpi=180)
    grid = fig.add_gridspec(2, 2)
    axes = [fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[0, 1]), fig.add_subplot(grid[1, 0]), fig.add_subplot(grid[1, 1])]

    axes[0].imshow(rgb, extent=extent)
    plot_line(axes[0], input_line_4326, "EPSG:4326", ortho_crs, color="#ff5f99", linewidth=1.5, linestyle="--", label="input corridor center")
    plot_line(axes[0], refined_line_4326, "EPSG:4326", ortho_crs, color="#00e676", linewidth=2.2, label="visual edge refined")
    if running_line_4326 is not None:
        plot_line(axes[0], running_line_4326, "EPSG:4326", ortho_crs, color="#ffd84d", linewidth=1.5, label="configured running line")
    axes[0].set_title("visual curb-edge refinement")
    axes[0].legend(loc="lower right", frameon=True, fontsize=7)

    crop_half_width = 18.0
    unit_to_meter = unit_conversion_to_meters(ortho_crs)
    for ax, station in zip(axes[1:], flagged_stations_m, strict=False):
        ax.imshow(rgb, extent=extent)
        plot_line(ax, input_line_4326, "EPSG:4326", ortho_crs, color="#ff5f99", linewidth=1.5, linestyle="--", alpha=0.85)
        plot_line(ax, refined_line_4326, "EPSG:4326", ortho_crs, color="#00e676", linewidth=2.3)
        value_raw = sample_at_station(stations, raw_offsets_m, station)
        value_filtered = sample_at_station(stations, filtered_offsets_m, station)
        score_value = sample_at_station(stations, score_at_path, station)
        point_measure = refined_line_measure.interpolate(float(station) % float(refined_line_measure.length))
        point_native = transform_geometry(point_measure, measurement_crs, ortho_crs)
        ax.scatter([point_native.x], [point_native.y], s=36, color="#ffea61", edgecolor="#111111", linewidth=0.8, zorder=5)
        ax.annotate(
            f"{station:.3f} m\n{value_filtered:+.2f} m",
            (point_native.x, point_native.y),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=7,
            color="#111111",
            bbox={"boxstyle": "round,pad=0.18", "fc": "#ffea61", "ec": "none", "alpha": 0.92},
        )
        crop_half_width_units = meters_to_units(crop_half_width, unit_to_meter)
        ax.set_xlim(point_native.x - crop_half_width_units, point_native.x + crop_half_width_units)
        ax.set_ylim(point_native.y - crop_half_width_units, point_native.y + crop_half_width_units)
        ax.set_title(f"station {station:.3f} m, offset {value_raw:+.2f}->{value_filtered:+.2f} m, score {score_value:.2f}")

    for ax in axes:
        ax.set_aspect("equal")
        ax.set_axis_off()
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--ortho", type=Path, default=PROJECT_ROOT / "data" / "interim" / "ortho" / "example-2024-loop-tight.tif")
    parser.add_argument("--centerline-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "loop-centerlines.geojson")
    parser.add_argument("--input-curb-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-data-aware-candidate.geojson")
    parser.add_argument("--output-curb-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-visual-candidate.geojson")
    parser.add_argument("--output-running-geojson", type=Path, default=None)
    parser.add_argument("--overlay", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "visual-curb-edge-refinement.png")
    parser.add_argument("--diagnostics", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "visual-curb-edge-diagnostics.json")
    parser.add_argument("--sample-spacing-m", type=float, default=0.20)
    parser.add_argument("--max-offset-m", type=float, default=1.05)
    parser.add_argument("--offset-step-m", type=float, default=0.05)
    parser.add_argument("--first-diff-weight", type=float, default=55.0)
    parser.add_argument("--second-diff-weight", type=float, default=185.0)
    parser.add_argument("--weak-prior-weight", type=float, default=0.10)
    parser.add_argument("--offset-filter-wavelength-m", type=float, default=5.0)
    parser.add_argument("--roll-start-station-m", type=float, default=95.0)
    parser.add_argument("--flag-stations-m", nargs="+", type=float, default=[15.399, 35.197, 62.295])
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    running_offset_m = float(config["measurement"]["intended_offset"]["meters"])
    running_offset_feet = running_offset_m / FEET_TO_METERS
    output_running_geojson = args.output_running_geojson or PROJECT_ROOT / "geometry" / f"running-line-{offset_slug(running_offset_m, running_offset_feet)}-visual-candidate.geojson"

    ortho_array, rgb, transform_affine, ortho_crs = load_ortho(args.ortho, downsample=1)
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
    image = ortho_features(ortho_array)
    image_stack = np.dstack(
        [
            image["red"],
            image["green"],
            image["blue"],
            image["nir"],
            image["luminance"],
            image["chroma"],
            image["grayness"],
            image["ndvi"],
            image["green_excess"],
        ]
    )
    image_names = ["red", "green", "blue", "nir", "luminance", "chroma", "grayness", "ndvi", "green_excess"]
    image_score, score_diagnostics = build_green_score(image_stack, road_mask, green_mask, image_names)
    visual_grids = build_visual_grids(ortho_array, image_score, resolution_m)

    input_line_4326, input_props = load_line_feature(args.input_curb_geojson, selected_ok=False)
    input_line_measure = transform_geometry(input_line_4326, "EPSG:4326", measurement_crs)
    coords, stations, spacing_m = resample_closed_line(input_line_measure, args.sample_spacing_m)
    normals = closed_normals(coords)
    offsets_m = np.arange(-args.max_offset_m, args.max_offset_m + args.offset_step_m * 0.5, args.offset_step_m)
    measurement_to_ortho = Transformer.from_crs(measurement_crs, ortho_crs, always_xy=True)
    inverse_transform = ~transform_affine
    scores, unary_diagnostics = build_unary_scores(visual_grids, coords, normals, offsets_m, measurement_to_ortho, inverse_transform)
    roll_start = int(np.argmin(np.abs(stations - args.roll_start_station_m)))
    raw_offsets_m = solve_second_order_offsets(
        scores,
        offsets_m,
        args.first_diff_weight,
        args.second_diff_weight,
        args.weak_prior_weight,
        roll_start=roll_start,
    )
    filtered_offsets_m = fourier_lowpass_periodic(raw_offsets_m, spacing_m, args.offset_filter_wavelength_m)
    refined_coords = coords + normals * filtered_offsets_m[:, None]
    refined_line_measure = LineString(np.vstack([refined_coords, refined_coords[0]]))
    refined_line_4326 = transform_geometry(refined_line_measure, measurement_crs, "EPSG:4326")
    polygon = polygon_from_closed_line(refined_line_measure)
    running_line_measure: LineString | None = None
    if polygon is not None:
        running_polygon = polygon.buffer(-running_offset_m)
        if not running_polygon.is_empty:
            if running_polygon.geom_type == "MultiPolygon":
                running_polygon = max(running_polygon.geoms, key=lambda item: item.area)
            running_line_measure = LineString(running_polygon.exterior.coords)
    running_line_4326 = transform_geometry(running_line_measure, measurement_crs, "EPSG:4326") if running_line_measure is not None else None

    chosen_indexes = np.array([int(np.argmin(np.abs(offsets_m - offset))) for offset in raw_offsets_m], dtype=int)
    score_at_path = scores[np.arange(scores.shape[0]), chosen_indexes]
    curb_props = {
        "method": "visual_curb_edge_refinement",
        "source": "data_aware_boundary_refinement",
        "input_properties": input_props,
        "length_m": float(refined_line_measure.length),
        "length_km": float(refined_line_measure.length / 1000.0),
        "length_miles": float(refined_line_measure.length / 1609.344),
        "mean_offset_from_input_m": float(np.mean(filtered_offsets_m)),
        "rms_offset_from_input_m": float(np.sqrt(np.mean(filtered_offsets_m * filtered_offsets_m))),
        "max_abs_offset_from_input_m": float(np.max(np.abs(filtered_offsets_m))),
    }
    write_geojson(args.output_curb_geojson, [geojson_feature(refined_line_measure, curb_props, measurement_crs)])
    running_features: list[dict[str, Any]] = []
    if running_line_measure is not None:
        running_features.append(
            geojson_feature(
                running_line_measure,
                {
                    **curb_props,
                    "offset_feet": float(running_offset_feet),
                    "offset_m": running_offset_m,
                    "length_m": float(running_line_measure.length),
                    "length_km": float(running_line_measure.length / 1000.0),
                    "length_miles": float(running_line_measure.length / 1609.344),
                },
                measurement_crs,
            )
        )
    write_geojson(output_running_geojson, running_features)
    render_overlay(
        args.overlay,
        rgb,
        transform_affine,
        ortho_crs,
        measurement_crs,
        input_line_4326,
        refined_line_4326,
        refined_line_measure,
        running_line_4326,
        stations,
        raw_offsets_m,
        filtered_offsets_m,
        score_at_path,
        args.flag_stations_m,
    )

    flagged = []
    for station in args.flag_stations_m:
        flagged.append(
            {
                "station_m": float(station),
                "raw_offset_m": float(sample_at_station(stations, raw_offsets_m, station)),
                "filtered_offset_m": float(sample_at_station(stations, filtered_offsets_m, station)),
                "visual_score": float(sample_at_station(stations, score_at_path, station)),
            }
        )
    diagnostics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_curb_geojson": str(args.input_curb_geojson),
        "input_properties": input_props,
        "ortho": str(args.ortho),
        "centerline_geojson": str(args.centerline_geojson),
        "seed_metadata": seed_metadata,
        "image_score_model": score_diagnostics,
        "unary_score": unary_diagnostics,
        "optimizer": {
            "sample_spacing_m": float(spacing_m),
            "max_offset_m": float(args.max_offset_m),
            "offset_step_m": float(args.offset_step_m),
            "first_diff_weight": float(args.first_diff_weight),
            "second_diff_weight": float(args.second_diff_weight),
            "weak_prior_weight": float(args.weak_prior_weight),
            "offset_filter_wavelength_m": float(args.offset_filter_wavelength_m),
            "roll_start_station_m": float(args.roll_start_station_m),
            "roll_start_index": int(roll_start),
            "raw_offset_rms_m": float(np.sqrt(np.mean(raw_offsets_m * raw_offsets_m))),
            "filtered_offset_rms_m": float(np.sqrt(np.mean(filtered_offsets_m * filtered_offsets_m))),
        },
        "flagged_stations": flagged,
        "curb": curb_props,
        "running_line": running_features[0]["properties"] if running_features else None,
    }
    write_json(args.diagnostics, diagnostics)
    print(f"wrote {args.output_curb_geojson}")
    print(f"wrote {output_running_geojson}")
    print(f"wrote {args.overlay}")
    print(f"wrote {args.diagnostics}")
    print(f"visual curb length: {curb_props['length_m']:.3f} m")
    if running_line_measure is not None:
        print(f"visual running length: {running_line_measure.length:.3f} m")
    for record in flagged:
        print(
            f"{record['station_m']:.3f} m: offset {record['filtered_offset_m']:+.3f} m "
            f"(raw {record['raw_offset_m']:+.3f}), score {record['visual_score']:+.2f}"
        )


if __name__ == "__main__":
    main()
