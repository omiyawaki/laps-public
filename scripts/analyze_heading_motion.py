#!/usr/bin/env python3
"""Analyze heading and course-correction signatures along curb candidates."""

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
from pyproj import CRS
from scipy import ndimage
from shapely.geometry import LineString, Point, shape

from build_fused_segmentation import load_ortho, transform_geometry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def load_line_feature(
    path: Path,
    selected_ok: bool = True,
    selector: dict[str, Any] | None = None,
) -> tuple[LineString, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    fallback: tuple[LineString, dict[str, Any]] | None = None
    for feature in collection["features"]:
        props = dict(feature.get("properties", {}))
        if selector and any(props.get(key) != value for key, value in selector.items()):
            continue
        geometry = shape(feature["geometry"])
        if not isinstance(geometry, LineString):
            continue
        if selected_ok and props.get("selected") is True:
            return geometry, props
        if fallback is None:
            fallback = (geometry, props)
    if fallback is None:
        raise ValueError(f"no matching LineString feature in {path}")
    return fallback


def sample_closed_line(line: LineString, spacing_m: float) -> tuple[np.ndarray, np.ndarray, float]:
    length = float(line.length)
    n_samples = max(128, int(round(length / spacing_m)))
    stations = np.linspace(0.0, length, n_samples, endpoint=False)
    coords = np.array([[line.interpolate(float(station)).x, line.interpolate(float(station)).y] for station in stations], dtype=float)
    return coords, stations, length / n_samples


def signed_area(coords: np.ndarray) -> float:
    x = coords[:, 0]
    y = coords[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - y * np.roll(x, -1)))


def gaussian_periodic(values: np.ndarray, sigma_samples: float) -> np.ndarray:
    if sigma_samples <= 0:
        return values.copy()
    return ndimage.gaussian_filter1d(values, sigma=sigma_samples, mode="wrap")


def circular_smooth_angles(angles: np.ndarray, sigma_samples: float) -> np.ndarray:
    sin_smooth = gaussian_periodic(np.sin(angles), sigma_samples)
    cos_smooth = gaussian_periodic(np.cos(angles), sigma_samples)
    return np.arctan2(sin_smooth, cos_smooth)


def angle_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.arctan2(np.sin(a - b), np.cos(a - b))


def sample_at_station(stations: np.ndarray, values: np.ndarray, station_m: float) -> float:
    spacing = float(stations[1] - stations[0])
    lap_length = float(stations[-1] + spacing)
    station = station_m % lap_length
    return float(np.interp(station, np.r_[stations, lap_length], np.r_[values, values[0]]))


def local_window_summary(stations: np.ndarray, values: np.ndarray, station_m: float, half_window_m: float) -> dict[str, float]:
    spacing = float(stations[1] - stations[0])
    lap_length = float(stations[-1] + spacing)
    delta = np.abs(stations - (station_m % lap_length))
    delta = np.minimum(delta, lap_length - delta)
    mask = delta <= half_window_m
    if not np.any(mask):
        return {"max": float("nan"), "min": float("nan"), "rms": float("nan")}
    window = values[mask]
    return {
        "max": float(np.max(window)),
        "min": float(np.min(window)),
        "rms": float(np.sqrt(np.mean(window * window))),
    }


def robust_zscore(values: np.ndarray) -> np.ndarray:
    center = float(np.median(values))
    mad = float(np.median(np.abs(values - center)))
    scale = 1.4826 * mad if mad > 1e-9 else float(np.std(values))
    if scale <= 1e-9:
        scale = 1.0
    return (values - center) / scale


def local_extrema(
    stations: np.ndarray,
    values: np.ndarray,
    min_separation_m: float,
    limit: int,
) -> list[dict[str, float]]:
    candidates = list(np.argsort(values)[::-1])
    spacing = float(stations[1] - stations[0])
    lap_length = float(stations[-1] + spacing)
    selected: list[int] = []
    for candidate in candidates:
        station = float(stations[candidate])
        if all(min(abs(station - float(stations[kept])), lap_length - abs(station - float(stations[kept]))) >= min_separation_m for kept in selected):
            selected.append(int(candidate))
        if len(selected) >= limit:
            break
    return [{"station_m": float(stations[index]), "value": float(values[index])} for index in selected]


