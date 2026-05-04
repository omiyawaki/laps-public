#!/usr/bin/env python3
"""Apply closed-curve wavelength filtering to the fused curb candidate."""

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
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import transform

from build_baseline_geometry import stretch_rgb


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


def load_method_line(path: Path, method: str) -> LineString:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    for feature in collection["features"]:
        if feature.get("properties", {}).get("method") == method:
            geometry = shape(feature["geometry"])
            if not isinstance(geometry, LineString):
                raise ValueError(f"{method} geometry is {geometry.geom_type}, expected LineString")
            return geometry
    raise ValueError(f"no feature with method={method!r} in {path}")


def closed_coords(line: LineString) -> np.ndarray:
    coords = np.asarray(line.coords, dtype=float)
    if coords.shape[0] < 4:
        raise ValueError("line has too few vertices to smooth")
    if np.linalg.norm(coords[0] - coords[-1]) > 1e-6:
        coords = np.vstack([coords, coords[0]])
    return coords


def resample_closed_line(line: LineString, spacing_m: float) -> tuple[np.ndarray, float]:
    total_length = float(line.length)
    n_samples = max(64, int(round(total_length / spacing_m)))
    distances = np.linspace(0.0, total_length, n_samples, endpoint=False)
    points = [line.interpolate(float(distance)) for distance in distances]
    coords = np.array([[point.x, point.y] for point in points], dtype=float)
    return coords, total_length / n_samples


def fourier_lowpass_closed_curve(
    line: LineString,
    cutoff_wavelength_m: float,
    spacing_m: float,
    order: int,
) -> tuple[LineString, dict[str, float]]:
    coords, actual_spacing_m = resample_closed_line(line, spacing_m)
    n = coords.shape[0]
    z = coords[:, 0] + 1j * coords[:, 1]
    frequencies = np.fft.fftfreq(n, d=actual_spacing_m)
    frequency_abs = np.abs(frequencies)
    cutoff_frequency = 1.0 / cutoff_wavelength_m
    with np.errstate(divide="ignore", invalid="ignore"):
        weights = 1.0 / np.sqrt(1.0 + (frequency_abs / cutoff_frequency) ** (2 * order))
    weights[0] = 1.0

    coefficients = np.fft.fft(z)
    smoothed = np.fft.ifft(coefficients * weights)
    smooth_coords = np.column_stack([smoothed.real, smoothed.imag])
    smooth_line = LineString(np.vstack([smooth_coords, smooth_coords[0]]))

    displacement = np.abs(smoothed - z)
    removed = np.fft.ifft(coefficients * (1.0 - weights))
    non_dc = np.arange(n) != 0
    retained_power = float(np.sum(np.abs(coefficients[non_dc] * weights[non_dc]) ** 2))
    removed_power = float(np.sum(np.abs(coefficients[non_dc] * (1.0 - weights[non_dc])) ** 2))
    diagnostics = {
        "cutoff_wavelength_m": float(cutoff_wavelength_m),
        "filter_order": int(order),
        "samples": int(n),
        "sample_spacing_m": float(actual_spacing_m),
        "raw_length_m": float(line.length),
        "smoothed_length_m": float(smooth_line.length),
        "length_change_m": float(smooth_line.length - line.length),
        "mean_displacement_m": float(np.mean(displacement)),
        "rms_displacement_m": float(np.sqrt(np.mean(displacement**2))),
        "max_displacement_m": float(np.max(displacement)),
        "removed_rms_m": float(np.sqrt(np.mean(np.abs(removed) ** 2))),
        "removed_power_fraction": float(removed_power / (retained_power + removed_power)),
    }
    return smooth_line, diagnostics


def polygon_from_closed_line(line: LineString) -> Polygon:
    coords = closed_coords(line)
    polygon = Polygon(coords)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        raise ValueError("smoothed line did not form a valid polygon")
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


def load_ortho_preview(path: Path) -> tuple[np.ndarray, Any, CRS, tuple[float, float, float, float]]:
    with rasterio.open(path) as dataset:
        array = dataset.read()
        rgb = stretch_rgb(array)
        crs = CRS.from_user_input(dataset.crs)
        bounds = (dataset.bounds.left, dataset.bounds.bottom, dataset.bounds.right, dataset.bounds.top)
        return rgb, dataset.transform, crs, bounds


def plot_line(ax: Any, line: LineString, src_crs: Any, dst_crs: Any, **kwargs: Any) -> None:
    native = transform_geometry(line, src_crs, dst_crs)
    xs, ys = native.xy
    ax.plot(xs, ys, **kwargs)


