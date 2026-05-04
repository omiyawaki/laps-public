#!/usr/bin/env python3
"""Fit a sparse piecewise line/arc model to the smoothed fused curb contour."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import rasterio
import yaml
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import transform

from build_baseline_geometry import stretch_rgb


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SegmentModel:
    model_type: str
    degree: int
    coeffs: tuple[float, ...]
    start: int
    end: int
    cost: float
    fit_cost: float
    cap_cost: float
    complexity_cost: float
    rms_fit_m: float
    p95_abs_fit_m: float
    max_abs_curvature: float
    radius_min_m: float | None
    chord_length_m: float


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


def load_smoothed_line(path: Path, cutoff_m: float | None) -> tuple[LineString, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    selected_feature: dict[str, Any] | None = None
    for feature in collection["features"]:
        properties = feature.get("properties", {})
        if cutoff_m is not None and abs(float(properties.get("cutoff_wavelength_m", -999.0)) - cutoff_m) < 1e-9:
            selected_feature = feature
            break
        if cutoff_m is None and properties.get("selected"):
            selected_feature = feature
    if selected_feature is None:
        raise ValueError(f"no selected/cutoff feature found in {path}")
    geometry = shape(selected_feature["geometry"])
    if not isinstance(geometry, LineString):
        raise ValueError(f"selected geometry is {geometry.geom_type}, expected LineString")
    return geometry, dict(selected_feature.get("properties", {}))


def load_ortho_preview(path: Path) -> tuple[np.ndarray, CRS, tuple[float, float, float, float]]:
    with rasterio.open(path) as dataset:
        rgb = stretch_rgb(dataset.read())
        crs = CRS.from_user_input(dataset.crs)
        bounds = (dataset.bounds.left, dataset.bounds.bottom, dataset.bounds.right, dataset.bounds.top)
    return rgb, crs, bounds


def resample_closed_line(line: LineString, spacing_m: float) -> tuple[np.ndarray, float, float]:
    total_length = float(line.length)
    n_samples = max(64, int(round(total_length / spacing_m)))
    distances = np.linspace(0.0, total_length, n_samples, endpoint=False)
    points = [line.interpolate(float(distance)) for distance in distances]
    coords = np.array([[point.x, point.y] for point in points], dtype=float)
    return coords, total_length / n_samples, total_length


def signed_area(coords: np.ndarray) -> float:
    x = coords[:, 0]
    y = coords[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - y * np.roll(x, -1)))


def rotate_array(values: np.ndarray, start: int) -> np.ndarray:
    return np.concatenate([values[start:], values[:start]], axis=0)


def choose_start_index(coords: np.ndarray) -> int:
    # Start on a long, visually low-curvature side so the open DP boundary is not
    # placed inside the tight north/south bends.
    y = coords[:, 1]
    x = coords[:, 0]
    score = y - 0.15 * x
    return int(np.argmin(score))


def local_chord_coordinates(points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]:
    p0 = points[0]
    p1 = points[-1]
    chord = p1 - p0
    length = float(np.linalg.norm(chord))
    if length <= 1e-9:
        raise ValueError("degenerate segment")
    tangent = chord / length
    normal = np.array([-tangent[1], tangent[0]], dtype=float)
    relative = points - p0
    x = relative @ tangent
    y = relative @ normal
    return x, y, p0, tangent, length


def basis_matrix(u: np.ndarray, degree: int) -> np.ndarray:
    if degree < 0:
        return np.zeros((u.size, 0), dtype=float)
    q = 2.0 * u - 1.0
    envelope = 4.0 * u * (1.0 - u)
    columns = [envelope]
    if degree >= 1:
        columns.append(envelope * q)
    if degree >= 2:
        columns.append(envelope * (q * q - 1.0 / 3.0))
    return np.column_stack(columns)


def predict_lateral(u: np.ndarray, coeffs: tuple[float, ...]) -> np.ndarray:
    if not coeffs:
        return np.zeros_like(u, dtype=float)
    return basis_matrix(u, len(coeffs) - 1) @ np.asarray(coeffs, dtype=float)


def curvature_of_lateral_model(u: np.ndarray, coeffs: tuple[float, ...], chord_length_m: float) -> np.ndarray:
    if not coeffs:
        return np.zeros_like(u, dtype=float)
    du = max(float(np.median(np.diff(u))) if u.size > 1 else 0.01, 0.005)
    y = predict_lateral(u, coeffs)
    dy_du = np.gradient(y, du, edge_order=2)
    d2y_du2 = np.gradient(dy_du, du, edge_order=2)
    dy_dx = dy_du / chord_length_m
    d2y_dx2 = d2y_du2 / (chord_length_m * chord_length_m)
    return d2y_dx2 / np.maximum((1.0 + dy_dx * dy_dx) ** 1.5, 1e-9)


def fit_segment_model(
    coords: np.ndarray,
    start: int,
    end: int,
    spacing_m: float,
    model_type: str,
    degree: int,
    physical_radius_m: float,
    curvature_cap_weight: float,
    complexity_cost: float,
    robust_residual_m: float,
) -> SegmentModel | None:
    points = coords[start:end]
    if points.shape[0] < 4:
        return None
    try:
        x, y, _p0, _tangent, chord_length = local_chord_coordinates(points)
    except ValueError:
        return None
    if chord_length < 1e-6:
        return None
    u = np.clip(x / chord_length, 0.0, 1.0)
    if degree < 0:
        coeffs: tuple[float, ...] = ()
        y_hat = np.zeros_like(y)
    else:
        design = basis_matrix(u, degree)
        coeff_array, *_ = np.linalg.lstsq(design, y, rcond=None)
        coeffs = tuple(float(value) for value in coeff_array)
        y_hat = design @ coeff_array

    residual = y - y_hat
    segment_length = max(float((end - start) * spacing_m), chord_length)
    fit_cost = float(np.mean(np.minimum(residual * residual, robust_residual_m * robust_residual_m)) * segment_length)
    sample_u = np.linspace(0.0, 1.0, max(25, int(round(segment_length / 0.5))))
    curvature = curvature_of_lateral_model(sample_u, coeffs, chord_length)
    max_allowed_curvature = 1.0 / physical_radius_m
    excess = np.maximum(np.abs(curvature) - max_allowed_curvature, 0.0)
    cap_cost = float(curvature_cap_weight * np.mean(excess * excess) * segment_length)
    max_abs_curvature = float(np.max(np.abs(curvature))) if curvature.size else 0.0
    radius_min_m = float(1.0 / max_abs_curvature) if max_abs_curvature > 1e-9 else None
    return SegmentModel(
        model_type=model_type,
        degree=degree,
        coeffs=coeffs,
        start=start,
        end=end,
        cost=fit_cost + cap_cost + complexity_cost,
        fit_cost=fit_cost,
        cap_cost=cap_cost,
        complexity_cost=complexity_cost,
        rms_fit_m=float(np.sqrt(np.mean(residual * residual))),
        p95_abs_fit_m=float(np.percentile(np.abs(residual), 95)),
        max_abs_curvature=max_abs_curvature,
        radius_min_m=radius_min_m,
        chord_length_m=chord_length,
    )


def best_segment_model(
    coords: np.ndarray,
    start: int,
    end: int,
    spacing_m: float,
    physical_radius_m: float,
    curvature_cap_weight: float,
    complexity_weights: dict[str, float],
    robust_residual_m: float,
) -> SegmentModel:
    candidates = [
        fit_segment_model(
            coords,
            start,
            end,
            spacing_m,
            "line",
            -1,
            physical_radius_m,
            curvature_cap_weight,
            complexity_weights["line"],
            robust_residual_m,
        ),
        fit_segment_model(
            coords,
            start,
            end,
            spacing_m,
            "circle_arc",
            0,
            physical_radius_m,
            curvature_cap_weight,
            complexity_weights["circle_arc"],
            robust_residual_m,
        ),
        fit_segment_model(
            coords,
            start,
            end,
            spacing_m,
            "clothoid_like",
            1,
            physical_radius_m,
            curvature_cap_weight,
            complexity_weights["clothoid_like"],
            robust_residual_m,
        ),
        fit_segment_model(
            coords,
            start,
            end,
            spacing_m,
            "flex_arc",
            2,
            physical_radius_m,
            curvature_cap_weight,
            complexity_weights["flex_arc"],
            robust_residual_m,
        ),
    ]
    valid = [candidate for candidate in candidates if candidate is not None]
    if not valid:
        raise ValueError(f"no valid segment model for {start}:{end}")
    return min(valid, key=lambda item: item.cost)


def precompute_segment_models(
    coords: np.ndarray,
    spacing_m: float,
    min_segment_m: float,
    max_segment_m: float,
    physical_radius_m: float,
    curvature_cap_weight: float,
    complexity_weights: dict[str, float],
    robust_residual_m: float,
) -> dict[tuple[int, int], SegmentModel]:
    n = coords.shape[0]
    min_points = max(5, int(round(min_segment_m / spacing_m)))
    max_points = max(min_points, int(round(max_segment_m / spacing_m)))
    models: dict[tuple[int, int], SegmentModel] = {}
    for start in range(n):
        max_end = min(n, start + max_points)
        for end in range(start + min_points, max_end + 1):
            models[(start, end)] = best_segment_model(
                coords,
                start,
                end,
                spacing_m,
                physical_radius_m,
                curvature_cap_weight,
                complexity_weights,
                robust_residual_m,
            )
    return models


def solve_segments(
    n: int,
    spacing_m: float,
    min_segment_m: float,
    max_segment_m: float,
    segment_penalty: float,
    models: dict[tuple[int, int], SegmentModel],
) -> list[SegmentModel]:
    min_points = max(5, int(round(min_segment_m / spacing_m)))
    max_points = max(min_points, int(round(max_segment_m / spacing_m)))
    dp = np.full(n + 1, np.inf, dtype=float)
    previous = np.full(n + 1, -1, dtype=int)
    dp[0] = 0.0
    for end in range(min_points, n + 1):
        best_cost = np.inf
        best_start = -1
        for start in range(max(0, end - max_points), end - min_points + 1):
            model = models.get((start, end))
            if model is None or not np.isfinite(dp[start]):
                continue
            cost = dp[start] + model.cost + segment_penalty
            if cost < best_cost:
                best_cost = cost
                best_start = start
        dp[end] = best_cost
        previous[end] = best_start
    if previous[n] < 0:
        raise ValueError("no valid dynamic-programming solution")
    chosen: list[SegmentModel] = []
    end = n
    while end > 0:
        start = int(previous[end])
        chosen.append(models[(start, end)])
        end = start
    chosen.reverse()
    return chosen


def sample_segment_model(coords: np.ndarray, model: SegmentModel, spacing_m: float) -> np.ndarray:
    points = coords[model.start:model.end]
    _x, _y, p0, tangent, chord_length = local_chord_coordinates(points)
    normal = np.array([-tangent[1], tangent[0]], dtype=float)
    n_samples = max(2, int(round(chord_length / spacing_m)))
    u = np.linspace(0.0, 1.0, n_samples, endpoint=False)
    x = u * chord_length
    y = predict_lateral(u, model.coeffs)
    return p0 + np.outer(x, tangent) + np.outer(y, normal)


def build_model_coords(coords: np.ndarray, segments: list[SegmentModel], spacing_m: float) -> np.ndarray:
    pieces = [sample_segment_model(coords, segment, spacing_m) for segment in segments]
    model_coords = np.vstack(pieces)
    if np.linalg.norm(model_coords[0] - model_coords[-1]) > spacing_m * 1.5:
        model_coords = np.vstack([model_coords, model_coords[0]])
    return model_coords


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


def point_line_distances(points: np.ndarray, line: LineString) -> np.ndarray:
    from shapely.geometry import Point

    distances = np.empty(points.shape[0], dtype=float)
    for index, (x, y) in enumerate(points):
        distances[index] = line.distance(Point(float(x), float(y)))
    return distances


def curvature_of_coords(coords: np.ndarray, spacing_m: float) -> np.ndarray:
    x = coords[:, 0]
    y = coords[:, 1]
    dx = np.gradient(x, spacing_m, edge_order=2)
    dy = np.gradient(y, spacing_m, edge_order=2)
    ddx = np.gradient(dx, spacing_m, edge_order=2)
    ddy = np.gradient(dy, spacing_m, edge_order=2)
    return (dx * ddy - dy * ddx) / np.maximum((dx * dx + dy * dy) ** 1.5, 1e-9)


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


def summarize_model(
    key: str,
    segment_penalty: float,
    segments: list[SegmentModel],
    reference_coords: np.ndarray,
    line: LineString,
    running_line: LineString | None,
) -> dict[str, Any]:
    distances = point_line_distances(reference_coords, line)
    type_counts: dict[str, int] = {}
    for segment in segments:
        type_counts[segment.model_type] = type_counts.get(segment.model_type, 0) + 1
    return {
        "key": key,
        "segment_penalty": float(segment_penalty),
        "segment_count": int(len(segments)),
        "type_counts": type_counts,
        "curb_length_m": float(line.length),
        "running_length_m": float(running_line.length) if running_line is not None else None,
        "reference_distance_mean_m": float(np.mean(distances)),
        "reference_distance_rms_m": float(np.sqrt(np.mean(distances * distances))),
        "reference_distance_p95_m": float(np.percentile(distances, 95)),
        "reference_distance_max_m": float(np.max(distances)),
        "model_cost": float(sum(segment.cost + segment_penalty for segment in segments)),
        "segments": [
            {
                "start_index": int(segment.start),
                "end_index": int(segment.end),
                "length_m": float((segment.end - segment.start) * (reference_coords.shape[0] and line.length / max(reference_coords.shape[0], 1))),
                "model_type": segment.model_type,
                "degree": int(segment.degree),
                "coeffs": list(segment.coeffs),
                "cost": float(segment.cost),
                "fit_cost": float(segment.fit_cost),
                "cap_cost": float(segment.cap_cost),
                "complexity_cost": float(segment.complexity_cost),
                "rms_fit_m": float(segment.rms_fit_m),
                "p95_abs_fit_m": float(segment.p95_abs_fit_m),
                "radius_min_m": segment.radius_min_m,
                "chord_length_m": float(segment.chord_length_m),
            }
            for segment in segments
        ],
    }


def selected_model_key(models: dict[str, Any], target_rms_m: float) -> str:
    candidates = [
        (key, value)
        for key, value in models.items()
        if float(value["reference_distance_rms_m"]) <= target_rms_m
    ]
    if not candidates:
        return min(models, key=lambda key: (models[key]["reference_distance_rms_m"], models[key]["segment_count"]))
    return min(candidates, key=lambda item: (item[1]["segment_count"], item[1]["reference_distance_rms_m"]))[0]


def plot_line(ax: Any, line: LineString, src_crs: Any, dst_crs: Any, **kwargs: Any) -> None:
    native = transform_geometry(line, src_crs, dst_crs)
    xs, ys = native.xy
    ax.plot(xs, ys, **kwargs)


def render_overlay(
    output_path: Path,
    ortho_path: Path,
    input_line_4326: LineString,
    model_lines: dict[str, LineString],
    running_lines: dict[str, LineString],
    selected_key: str,
    diagnostics: dict[str, Any],
    measurement_crs: CRS,
) -> None:
    rgb, ortho_crs, bounds = load_ortho_preview(ortho_path)
    extent = [bounds[0], bounds[2], bounds[1], bounds[3]]
    fig, axes = plt.subplots(2, 2, figsize=(14, 14), dpi=180)
    axes = axes.ravel()

    axes[0].imshow(rgb, extent=extent)
    plot_line(axes[0], input_line_4326, "EPSG:4326", ortho_crs, color="#ff4f7b", linewidth=0.9, alpha=0.45, label="8 m filtered input")
    for key, line in model_lines.items():
        linewidth = 2.2 if key == selected_key else 1.0
        alpha = 1.0 if key == selected_key else 0.45
        plot_line(axes[0], line, measurement_crs, ortho_crs, linewidth=linewidth, alpha=alpha, label=key)
    axes[0].set_title("piecewise line/arc sweep")
    axes[0].legend(loc="lower right", frameon=True, fontsize=8)

    axes[1].imshow(rgb, extent=extent)
    plot_line(axes[1], input_line_4326, "EPSG:4326", ortho_crs, color="#ff4f7b", linewidth=0.9, alpha=0.35, label="8 m filtered input")
    plot_line(axes[1], model_lines[selected_key], measurement_crs, ortho_crs, color="#00e0ff", linewidth=2.2, label=f"{selected_key} curb")
    if selected_key in running_lines:
        plot_line(axes[1], running_lines[selected_key], measurement_crs, ortho_crs, color="#ffd84d", linewidth=1.7, label="2 ft running line")
    axes[1].set_title("selected piecewise model")
    axes[1].legend(loc="lower right", frameon=True, fontsize=8)

    keys = list(diagnostics["models"].keys())
    x = np.arange(len(keys))
    rms = [diagnostics["models"][key]["reference_distance_rms_m"] for key in keys]
    p95 = [diagnostics["models"][key]["reference_distance_p95_m"] for key in keys]
    segments = [diagnostics["models"][key]["segment_count"] for key in keys]
    axes[2].plot(x, rms, marker="o", color="#6a51a3", label="RMS distance")
    axes[2].plot(x, p95, marker="o", color="#de2d26", label="95th percentile")
    axes[2].set_xticks(x)
    axes[2].set_xticklabels(keys, rotation=30, ha="right")
    axes[2].set_ylabel("distance to 8 m input (m)")
    axes[2].grid(True, alpha=0.25)
    twin = axes[2].twinx()
    twin.plot(x, segments, marker="s", color="#1b9e77", label="segments")
    twin.set_ylabel("segment count")
    axes[2].set_title("model complexity vs fit")
    handles_a, labels_a = axes[2].get_legend_handles_labels()
    handles_b, labels_b = twin.get_legend_handles_labels()
    axes[2].legend(handles_a + handles_b, labels_a + labels_b, frameon=False, loc="upper left")

    selected = diagnostics["models"][selected_key]
    type_counts = selected["type_counts"]
    labels = list(type_counts)
    values = [type_counts[label] for label in labels]
    colors = {
        "line": "#4c78a8",
        "circle_arc": "#f58518",
        "clothoid_like": "#54a24b",
        "flex_arc": "#b279a2",
    }
    axes[3].bar(labels, values, color=[colors.get(label, "#888888") for label in labels])
    axes[3].set_ylabel("segment count")
    axes[3].set_title(f"selected primitive mix: {selected_key}")
    axes[3].grid(True, axis="y", alpha=0.25)

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
    parser.add_argument("--input-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-fused-smoothed-candidates.geojson")
    parser.add_argument("--input-cutoff-m", type=float, default=8.0)
    parser.add_argument("--curvature-diagnostics", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "curvature-radius-diagnostics.json")
    parser.add_argument("--output-curb-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-piecewise-curvature-candidates.geojson")
    parser.add_argument("--output-running-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "running-line-2ft-piecewise-curvature-candidates.geojson")
    parser.add_argument("--output-segments-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-piecewise-curvature-segments.geojson")
    parser.add_argument("--overlay", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "piecewise-curvature-models.png")
    parser.add_argument("--diagnostics", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "piecewise-curvature-diagnostics.json")
    parser.add_argument("--sample-spacing-m", type=float, default=0.45)
    parser.add_argument("--min-segment-m", type=float, default=10.0)
    parser.add_argument("--max-segment-m", type=float, default=75.0)
    parser.add_argument("--segment-penalties", nargs="+", type=float, default=[0.18, 0.28, 0.42, 0.62, 0.90, 1.30])
    parser.add_argument("--curvature-cap-weight", type=float, default=100.0)
    parser.add_argument("--robust-residual-m", type=float, default=0.45)
    parser.add_argument("--target-rms-m", type=float, default=0.28)
    parser.add_argument("--physical-radius-m", type=float, default=None)
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    offset_m = float(config["measurement"]["intended_offset"]["meters"])
    input_line_4326, input_properties = load_smoothed_line(args.input_geojson, args.input_cutoff_m)
    input_line = transform_geometry(input_line_4326, "EPSG:4326", measurement_crs)

    physical_radius_m = args.physical_radius_m
    if physical_radius_m is None:
        with args.curvature_diagnostics.open("r", encoding="utf-8") as handle:
            curvature_diagnostics = json.load(handle)
        physical_radius_m = float(curvature_diagnostics["physical_radius_estimate_m"]["median_m"])

    coords, spacing_m, input_length_m = resample_closed_line(input_line, args.sample_spacing_m)
    if signed_area(coords) < 0:
        coords = coords[::-1]
    start_index = choose_start_index(coords)
    coords = rotate_array(coords, start_index)

    complexity_weights = {
        "line": 0.000,
        "circle_arc": 0.020,
        "clothoid_like": 0.045,
        "flex_arc": 0.110,
    }
    segment_models = precompute_segment_models(
        coords,
        spacing_m,
        args.min_segment_m,
        args.max_segment_m,
        physical_radius_m,
        args.curvature_cap_weight,
        complexity_weights,
        args.robust_residual_m,
    )

    model_features: list[dict[str, Any]] = []
    running_features: list[dict[str, Any]] = []
    segment_features: list[dict[str, Any]] = []
    model_lines: dict[str, LineString] = {}
    running_lines: dict[str, LineString] = {}
    diagnostics: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_geojson": str(args.input_geojson),
        "input_cutoff_m": float(args.input_cutoff_m),
        "input_properties": input_properties,
        "measurement_crs": str(measurement_crs),
        "sample_spacing_m": float(spacing_m),
        "input_length_m": float(input_length_m),
        "physical_radius_m": float(physical_radius_m),
        "curvature_cap_1_per_m": float(1.0 / physical_radius_m),
        "curvature_cap_weight": float(args.curvature_cap_weight),
        "robust_residual_m": float(args.robust_residual_m),
        "min_segment_m": float(args.min_segment_m),
        "max_segment_m": float(args.max_segment_m),
        "complexity_weights": complexity_weights,
        "start_index": int(start_index),
        "model_basis": {
            "line": "lateral offset y(u)=0",
            "circle_arc": "single sagitta parameter y(u)=4u(1-u)a",
            "clothoid_like": "two-parameter transition y(u)=4u(1-u)(a+b(2u-1))",
            "flex_arc": "three-parameter low-order arc y(u)=4u(1-u)(a+bq+c(q^2-1/3))",
        },
        "models": {},
    }

    for penalty in args.segment_penalties:
        segments = solve_segments(
            coords.shape[0],
            spacing_m,
            args.min_segment_m,
            args.max_segment_m,
            penalty,
            segment_models,
        )
        model_coords = build_model_coords(coords, segments, spacing_m)
        model_line = LineString(model_coords)
        polygon = polygon_from_closed_line(model_line)
        running_line: LineString | None = None
        if polygon is not None:
            running_polygon = polygon.buffer(-offset_m)
            if not running_polygon.is_empty:
                if running_polygon.geom_type == "MultiPolygon":
                    running_polygon = max(running_polygon.geoms, key=lambda item: item.area)
                running_line = LineString(running_polygon.exterior.coords)

        key = f"penalty {penalty:g}"
        summary = summarize_model(key, penalty, segments, coords, model_line, running_line)
        diagnostics["models"][key] = summary
        properties = {
            "method": "piecewise_curvature",
            "source_cutoff_m": float(args.input_cutoff_m),
            "segment_penalty": float(penalty),
            "segment_count": int(summary["segment_count"]),
            "type_counts": summary["type_counts"],
            "curb_length_m": float(summary["curb_length_m"]),
            "running_length_m": summary["running_length_m"],
            "reference_distance_rms_m": float(summary["reference_distance_rms_m"]),
            "reference_distance_p95_m": float(summary["reference_distance_p95_m"]),
            "selected": False,
        }
        model_features.append(geojson_feature(model_line, properties, measurement_crs))
        model_lines[key] = model_line
        if running_line is not None:
            running_properties = {
                **properties,
                "offset_m": offset_m,
                "length_m": float(running_line.length),
                "length_km": float(running_line.length / 1000.0),
                "length_miles": float(running_line.length / 1609.344),
            }
            running_features.append(geojson_feature(running_line, running_properties, measurement_crs))
            running_lines[key] = running_line

        for segment_index, segment in enumerate(segments):
            segment_coords = sample_segment_model(coords, segment, spacing_m)
            segment_line = LineString(segment_coords)
            if segment_line.length == 0:
                continue
            segment_features.append(
                geojson_feature(
                    segment_line,
                    {
                        "method": "piecewise_curvature_segment",
                        "parent": key,
                        "segment_index": int(segment_index),
                        "segment_penalty": float(penalty),
                        "model_type": segment.model_type,
                        "degree": int(segment.degree),
                        "length_m": float(segment_line.length),
                        "radius_min_m": segment.radius_min_m,
                        "rms_fit_m": float(segment.rms_fit_m),
                        "p95_abs_fit_m": float(segment.p95_abs_fit_m),
                    },
                    measurement_crs,
                )
            )

    selected_key = selected_model_key(diagnostics["models"], args.target_rms_m)
    diagnostics["selected_model"] = selected_key
    for feature in model_features:
        feature["properties"]["selected"] = feature["properties"]["segment_penalty"] == diagnostics["models"][selected_key]["segment_penalty"]
    for feature in running_features:
        feature["properties"]["selected"] = feature["properties"]["segment_penalty"] == diagnostics["models"][selected_key]["segment_penalty"]

    write_geojson(args.output_curb_geojson, model_features)
    write_geojson(args.output_running_geojson, running_features)
    write_geojson(args.output_segments_geojson, segment_features)
    render_overlay(
        args.overlay,
        args.ortho,
        input_line_4326,
        model_lines,
        running_lines,
        selected_key,
        diagnostics,
        measurement_crs,
    )
    write_json(args.diagnostics, diagnostics)

    selected = diagnostics["models"][selected_key]
    print(f"wrote {args.output_curb_geojson}")
    print(f"wrote {args.output_running_geojson}")
    print(f"wrote {args.output_segments_geojson}")
    print(f"wrote {args.overlay}")
    print(f"wrote {args.diagnostics}")
    print(f"selected model: {selected_key}")
    print(f"segments: {selected['segment_count']}, RMS: {selected['reference_distance_rms_m']:.3f} m")
    if selected["running_length_m"] is not None:
        print(f"running length: {selected['running_length_m']:.3f} m")


if __name__ == "__main__":
    main()