def trajectory_metrics(
    line_4326: LineString,
    measurement_crs: CRS,
    sample_spacing_m: float,
    coordinate_smooth_m: float,
    trend_wavelength_m: float,
    energy_window_m: float,
) -> dict[str, Any]:
    line_m = transform_geometry(line_4326, "EPSG:4326", measurement_crs)
    coords, stations, actual_spacing = sample_closed_line(line_m, sample_spacing_m)

    coordinate_sigma = max(coordinate_smooth_m / actual_spacing, 0.0)
    x = gaussian_periodic(coords[:, 0], coordinate_sigma)
    y = gaussian_periodic(coords[:, 1], coordinate_sigma)
    smooth_coords = np.column_stack([x, y])

    dx = np.gradient(x, actual_spacing, edge_order=2)
    dy = np.gradient(y, actual_spacing, edge_order=2)
    ddx = np.gradient(dx, actual_spacing, edge_order=2)
    ddy = np.gradient(dy, actual_spacing, edge_order=2)
    heading = np.arctan2(dy, dx)
    speed = np.maximum(np.hypot(dx, dy), 1e-9)
    curvature = (dx * ddy - dy * ddx) / np.maximum(speed**3, 1e-9)

    trend_sigma = max(trend_wavelength_m / actual_spacing / 4.0, 1.0)
    heading_trend = circular_smooth_angles(heading, trend_sigma)
    heading_residual = angle_diff(heading, heading_trend)
    curvature_trend = gaussian_periodic(curvature, trend_sigma)
    curvature_residual = curvature - curvature_trend

    energy_sigma = max(energy_window_m / actual_spacing / 4.0, 1.0)
    heading_energy = np.sqrt(gaussian_periodic(heading_residual * heading_residual, energy_sigma))
    curvature_energy = np.sqrt(gaussian_periodic(curvature_residual * curvature_residual, energy_sigma))

    return {
        "line_measure": line_m,
        "coords": smooth_coords,
        "stations": stations,
        "sample_spacing_m": actual_spacing,
        "heading_rad": heading,
        "heading_trend_rad": heading_trend,
        "heading_residual_rad": heading_residual,
        "curvature_1_per_m": curvature,
        "curvature_trend_1_per_m": curvature_trend,
        "curvature_residual_1_per_m": curvature_residual,
        "heading_energy_rad": heading_energy,
        "curvature_energy_1_per_m": curvature_energy,
        "course_correction_score": robust_zscore(curvature_energy) + robust_zscore(np.abs(heading_residual)),
    }


def point_at_station(line: LineString, station_m: float) -> Point:
    return line.interpolate(float(station_m) % float(line.length))


def plot_line(ax: Any, line: LineString, src_crs: Any, dst_crs: Any, **kwargs: Any) -> None:
    native = transform_geometry(line, src_crs, dst_crs)
    xs, ys = native.xy
    ax.plot(xs, ys, **kwargs)


