#!/usr/bin/env python3
"""Analyze physical curve radii and curvature spectrum for curb candidates."""

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
from scipy.ndimage import gaussian_filter1d
from shapely.geometry import LineString, mapping, shape
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


def load_ortho_preview(path: Path) -> tuple[np.ndarray, CRS, tuple[float, float, float, float]]:
    with rasterio.open(path) as dataset:
        rgb = stretch_rgb(dataset.read())
        crs = CRS.from_user_input(dataset.crs)
        bounds = (dataset.bounds.left, dataset.bounds.bottom, dataset.bounds.right, dataset.bounds.top)
    return rgb, crs, bounds


def load_raw_line(path: Path, method: str) -> LineString:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    for feature in collection["features"]:
        if feature.get("properties", {}).get("method") == method:
            geometry = shape(feature["geometry"])
            if not isinstance(geometry, LineString):
                raise ValueError(f"raw {method} geometry is {geometry.geom_type}, expected LineString")
            return geometry
    raise ValueError(f"no raw feature with method={method!r} in {path}")


def load_smoothed_lines(path: Path) -> dict[float, LineString]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    lines: dict[float, LineString] = {}
    for feature in collection["features"]:
        properties = feature.get("properties", {})
        if "cutoff_wavelength_m" not in properties:
            continue
        geometry = shape(feature["geometry"])
        if not isinstance(geometry, LineString):
            continue
        lines[float(properties["cutoff_wavelength_m"])] = geometry
    if not lines:
        raise ValueError(f"no smoothed cutoff features found in {path}")
    return dict(sorted(lines.items()))


def resample_closed_line(line: LineString, spacing_m: float) -> tuple[np.ndarray, float, float]:
    total_length = float(line.length)
    n_samples = max(64, int(round(total_length / spacing_m)))
    distances = np.linspace(0.0, total_length, n_samples, endpoint=False)
    points = [line.interpolate(float(distance)) for distance in distances]
    coords = np.array([[point.x, point.y] for point in points], dtype=float)
    return coords, total_length / n_samples, total_length


def closed_line_from_coords(coords: np.ndarray) -> LineString:
    return LineString(np.vstack([coords, coords[0]]))


def fit_circle(points: np.ndarray) -> dict[str, Any]:
    x = points[:, 0]
    y = points[:, 1]
    x_mean = float(np.mean(x))
    y_mean = float(np.mean(y))
    u = x - x_mean
    v = y - y_mean
    matrix = np.column_stack([2.0 * u, 2.0 * v, np.ones_like(u)])
    target = u * u + v * v
    solution, *_ = np.linalg.lstsq(matrix, target, rcond=None)
    u_center, v_center, constant = solution
    center = np.array([x_mean + u_center, y_mean + v_center], dtype=float)
    radius = float(math.sqrt(max(u_center * u_center + v_center * v_center + constant, 0.0)))
    radial_distance = np.sqrt(np.sum((points - center) ** 2, axis=1))
    residual = radial_distance - radius
    return {
        "center": center,
        "radius_m": radius,
        "rmse_m": float(np.sqrt(np.mean(residual**2))),
        "max_abs_residual_m": float(np.max(np.abs(residual))),
    }


def north_arc_fit(coords: np.ndarray, sample_spacing_m: float, half_window_m: float) -> dict[str, Any]:
    north_index = int(np.argmax(coords[:, 1]))
    half_count = max(5, int(round(half_window_m / sample_spacing_m)))
    indexes = np.arange(north_index - half_count, north_index + half_count + 1) % coords.shape[0]
    points = coords[indexes]
    fit = fit_circle(points)
    fit["north_index"] = north_index
    fit["half_window_m"] = float(half_window_m)
    fit["arc_length_m"] = float(2.0 * half_window_m)
    fit["point_count"] = int(points.shape[0])
    fit["north_point"] = coords[north_index].tolist()
    return fit


def curvature_signal(coords: np.ndarray, sample_spacing_m: float, smooth_sigma_m: float) -> np.ndarray:
    sigma = max(smooth_sigma_m / sample_spacing_m, 0.0)
    x = coords[:, 0]
    y = coords[:, 1]
    if sigma > 0:
        x = gaussian_filter1d(x, sigma=sigma, mode="wrap")
        y = gaussian_filter1d(y, sigma=sigma, mode="wrap")
    dx = np.gradient(x, sample_spacing_m, edge_order=2)
    dy = np.gradient(y, sample_spacing_m, edge_order=2)
    ddx = np.gradient(dx, sample_spacing_m, edge_order=2)
    ddy = np.gradient(dy, sample_spacing_m, edge_order=2)
    denominator = np.maximum((dx * dx + dy * dy) ** 1.5, 1e-9)
    return (dx * ddy - dy * ddx) / denominator


