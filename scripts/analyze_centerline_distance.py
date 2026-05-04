#!/usr/bin/env python3
"""Analyze centerline-to-curb distance along the current closed curb path."""

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
from pyproj import CRS
from scipy import ndimage
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import transform, unary_union

from build_baseline_geometry import stretch_rgb
from build_fused_segmentation import load_centerlines, load_ortho, transform_geometry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


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


def sample_line(line: LineString, spacing_m: float) -> tuple[np.ndarray, np.ndarray]:
    length = float(line.length)
    n_samples = max(64, int(round(length / spacing_m)))
    stations = np.linspace(0.0, length, n_samples, endpoint=False)
    coords = np.array([[line.interpolate(float(station)).x, line.interpolate(float(station)).y] for station in stations], dtype=float)
    return coords, stations


def sample_at_station(stations: np.ndarray, values: np.ndarray, station_m: float) -> float:
    station = station_m % float(stations[-1] + (stations[1] - stations[0]))
    return float(np.interp(station, np.r_[stations, stations[0] + stations[-1] + (stations[1] - stations[0])], np.r_[values, values[0]]))


def nearest_centerline_distances(
    coords: np.ndarray,
    centerlines: list[LineString],
) -> tuple[np.ndarray, np.ndarray]:
    distances = np.zeros(coords.shape[0], dtype=float)
    nearest_index = np.zeros(coords.shape[0], dtype=int)
    for index, (x, y) in enumerate(coords):
        point = Point(float(x), float(y))
        per_line = np.array([line.distance(point) for line in centerlines], dtype=float)
        nearest_index[index] = int(np.argmin(per_line))
        distances[index] = float(per_line[nearest_index[index]])
    return distances, nearest_index


def robust_zscore(values: np.ndarray) -> np.ndarray:
    center = float(np.median(values))
    mad = float(np.median(np.abs(values - center)))
    scale = 1.4826 * mad if mad > 1e-9 else float(np.std(values))
    if scale <= 1e-9:
        scale = 1.0
    return (values - center) / scale


def local_extrema(
    stations: np.ndarray,
    residual: np.ndarray,
    min_abs_residual_m: float,
    min_separation_m: float,
    limit: int,
) -> list[dict[str, Any]]:
    abs_residual = np.abs(residual)
    candidates: list[int] = []
    n = residual.size
    for index in range(n):
        prev_index = (index - 1) % n
        next_index = (index + 1) % n
        if abs_residual[index] >= abs_residual[prev_index] and abs_residual[index] >= abs_residual[next_index]:
            if abs_residual[index] >= min_abs_residual_m:
                candidates.append(index)
    candidates.sort(key=lambda item: abs_residual[item], reverse=True)
    selected: list[int] = []
    lap_length = float(stations[-1] + (stations[1] - stations[0]))
    for candidate in candidates:
        station = float(stations[candidate])
        if all(min(abs(station - float(stations[kept])), lap_length - abs(station - float(stations[kept]))) >= min_separation_m for kept in selected):
            selected.append(candidate)
        if len(selected) >= limit:
            break
    return [
        {
            "station_m": float(stations[index]),
            "residual_m": float(residual[index]),
            "abs_residual_m": float(abs_residual[index]),
        }
        for index in selected
    ]


def plot_line(ax: Any, line: LineString, src_crs: Any, dst_crs: Any, **kwargs: Any) -> None:
    native = transform_geometry(line, src_crs, dst_crs)
    xs, ys = native.xy
    ax.plot(xs, ys, **kwargs)