def render_plot(
    output_path: Path,
    rgb: np.ndarray,
    transform_affine: Any,
    ortho_crs: Any,
    current_4326: LineString,
    prior_4326: LineString,
    measurement_crs: CRS,
    current: dict[str, Any],
    prior: dict[str, Any],
    flag_stations_m: list[float],
) -> None:
    height, width = rgb.shape[:2]
    left = transform_affine.c
    top = transform_affine.f
    right = left + transform_affine.a * width
    bottom = top + transform_affine.e * height
    extent = [left, right, bottom, top]

    stations = current["stations"]
    heading_resid_deg = np.degrees(current["heading_residual_rad"])
    prior_heading_resid_deg = np.degrees(prior["heading_residual_rad"])
    curvature_resid = current["curvature_residual_1_per_m"]
    score = current["course_correction_score"]

    fig = plt.figure(figsize=(15, 14), dpi=180)
    grid = fig.add_gridspec(3, 2, height_ratios=[1.0, 0.88, 0.88])
    ax_map = fig.add_subplot(grid[0, 0])
    ax_heading = fig.add_subplot(grid[0, 1])
    ax_curvature = fig.add_subplot(grid[1, :], sharex=ax_heading)
    ax_score = fig.add_subplot(grid[2, :], sharex=ax_heading)

    ax_map.imshow(rgb, extent=extent)
    plot_line(ax_map, prior_4326, "EPSG:4326", ortho_crs, color="#ff5f99", linewidth=1.4, linestyle="--", alpha=0.75, label="piecewise prior")
    plot_line(ax_map, current_4326, "EPSG:4326", ortho_crs, color="#00d9ff", linewidth=2.2, label="data-aware curb")
    for station in flag_stations_m:
        point = point_at_station(current["line_measure"], station)
        point_ortho = transform_geometry(point, measurement_crs, ortho_crs)
        ax_map.scatter([point_ortho.x], [point_ortho.y], s=36, color="#ffea61", edgecolor="#111111", linewidth=0.8, zorder=6)
        ax_map.annotate(
            f"{station:.3f} m",
            (point_ortho.x, point_ortho.y),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=7,
            color="#111111",
            bbox={"boxstyle": "round,pad=0.18", "fc": "#ffea61", "ec": "none", "alpha": 0.92},
        )
    ax_map.set_title("flagged stations")
    ax_map.set_aspect("equal")
    ax_map.set_axis_off()
    ax_map.legend(loc="lower right", fontsize=7, frameon=True)

    ax_heading.plot(stations, heading_resid_deg, color="#1f8fbf", linewidth=1.1, label="current")
    ax_heading.plot(prior["stations"], prior_heading_resid_deg, color="#ff5f99", linewidth=0.9, linestyle="--", alpha=0.8, label="prior")
    ax_heading.axhline(0.0, color="#111111", linewidth=0.8)
    for station in flag_stations_m:
        ax_heading.axvline(station, color="#f0b400", linewidth=1.0, alpha=0.9)
        value = sample_at_station(stations, heading_resid_deg, station)
        ax_heading.scatter([station], [value], s=32, color="#ffea61", edgecolor="#111111", linewidth=0.8, zorder=5)
    ax_heading.set_title("heading residual after local trend removal")
    ax_heading.set_ylabel("degrees")
    ax_heading.grid(True, alpha=0.25)
    ax_heading.legend(loc="best", fontsize=7, frameon=False)

    ax_curvature.plot(stations, curvature_resid, color="#565656", linewidth=1.0)
    ax_curvature.fill_between(stations, 0.0, curvature_resid, where=curvature_resid >= 0, color="#00a36c", alpha=0.28, interpolate=True)
    ax_curvature.fill_between(stations, 0.0, curvature_resid, where=curvature_resid < 0, color="#d63b5c", alpha=0.25, interpolate=True)
    ax_curvature.axhline(0.0, color="#111111", linewidth=0.8)
    for station in flag_stations_m:
        ax_curvature.axvline(station, color="#f0b400", linewidth=1.0, alpha=0.9)
    ax_curvature.set_title("curvature residual: steering left/right corrections")
    ax_curvature.set_ylabel("1/m")
    ax_curvature.grid(True, alpha=0.25)

    ax_score.plot(stations, score, color="#6a51a3", linewidth=1.1)
    ax_score.axhline(0.0, color="#111111", linewidth=0.8)
    for station in flag_stations_m:
        ax_score.axvline(station, color="#f0b400", linewidth=1.0, alpha=0.9)
        value = sample_at_station(stations, score, station)
        ax_score.scatter([station], [value], s=34, color="#ffea61", edgecolor="#111111", linewidth=0.8, zorder=5)
        ax_score.annotate(
            f"{station:.3f}",
            (station, value),
            xytext=(4, 5),
            textcoords="offset points",
            fontsize=7,
            color="#111111",
            bbox={"boxstyle": "round,pad=0.16", "fc": "#ffea61", "ec": "none", "alpha": 0.9},
        )
    ax_score.set_title("course-correction score")
    ax_score.set_xlabel("station along current curb (m)")
    ax_score.set_ylabel("robust score")
    ax_score.grid(True, alpha=0.25)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)