def radius_summary(curvature: np.ndarray) -> dict[str, float]:
    radius = 1.0 / np.maximum(np.abs(curvature), 1e-9)
    radius = radius[np.isfinite(radius) & (radius < 1000.0)]
    if radius.size == 0:
        return {}
    return {
        "p0_5_m": float(np.percentile(radius, 0.5)),
        "p1_m": float(np.percentile(radius, 1.0)),
        "p5_m": float(np.percentile(radius, 5.0)),
        "p10_m": float(np.percentile(radius, 10.0)),
        "median_m": float(np.median(radius)),
        "min_m": float(np.min(radius)),
        "max_abs_curvature_1_per_m": float(np.max(np.abs(curvature))),
    }


def curvature_spectrum(curvature: np.ndarray, sample_spacing_m: float) -> dict[str, np.ndarray]:
    centered = curvature - np.mean(curvature)
    coefficients = np.fft.rfft(centered)
    frequencies = np.fft.rfftfreq(centered.size, d=sample_spacing_m)
    valid = frequencies > 0
    wavelengths = 1.0 / frequencies[valid]
    power = (np.abs(coefficients[valid]) ** 2) / centered.size
    return {
        "wavelength_m": wavelengths,
        "power": power,
        "amplitude": np.sqrt(power),
    }


def line_to_ortho(line: LineString, src_crs: Any, ortho_crs: Any) -> LineString:
    return transform_geometry(line, src_crs, ortho_crs)


