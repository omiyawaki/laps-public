#!/usr/bin/env python3
"""Derive per-lap elevation gain from the accepted running line and DEM."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import yaml
from pyproj import CRS, Transformer
from scipy import ndimage
from shapely.geometry import LineString, shape
from shapely.ops import transform as transform_geometry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_line_feature(path: Path) -> tuple[LineString, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    for feature in collection["features"]:
        geometry = shape(feature["geometry"])
        if isinstance(geometry, LineString):
            return geometry, dict(feature.get("properties", {}))
    raise ValueError(f"no LineString feature found in {path}")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def sample_closed_line(line: LineString, spacing_m: float) -> tuple[np.ndarray, list[tuple[float, float]]]:
    length_m = float(line.length)
    sample_count = int(math.ceil(length_m / spacing_m))
    stations = np.linspace(0.0, length_m, sample_count, endpoint=False)
    points = [line.interpolate(float(station)) for station in stations]
    return stations, [(point.x, point.y) for point in points]


def sample_dem_bilinear(dataset: rasterio.io.DatasetReader, xy: list[tuple[float, float]]) -> np.ndarray:
    elevation = dataset.read(1).astype(float)
    if dataset.nodata is not None:
        elevation[elevation == dataset.nodata] = np.nan
    rows_cols = [dataset.index(x, y, op=float) for x, y in xy]
    rows = np.array([row for row, _ in rows_cols], dtype=float)
    cols = np.array([col for _, col in rows_cols], dtype=float)
    sampled = ndimage.map_coordinates(elevation, [rows, cols], order=1, mode="nearest")
    if np.isnan(sampled).any():
        finite = np.isfinite(sampled)
        if not finite.any():
            raise ValueError("DEM sampling returned only nodata values")
        sampled[~finite] = np.interp(np.flatnonzero(~finite), np.flatnonzero(finite), sampled[finite])
    return sampled


def smooth_closed_profile(elevation: np.ndarray, sigma_m: float, spacing_m: float) -> np.ndarray:
    if sigma_m <= 0:
        return elevation.copy()
    return ndimage.gaussian_filter1d(elevation, sigma=sigma_m / spacing_m, mode="wrap")


def gain_loss(elevation: np.ndarray) -> tuple[float, float]:
    closed = np.concatenate([elevation, elevation[:1]])
    deltas = np.diff(closed)
    gain = float(deltas[deltas > 0].sum())
    loss = float(-deltas[deltas < 0].sum())
    return gain, loss


def profile_points(stations: np.ndarray, raw_elevation: np.ndarray, smoothed_elevation: np.ndarray, step: int) -> list[dict[str, float]]:
    points = []
    for index in range(0, len(stations), step):
        points.append(
            {
                "station_m": round(float(stations[index]), 3),
                "raw_elevation_m": round(float(raw_elevation[index]), 3),
                "smoothed_elevation_m": round(float(smoothed_elevation[index]), 3),
            }
        )
    return points


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--running-line", type=Path, default=PROJECT_ROOT / "geometry" / "running-line-1m-visual-candidate.geojson")
    parser.add_argument("--dem", type=Path, default=PROJECT_ROOT / "data" / "interim" / "lidar" / "fema-2019-dem-loop-tight.tif")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "geometry" / "running-line-1m-elevation-profile.json")
    parser.add_argument("--sample-spacing-m", type=float, default=1.0)
    parser.add_argument("--smoothing-sigma-m", type=float, default=10.0)
    parser.add_argument("--profile-step", type=int, default=5)
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    line_4326, line_properties = load_line_feature(args.running_line)
    line_measure = transform_geometry(
        Transformer.from_crs("EPSG:4326", measurement_crs, always_xy=True).transform,
        line_4326,
    )
    stations, measurement_xy = sample_closed_line(line_measure, args.sample_spacing_m)

    with rasterio.open(args.dem) as dataset:
        dem_crs = CRS.from_user_input(dataset.crs)
        to_dem = Transformer.from_crs(measurement_crs, dem_crs, always_xy=True)
        dem_xy = [to_dem.transform(x, y) for x, y in measurement_xy]
        raw_elevation = sample_dem_bilinear(dataset, dem_xy)

    smoothed_elevation = smooth_closed_profile(raw_elevation, args.smoothing_sigma_m, args.sample_spacing_m)
    raw_gain_m, raw_loss_m = gain_loss(raw_elevation)
    gain_m, loss_m = gain_loss(smoothed_elevation)
    sensitivity = []
    for sigma_m in [0.0, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0]:
        profile = smooth_closed_profile(raw_elevation, sigma_m, args.sample_spacing_m)
        profile_gain, profile_loss = gain_loss(profile)
        sensitivity.append(
            {
                "smoothing_sigma_m": sigma_m,
                "gain_m": profile_gain,
                "loss_m": profile_loss,
                "range_m": float(np.max(profile) - np.min(profile)),
            }
        )

    diagnostics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "running_line": str(args.running_line),
        "dem": str(args.dem),
        "source": {
            "collection": "FEMA 2019",
            "type": "1 m bare-earth DEM",
            "source_dem": "data/raw/lidar/fema_2019/18TXX000000.tif",
        },
        "measurement_crs": str(measurement_crs),
        "sample_spacing_m": args.sample_spacing_m,
        "sample_count": int(len(stations)),
        "lap_length_m": float(line_properties.get("length_m", line_measure.length)),
        "sampled_line_length_m": float(line_measure.length),
        "smoothing": {
            "method": "closed-loop Gaussian profile smoothing",
            "sigma_m": args.smoothing_sigma_m,
            "note": "Positive elevation gain is summed after smoothing to avoid counting 1 m raster-cell wiggles as ascent.",
        },
        "lap_elevation_gain_m": gain_m,
        "lap_elevation_loss_m": loss_m,
        "lap_elevation_gain_ft": gain_m / 0.3048,
        "lap_elevation_loss_ft": loss_m / 0.3048,
        "raw_lap_elevation_gain_m": raw_gain_m,
        "raw_lap_elevation_loss_m": raw_loss_m,
        "elevation_min_m": float(np.min(smoothed_elevation)),
        "elevation_max_m": float(np.max(smoothed_elevation)),
        "elevation_range_m": float(np.max(smoothed_elevation) - np.min(smoothed_elevation)),
        "smoothing_sensitivity": sensitivity,
        "profile": profile_points(stations, raw_elevation, smoothed_elevation, max(1, args.profile_step)),
    }
    write_json(args.output, diagnostics)

    print(f"wrote {args.output}")
    print(f"lap elevation gain: {gain_m:.3f} m / {gain_m / 0.3048:.2f} ft")
    print(f"lap elevation loss: {loss_m:.3f} m / {loss_m / 0.3048:.2f} ft")


if __name__ == "__main__":
    main()