def metric_record(metrics: dict[str, Any], station_m: float) -> dict[str, Any]:
    stations = metrics["stations"]
    heading_resid_deg = np.degrees(metrics["heading_residual_rad"])
    window_heading = local_window_summary(stations, heading_resid_deg, station_m, half_window_m=2.0)
    window_curvature = local_window_summary(stations, metrics["curvature_residual_1_per_m"], station_m, half_window_m=2.0)
    return {
        "station_m": float(station_m),
        "heading_residual_deg": float(sample_at_station(stations, heading_resid_deg, station_m)),
        "heading_window_max_deg": window_heading["max"],
        "heading_window_min_deg": window_heading["min"],
        "heading_window_peak_to_peak_deg": float(window_heading["max"] - window_heading["min"]),
        "curvature_residual_1_per_m": float(sample_at_station(stations, metrics["curvature_residual_1_per_m"], station_m)),
        "curvature_window_max_1_per_m": window_curvature["max"],
        "curvature_window_min_1_per_m": window_curvature["min"],
        "curvature_window_peak_to_peak_1_per_m": float(window_curvature["max"] - window_curvature["min"]),
        "course_correction_score": float(sample_at_station(stations, metrics["course_correction_score"], station_m)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--ortho", type=Path, default=PROJECT_ROOT / "data" / "interim" / "ortho" / "example-2024-loop-tight.tif")
    parser.add_argument("--curb-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-data-aware-candidate.geojson")
    parser.add_argument("--prior-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-piecewise-curvature-candidates.geojson")
    parser.add_argument("--output-plot", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "heading-motion-analysis.png")
    parser.add_argument("--diagnostics", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "heading-motion-diagnostics.json")
    parser.add_argument("--sample-spacing-m", type=float, default=0.1)
    parser.add_argument("--coordinate-smooth-m", type=float, default=0.45)
    parser.add_argument("--trend-wavelength-m", type=float, default=18.0)
    parser.add_argument("--energy-window-m", type=float, default=4.0)
    parser.add_argument("--flag-stations-m", nargs="+", type=float, default=[14.405, 38.263, 59.421])
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    current_4326, current_props = load_line_feature(args.curb_geojson, selected_ok=False)
    prior_4326, prior_props = load_line_feature(args.prior_geojson, selected_ok=True)

    current = trajectory_metrics(
        current_4326,
        measurement_crs,
        args.sample_spacing_m,
        args.coordinate_smooth_m,
        args.trend_wavelength_m,
        args.energy_window_m,
    )
    prior = trajectory_metrics(
        prior_4326,
        measurement_crs,
        args.sample_spacing_m,
        args.coordinate_smooth_m,
        args.trend_wavelength_m,
        args.energy_window_m,
    )

    ortho_array, rgb, transform_affine, ortho_crs = load_ortho(args.ortho, downsample=1)
    render_plot(
        args.output_plot,
        rgb,
        transform_affine,
        ortho_crs,
        current_4326,
        prior_4326,
        measurement_crs,
        current,
        prior,
        args.flag_stations_m,
    )

    flagged = [metric_record(current, station) for station in args.flag_stations_m]
    peaks = local_extrema(current["stations"], current["course_correction_score"], min_separation_m=3.0, limit=20)
    for record in flagged:
        score = record["course_correction_score"]
        record["score_rank_among_samples"] = int(np.count_nonzero(current["course_correction_score"] > score) + 1)

    diagnostics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "curb_geojson": str(args.curb_geojson),
        "curb_properties": current_props,
        "prior_geojson": str(args.prior_geojson),
        "prior_properties": prior_props,
        "sample_spacing_m": float(current["sample_spacing_m"]),
        "coordinate_smooth_m": float(args.coordinate_smooth_m),
        "trend_wavelength_m": float(args.trend_wavelength_m),
        "energy_window_m": float(args.energy_window_m),
        "flagged_stations": flagged,
        "largest_course_correction_peaks": peaks,
        "current_summary": {
            "heading_residual_rms_deg": float(np.sqrt(np.mean(np.degrees(current["heading_residual_rad"]) ** 2))),
            "curvature_residual_rms_1_per_m": float(np.sqrt(np.mean(current["curvature_residual_1_per_m"] ** 2))),
            "course_correction_score_p95": float(np.percentile(current["course_correction_score"], 95)),
            "course_correction_score_max": float(np.max(current["course_correction_score"])),
        },
    }
    write_json(args.diagnostics, diagnostics)
    print(f"wrote {args.output_plot}")
    print(f"wrote {args.diagnostics}")
    for record in flagged:
        print(
            f"{record['station_m']:.3f} m: heading {record['heading_residual_deg']:+.2f} deg, "
            f"heading p2p {record['heading_window_peak_to_peak_deg']:.2f} deg, "
            f"course score {record['course_correction_score']:+.2f}, "
            f"rank {record['score_rank_among_samples']}"
        )


if __name__ == "__main__":
    main()