def render_comparison(
    ortho_path: Path,
    raw_line_4326: LineString,
    smoothed_lines: dict[float, LineString],
    running_lines: dict[float, LineString],
    diagnostics: dict[str, Any],
    selected_cutoff_m: float,
    output_path: Path,
) -> None:
    rgb, _transform_affine, ortho_crs, bounds = load_ortho_preview(ortho_path)
    extent = [bounds[0], bounds[2], bounds[1], bounds[3]]
    cutoffs = sorted(smoothed_lines)
    colors = plt.cm.viridis(np.linspace(0.12, 0.92, len(cutoffs)))

    fig, axes = plt.subplots(2, 2, figsize=(14, 14), dpi=180)
    axes = axes.ravel()

    axes[0].imshow(rgb, extent=extent)
    plot_line(axes[0], raw_line_4326, "EPSG:4326", ortho_crs, color="#ff4f7b", linewidth=1.0, alpha=0.7, label="raw fused")
    for cutoff, color in zip(cutoffs, colors):
        plot_line(
            axes[0],
            smoothed_lines[cutoff],
            "EPSG:4326",
            ortho_crs,
            color=color,
            linewidth=1.2 if cutoff != selected_cutoff_m else 2.3,
            label=f"{cutoff:g} m",
        )
    axes[0].set_title("wavelength cutoff comparison")
    axes[0].legend(loc="lower right", frameon=True, fontsize=8)

    axes[1].imshow(rgb, extent=extent)
    plot_line(axes[1], raw_line_4326, "EPSG:4326", ortho_crs, color="#ff4f7b", linewidth=1.0, alpha=0.45, label="raw fused curb")
    plot_line(axes[1], smoothed_lines[selected_cutoff_m], "EPSG:4326", ortho_crs, color="#00e0ff", linewidth=2.2, label=f"{selected_cutoff_m:g} m filtered curb")
    if selected_cutoff_m in running_lines:
        plot_line(axes[1], running_lines[selected_cutoff_m], "EPSG:4326", ortho_crs, color="#ffd84d", linewidth=1.8, label="2 ft running line")
    axes[1].set_title("selected filtered candidate")
    axes[1].legend(loc="lower right", frameon=True, fontsize=8)

    length_ax = axes[2]
    running_lengths = [diagnostics["cutoffs"][str(cutoff)].get("running_length_m") for cutoff in cutoffs]
    curb_lengths = [diagnostics["cutoffs"][str(cutoff)]["smoothed_length_m"] for cutoff in cutoffs]
    length_ax.plot(cutoffs, curb_lengths, marker="o", color="#00a7c8", label="curb contour")
    length_ax.plot(cutoffs, running_lengths, marker="o", color="#d19b00", label="2 ft running line")
    length_ax.axvline(selected_cutoff_m, color="#222222", linewidth=1.0, linestyle="--")
    length_ax.set_xlabel("minimum retained wavelength (m)")
    length_ax.set_ylabel("length (m)")
    length_ax.set_title("length response")
    length_ax.grid(True, alpha=0.25)
    length_ax.legend(frameon=False)

    displacement_ax = axes[3]
    rms = [diagnostics["cutoffs"][str(cutoff)]["rms_displacement_m"] for cutoff in cutoffs]
    max_displacement = [diagnostics["cutoffs"][str(cutoff)]["max_displacement_m"] for cutoff in cutoffs]
    displacement_ax.plot(cutoffs, rms, marker="o", color="#6a51a3", label="RMS displacement")
    displacement_ax.plot(cutoffs, max_displacement, marker="o", color="#de2d26", label="max displacement")
    displacement_ax.axvline(selected_cutoff_m, color="#222222", linewidth=1.0, linestyle="--")
    displacement_ax.set_xlabel("minimum retained wavelength (m)")
    displacement_ax.set_ylabel("distance from raw contour (m)")
    displacement_ax.set_title("filter movement")
    displacement_ax.grid(True, alpha=0.25)
    displacement_ax.legend(frameon=False)

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
    parser.add_argument("--input-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-fused-segmentation-candidates.geojson")
    parser.add_argument("--method", default="fused")
    parser.add_argument("--output-curb-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-fused-smoothed-candidates.geojson")
    parser.add_argument("--output-running-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "running-line-2ft-fused-smoothed-candidates.geojson")
    parser.add_argument("--overlay", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "fourier-smoothing-comparison.png")
    parser.add_argument("--diagnostics", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "fourier-smoothing-diagnostics.json")
    parser.add_argument("--cutoffs-m", nargs="+", type=float, default=[2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 12.0])
    parser.add_argument("--selected-cutoff-m", type=float, default=8.0)
    parser.add_argument("--sample-spacing-m", type=float, default=0.20)
    parser.add_argument("--filter-order", type=int, default=8)
    args = parser.parse_args()

    if args.selected_cutoff_m not in args.cutoffs_m:
        args.cutoffs_m.append(args.selected_cutoff_m)
    args.cutoffs_m = sorted(set(args.cutoffs_m))

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    offset_m = float(config["measurement"]["intended_offset"]["meters"])
    raw_line_4326 = load_method_line(args.input_geojson, args.method)
    raw_line = transform_geometry(raw_line_4326, "EPSG:4326", measurement_crs)

    smoothed_features: list[dict[str, Any]] = []
    running_features: list[dict[str, Any]] = []
    smoothed_lines_4326: dict[float, LineString] = {}
    running_lines_4326: dict[float, LineString] = {}
    diagnostics: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_geojson": str(args.input_geojson),
        "method": args.method,
        "measurement_crs": str(measurement_crs),
        "sample_spacing_m": float(args.sample_spacing_m),
        "filter_order": int(args.filter_order),
        "selected_cutoff_m": float(args.selected_cutoff_m),
        "offset_m": offset_m,
        "raw_length_m": float(raw_line.length),
        "cutoffs": {},
    }

    for cutoff in args.cutoffs_m:
        smoothed_line, cutoff_diagnostics = fourier_lowpass_closed_curve(
            raw_line,
            cutoff,
            args.sample_spacing_m,
            args.filter_order,
        )
        polygon = polygon_from_closed_line(smoothed_line)
        running_polygon = polygon.buffer(-offset_m)
        running_line: LineString | None = None
        running_length_m: float | None = None
        if not running_polygon.is_empty:
            if running_polygon.geom_type == "MultiPolygon":
                running_polygon = max(running_polygon.geoms, key=lambda item: item.area)
            running_line = LineString(running_polygon.exterior.coords)
            running_length_m = float(running_line.length)

        properties = {
            "method": args.method,
            "smoothing": "closed_curve_fourier_butterworth_lowpass",
            "cutoff_wavelength_m": float(cutoff),
            "filter_order": int(args.filter_order),
            "sample_spacing_m": float(cutoff_diagnostics["sample_spacing_m"]),
            "selected": bool(cutoff == args.selected_cutoff_m),
            "curb_length_m": float(smoothed_line.length),
            "raw_length_m": float(raw_line.length),
            "length_change_m": float(cutoff_diagnostics["length_change_m"]),
            "rms_displacement_m": float(cutoff_diagnostics["rms_displacement_m"]),
            "max_displacement_m": float(cutoff_diagnostics["max_displacement_m"]),
        }
        smoothed_features.append(geojson_feature(smoothed_line, properties, measurement_crs))
        smoothed_lines_4326[cutoff] = transform_geometry(smoothed_line, measurement_crs, "EPSG:4326")

        cutoff_diagnostics["running_length_m"] = running_length_m
        if running_line is not None and running_length_m is not None:
            running_properties = {
                **properties,
                "offset_m": offset_m,
                "length_m": running_length_m,
                "length_km": running_length_m / 1000.0,
                "length_miles": running_length_m / 1609.344,
            }
            running_features.append(geojson_feature(running_line, running_properties, measurement_crs))
            running_lines_4326[cutoff] = transform_geometry(running_line, measurement_crs, "EPSG:4326")

        diagnostics["cutoffs"][str(cutoff)] = cutoff_diagnostics

    write_geojson(args.output_curb_geojson, smoothed_features)
    write_geojson(args.output_running_geojson, running_features)
    render_comparison(
        args.ortho,
        raw_line_4326,
        smoothed_lines_4326,
        running_lines_4326,
        diagnostics,
        args.selected_cutoff_m,
        args.overlay,
    )
    write_json(args.diagnostics, diagnostics)

    selected = diagnostics["cutoffs"][str(args.selected_cutoff_m)]
    print(f"wrote {args.output_curb_geojson}")
    print(f"wrote {args.output_running_geojson}")
    print(f"wrote {args.overlay}")
    print(f"wrote {args.diagnostics}")
    print(f"selected cutoff: {args.selected_cutoff_m:g} m")
    print(f"selected curb length: {selected['smoothed_length_m']:.3f} m")
    print(f"selected running length: {selected['running_length_m']:.3f} m")


if __name__ == "__main__":
    main()