def render_analysis(
    output_path: Path,
    ortho_path: Path,
    raw_line_4326: LineString,
    smoothed_lines_4326: dict[float, LineString],
    diagnostics: dict[str, Any],
    north_reference_cutoff_m: float,
    north_reference_window_m: float,
    spectrum_cutoffs: list[float],
) -> None:
    rgb, ortho_crs, bounds = load_ortho_preview(ortho_path)
    extent = [bounds[0], bounds[2], bounds[1], bounds[3]]
    measurement_crs = CRS.from_user_input(diagnostics["measurement_crs"])
    reference_line_4326 = smoothed_lines_4326[north_reference_cutoff_m]
    reference_line = transform_geometry(reference_line_4326, "EPSG:4326", measurement_crs)
    ref_coords, ref_spacing_m, _ref_length = resample_closed_line(reference_line, diagnostics["sample_spacing_m"])
    ref_fit = north_arc_fit(ref_coords, ref_spacing_m, north_reference_window_m)
    north_index = ref_fit["north_index"]
    half_count = max(5, int(round(north_reference_window_m / ref_spacing_m)))
    arc_indexes = np.arange(north_index - half_count, north_index + half_count + 1) % ref_coords.shape[0]
    north_arc = closed_line_from_coords(ref_coords[arc_indexes])
    center = ref_fit["center"]
    radius = ref_fit["radius_m"]
    theta = np.linspace(0.0, 2.0 * np.pi, 360)
    circle = LineString(np.column_stack([center[0] + radius * np.cos(theta), center[1] + radius * np.sin(theta)]))

    fig, axes = plt.subplots(2, 2, figsize=(14, 14), dpi=180)
    axes = axes.ravel()

    axes[0].imshow(rgb, extent=extent)
    raw_native = line_to_ortho(raw_line_4326, "EPSG:4326", ortho_crs)
    ref_native = line_to_ortho(reference_line_4326, "EPSG:4326", ortho_crs)
    arc_native = transform_geometry(north_arc, measurement_crs, ortho_crs)
    circle_native = transform_geometry(circle, measurement_crs, ortho_crs)
    axes[0].plot(*raw_native.xy, color="#ff4f7b", linewidth=0.9, alpha=0.4, label="raw fused")
    axes[0].plot(*ref_native.xy, color="#00e0ff", linewidth=1.7, alpha=0.9, label=f"{north_reference_cutoff_m:g} m filtered")
    axes[0].plot(*arc_native.xy, color="#ffd84d", linewidth=3.0, label="north fit arc")
    axes[0].plot(*circle_native.xy, color="#ffffff", linewidth=1.0, linestyle="--", alpha=0.85, label=f"r={radius:.1f} m")
    axes[0].set_title("north bend circle fit")
    axes[0].legend(loc="lower right", frameon=True, fontsize=8)

    windows = diagnostics["north_curve_radius"]["half_windows_m"]
    for cutoff_key, values in diagnostics["north_curve_radius"]["by_cutoff"].items():
        if cutoff_key == "raw":
            continue
        cutoff = float(cutoff_key)
        radii = [values[str(window)]["radius_m"] for window in windows]
        color = "#222222" if cutoff == north_reference_cutoff_m else None
        linewidth = 2.4 if cutoff == north_reference_cutoff_m else 1.0
        alpha = 1.0 if cutoff == north_reference_cutoff_m else 0.45
        axes[1].plot(windows, radii, marker="o", linewidth=linewidth, alpha=alpha, color=color, label=f"{cutoff:g} m")
    axes[1].axhspan(
        diagnostics["physical_radius_estimate_m"]["p25_m"],
        diagnostics["physical_radius_estimate_m"]["p75_m"],
        color="#ffd84d",
        alpha=0.2,
        label="central estimate IQR",
    )
    axes[1].set_xlabel("north-arc half-window (m)")
    axes[1].set_ylabel("circle-fit radius (m)")
    axes[1].set_title("north bend radius stability")
    axes[1].grid(True, alpha=0.25)
    axes[1].legend(frameon=False, fontsize=8, ncols=2)

    cutoffs = [float(key) for key in diagnostics["curvature_radius_summary"].keys()]
    p1 = [diagnostics["curvature_radius_summary"][str(cutoff)]["p1_m"] for cutoff in cutoffs]
    p5 = [diagnostics["curvature_radius_summary"][str(cutoff)]["p5_m"] for cutoff in cutoffs]
    p10 = [diagnostics["curvature_radius_summary"][str(cutoff)]["p10_m"] for cutoff in cutoffs]
    target = diagnostics["physical_radius_estimate_m"]["median_m"]
    axes[2].plot(cutoffs, p1, marker="o", label="1st percentile")
    axes[2].plot(cutoffs, p5, marker="o", label="5th percentile")
    axes[2].plot(cutoffs, p10, marker="o", label="10th percentile")
    axes[2].axhline(target, color="#222222", linestyle="--", linewidth=1.0, label=f"north radius ~{target:.1f} m")
    axes[2].set_xlabel("Fourier cutoff wavelength (m)")
    axes[2].set_ylabel("local curvature radius (m)")
    axes[2].set_title(f"radius percentiles, curvature scale {diagnostics['curvature_sigma_m']:g} m")
    axes[2].grid(True, alpha=0.25)
    axes[2].legend(frameon=False)

    for label, spectrum_data in diagnostics["curvature_spectrum"].items():
        if label == "raw" or float(label) in spectrum_cutoffs:
            wavelengths = np.array(spectrum_data["wavelength_m"])
            amplitudes = np.array(spectrum_data["amplitude"])
            order = np.argsort(wavelengths)
            axes[3].plot(wavelengths[order], amplitudes[order], linewidth=1.0 if label != "raw" else 1.4, label=label)
    axes[3].axvspan(0.5, diagnostics["physical_arc_scale_m"], color="#de2d26", alpha=0.08, label="below physical arc scale")
    axes[3].set_xscale("log")
    axes[3].set_yscale("log")
    axes[3].set_xlim(1.0, 140.0)
    axes[3].set_xlabel("curvature wavelength (m)")
    axes[3].set_ylabel("curvature amplitude")
    axes[3].set_title("curvature spectrum")
    axes[3].grid(True, which="both", alpha=0.2)
    axes[3].legend(frameon=False, fontsize=8)

    for ax in axes[:1]:
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
    parser.add_argument("--raw-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-fused-segmentation-candidates.geojson")
    parser.add_argument("--smoothed-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-fused-smoothed-candidates.geojson")
    parser.add_argument("--method", default="fused")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "curvature-radius-analysis.png")
    parser.add_argument("--diagnostics", type=Path, default=PROJECT_ROOT / "build" / "fusion" / "curvature-radius-diagnostics.json")
    parser.add_argument("--sample-spacing-m", type=float, default=0.2)
    parser.add_argument("--curvature-sigma-m", type=float, default=2.0)
    parser.add_argument("--spectrum-sigma-m", type=float, default=1.0)
    parser.add_argument("--north-half-windows-m", nargs="+", type=float, default=[8.0, 10.0, 12.0, 15.0, 18.0, 22.0])
    parser.add_argument("--physical-fit-cutoffs-m", nargs="+", type=float, default=[4.0, 5.0, 6.0, 8.0])
    parser.add_argument("--physical-fit-windows-m", nargs="+", type=float, default=[8.0, 10.0, 12.0])
    parser.add_argument("--north-reference-cutoff-m", type=float, default=8.0)
    parser.add_argument("--north-reference-window-m", type=float, default=12.0)
    parser.add_argument("--spectrum-cutoffs-m", nargs="+", type=float, default=[4.0, 8.0, 12.0])
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    raw_line_4326 = load_raw_line(args.raw_geojson, args.method)
    smoothed_lines_4326 = load_smoothed_lines(args.smoothed_geojson)
    if args.north_reference_cutoff_m not in smoothed_lines_4326:
        raise ValueError(f"north reference cutoff {args.north_reference_cutoff_m} m not found")

    all_lines = {"raw": transform_geometry(raw_line_4326, "EPSG:4326", measurement_crs)}
    all_lines.update(
        {
            str(cutoff): transform_geometry(line, "EPSG:4326", measurement_crs)
            for cutoff, line in smoothed_lines_4326.items()
        }
    )
    diagnostics: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "measurement_crs": str(measurement_crs),
        "raw_geojson": str(args.raw_geojson),
        "smoothed_geojson": str(args.smoothed_geojson),
        "sample_spacing_m": float(args.sample_spacing_m),
        "curvature_sigma_m": float(args.curvature_sigma_m),
        "spectrum_sigma_m": float(args.spectrum_sigma_m),
        "north_curve_radius": {
            "half_windows_m": [float(value) for value in args.north_half_windows_m],
            "by_cutoff": {},
        },
        "curvature_radius_summary": {},
        "curvature_spectrum": {},
    }

    physical_radius_samples: list[float] = []
    for name, line in all_lines.items():
        coords, spacing_m, length_m = resample_closed_line(line, args.sample_spacing_m)
        diagnostics["north_curve_radius"]["by_cutoff"][name] = {}
        for half_window_m in args.north_half_windows_m:
            fit = north_arc_fit(coords, spacing_m, half_window_m)
            fit_json = {
                key: (value.tolist() if isinstance(value, np.ndarray) else value)
                for key, value in fit.items()
            }
            diagnostics["north_curve_radius"]["by_cutoff"][name][str(float(half_window_m))] = fit_json
            if (
                name != "raw"
                and float(name) in args.physical_fit_cutoffs_m
                and float(half_window_m) in args.physical_fit_windows_m
            ):
                physical_radius_samples.append(float(fit["radius_m"]))

        curvature = curvature_signal(coords, spacing_m, args.curvature_sigma_m)
        spectrum_curvature = curvature_signal(coords, spacing_m, args.spectrum_sigma_m)
        if name != "raw":
            diagnostics["curvature_radius_summary"][name] = {
                **radius_summary(curvature),
                "length_m": float(length_m),
            }
        spectrum = curvature_spectrum(spectrum_curvature, spacing_m)
        diagnostics["curvature_spectrum"][name] = {
            "wavelength_m": spectrum["wavelength_m"].tolist(),
            "amplitude": spectrum["amplitude"].tolist(),
        }

    if not physical_radius_samples:
        raise ValueError("no samples available for physical radius estimate")
    samples = np.array(physical_radius_samples, dtype=float)
    median_radius = float(np.median(samples))
    diagnostics["physical_radius_estimate_m"] = {
        "median_m": median_radius,
        "p25_m": float(np.percentile(samples, 25)),
        "p75_m": float(np.percentile(samples, 75)),
        "min_m": float(np.min(samples)),
        "max_m": float(np.max(samples)),
        "sample_count": int(samples.size),
        "source_cutoffs_m": [float(value) for value in args.physical_fit_cutoffs_m],
        "source_half_windows_m": [float(value) for value in args.physical_fit_windows_m],
    }
    diagnostics["physical_arc_scale_m"] = float(median_radius * np.pi / 2.0)
    diagnostics["interpretation"] = (
        "The north bend central radius is the tightest obvious physical curb curve. "
        "Fourier cutoff wavelength is not the same as radius: a 90 degree bend with this radius "
        "occupies an arc length of roughly radius*pi/2. Cutoffs well below that arc scale should "
        "preserve the real bend while removing shorter wiggles."
    )

    render_analysis(
        args.output,
        args.ortho,
        raw_line_4326,
        smoothed_lines_4326,
        diagnostics,
        args.north_reference_cutoff_m,
        args.north_reference_window_m,
        args.spectrum_cutoffs_m,
    )
    write_json(args.diagnostics, diagnostics)
    print(f"wrote {args.output}")
    print(f"wrote {args.diagnostics}")
    print(f"north radius estimate: {median_radius:.2f} m")
    print(f"90 degree physical arc scale: {diagnostics['physical_arc_scale_m']:.2f} m")


if __name__ == "__main__":
    main()