def render_plot(
    output_path: Path,
    rgb: np.ndarray,
    raster_transform: Any,
    raster_crs: Any,
    curb_4326: LineString,
    centerlines_4326: list[LineString],
    stations: np.ndarray,
    distances: np.ndarray,
    trend: np.ndarray,
    residual: np.ndarray,
    nearest_index: np.ndarray,
    user_stations: list[float],
    station_points_4326: list[Point],
    centerline_labels: list[str],
) -> None:
    height, width = rgb.shape[:2]
    left = raster_transform.c
    top = raster_transform.f
    right = left + raster_transform.a * width
    bottom = top + raster_transform.e * height
    extent = [left, right, bottom, top]

    fig = plt.figure(figsize=(15, 12), dpi=180)
    grid = fig.add_gridspec(2, 2, height_ratios=[1.08, 1.0])
    ax_map = fig.add_subplot(grid[0, 0])
    ax_distance = fig.add_subplot(grid[0, 1])
    ax_residual = fig.add_subplot(grid[1, :], sharex=ax_distance)

    ax_map.imshow(rgb, extent=extent)
    plot_line(ax_map, curb_4326, "EPSG:4326", raster_crs, color="#00d9ff", linewidth=2.0, label="data-aware curb")
    for line, label in zip(centerlines_4326, centerline_labels, strict=True):
        plot_line(ax_map, line, "EPSG:4326", raster_crs, linewidth=1.2, linestyle="--", label=label)
    for station, point in zip(user_stations, station_points_4326, strict=True):
        point_native = transform_geometry(point, "EPSG:4326", raster_crs)
        ax_map.scatter([point_native.x], [point_native.y], s=36, color="#ffea61", edgecolor="#111111", linewidth=0.8, zorder=6)
        ax_map.annotate(
            f"{station:.3f} m",
            (point_native.x, point_native.y),
            xytext=(5, 5),
            textcoords="offset points",
            fontsize=7,
            color="#111111",
            bbox={"boxstyle": "round,pad=0.18", "fc": "#ffea61", "ec": "none", "alpha": 0.92},
        )
    ax_map.set_title("flagged stations on current curb")
    ax_map.set_aspect("equal")
    ax_map.set_axis_off()
    ax_map.legend(loc="lower right", fontsize=7, frameon=True)

    colors = np.array(["#2f80ed", "#7f52ff"])
    for line_index, label in enumerate(centerline_labels):
        mask = nearest_index == line_index
        ax_distance.scatter(stations[mask], distances[mask], s=4, color=colors[line_index % len(colors)], alpha=0.45, label=f"nearest {label}")
    ax_distance.plot(stations, trend, color="#111111", linewidth=1.6, label="18 m local trend")
    for station in user_stations:
        ax_distance.axvline(station, color="#f0b400", linewidth=1.0, alpha=0.9)
    ax_distance.set_title("nearest centerline-to-curb distance")
    ax_distance.set_ylabel("distance (m)")
    ax_distance.grid(True, alpha=0.25)
    ax_distance.legend(loc="best", fontsize=7, frameon=False)

    ax_residual.plot(stations, residual, color="#565656", linewidth=1.0)
    ax_residual.fill_between(stations, 0.0, residual, where=residual >= 0, color="#00a36c", alpha=0.28, interpolate=True)
    ax_residual.fill_between(stations, 0.0, residual, where=residual < 0, color="#d63b5c", alpha=0.25, interpolate=True)
    ax_residual.axhline(0.0, color="#111111", linewidth=0.9)
    for station in user_stations:
        ax_residual.axvline(station, color="#f0b400", linewidth=1.0, alpha=0.9)
        value = sample_at_station(stations, residual, station)
        ax_residual.scatter([station], [value], s=34, color="#ffea61", edgecolor="#111111", linewidth=0.8, zorder=5)
        ax_residual.annotate(
            f"{station:.3f}",
            (station, value),
            xytext=(4, 5),
            textcoords="offset points",
            fontsize=7,
            color="#111111",
            bbox={"boxstyle": "round,pad=0.16", "fc": "#ffea61", "ec": "none", "alpha": 0.9},
        )
    ax_residual.set_title("short-scale residual after removing local trend")
    ax_residual.set_xlabel("station along current curb (m)")
    ax_residual.set_ylabel("distance residual (m)")
    ax_residual.grid(True, alpha=0.25)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--ortho", type=Path, default=PROJECT_ROOT / "data" / "interim" / "ortho" / "example-2024-loop-tight.tif")
    parser.add_argument("--centerline-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "loop-centerlines.geojson")
    parser.add_argument("--curb-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-data-aware-candidate.geojson")
    parser.add_argument("--output-plot", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "centerline-distance-analysis.png")
    parser.add_argument("--diagnostics", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "centerline-distance-diagnostics.json")
    parser.add_argument("--sample-spacing-m", type=float, default=0.1)
    parser.add_argument("--trend-wavelength-m", type=float, default=18.0)
    parser.add_argument("--flag-stations-m", nargs="+", type=float, default=[14.405, 38.263, 59.421])
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    centerlines_4326 = load_centerlines(args.centerline_geojson, config)
    centerline_labels = ["Loop N", "Loop S"]
    centerlines_measure = [transform_geometry(line, "EPSG:4326", measurement_crs) for line in centerlines_4326]
    curb_4326, curb_props = load_line_feature(args.curb_geojson, selected_ok=False)
    curb_measure = transform_geometry(curb_4326, "EPSG:4326", measurement_crs)

    coords, stations = sample_line(curb_measure, args.sample_spacing_m)
    distances, nearest_index = nearest_centerline_distances(coords, centerlines_measure)
    sigma_samples = max(args.trend_wavelength_m / args.sample_spacing_m / 4.0, 1.0)
    trend = ndimage.gaussian_filter1d(distances, sigma=sigma_samples, mode="wrap")
    residual = distances - trend
    zscore = robust_zscore(residual)
    peaks = local_extrema(
        stations,
        residual,
        min_abs_residual_m=0.08,
        min_separation_m=3.0,
        limit=20,
    )

    measurement_to_4326 = CRS.from_user_input(measurement_crs)
    station_points_measure = [curb_measure.interpolate(float(station) % curb_measure.length) for station in args.flag_stations_m]
    station_points_4326 = [transform_geometry(point, measurement_to_4326, "EPSG:4326") for point in station_points_measure]

    ortho_array, rgb, transform_affine, ortho_crs = load_ortho(args.ortho, downsample=1)
    render_plot(
        args.output_plot,
        rgb,
        transform_affine,
        ortho_crs,
        curb_4326,
        centerlines_4326,
        stations,
        distances,
        trend,
        residual,
        nearest_index,
        args.flag_stations_m,
        station_points_4326,
        centerline_labels,
    )

    flag_records = []
    for station, point in zip(args.flag_stations_m, station_points_4326, strict=True):
        station_mod = float(station) % float(curb_measure.length)
        nearest_sample = int(np.argmin(np.abs(stations - station_mod)))
        abs_residual_rank = int(np.count_nonzero(np.abs(residual) > abs(residual[nearest_sample])) + 1)
        flag_records.append(
            {
                "station_m": float(station),
                "distance_m": float(sample_at_station(stations, distances, station)),
                "trend_m": float(sample_at_station(stations, trend, station)),
                "residual_m": float(sample_at_station(stations, residual, station)),
                "residual_z": float(sample_at_station(stations, zscore, station)),
                "abs_residual_rank_among_samples": abs_residual_rank,
                "nearest_centerline": centerline_labels[int(nearest_index[nearest_sample])],
                "longitude": float(point.x),
                "latitude": float(point.y),
            }
        )

    diagnostics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "curb_geojson": str(args.curb_geojson),
        "curb_properties": curb_props,
        "centerline_geojson": str(args.centerline_geojson),
        "sample_spacing_m": float(args.sample_spacing_m),
        "trend_wavelength_m": float(args.trend_wavelength_m),
        "lap_length_m": float(curb_measure.length),
        "distance_summary_m": {
            "mean": float(np.mean(distances)),
            "median": float(np.median(distances)),
            "p05": float(np.percentile(distances, 5)),
            "p95": float(np.percentile(distances, 95)),
            "min": float(np.min(distances)),
            "max": float(np.max(distances)),
        },
        "residual_summary_m": {
            "rms": float(np.sqrt(np.mean(residual * residual))),
            "mad_scaled": float(1.4826 * np.median(np.abs(residual - np.median(residual)))),
            "p05": float(np.percentile(residual, 5)),
            "p95": float(np.percentile(residual, 95)),
            "min": float(np.min(residual)),
            "max": float(np.max(residual)),
        },
        "flagged_stations": flag_records,
        "largest_abs_residual_peaks": peaks,
    }
    write_json(args.diagnostics, diagnostics)
    print(f"wrote {args.output_plot}")
    print(f"wrote {args.diagnostics}")
    for record in flag_records:
        print(
            f"{record['station_m']:.3f} m: distance {record['distance_m']:.3f} m, "
            f"residual {record['residual_m']:+.3f} m, z {record['residual_z']:+.2f}, "
            f"rank {record['abs_residual_rank_among_samples']}"
        )


if __name__ == "__main__":
    main()
