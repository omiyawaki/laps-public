#!/usr/bin/env python3
"""Build a reviewable curb/running-line candidate from the 0.5-ft ortho crop.

The detector treats the curb as a smooth physical object. It samples inward
    transects from the NYS Streets centerlines, looks for local changepoints
    between road-like and non-road-like pixels, rejects implausible/outlier
    detections, then fills gaps with a smooth curve before generating the 2 ft
    running-line offset.
"""

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
import requests
import yaml
from pyproj import CRS, Transformer
from scipy.interpolate import UnivariateSpline
from scipy.ndimage import gaussian_filter1d
from skimage.restoration import denoise_tv_chambolle
from shapely.geometry import LineString, MultiLineString, Point, Polygon, mapping
from shapely.ops import transform


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSTPROCESS_METHODS = (
    "spline",
    "viterbi",
    "hampel",
    "tv",
    "ridge",
    "elastic-net",
    "hybrid",
)
TRANSECT_DIRECTIONS = ("normal", "radial-center", "multi-ray")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def arcgis_query(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS query failed: {data['error']}")
    return data


def query_loop_centerlines(config: dict[str, Any]) -> list[dict[str, Any]]:
    objectids = config["imagery_crop"]["selected_street_objectids"]
    feature_layer = config["sources"]["nys_streets"]["feature_layer"].rstrip("/")
    query_url = f"{feature_layer}/query"
    where = f"OBJECTID IN ({','.join(str(item) for item in objectids)})"
    data = arcgis_query(
        query_url,
        {
            "f": "json",
            "where": where,
            "outFields": "OBJECTID,CompleteStreetName,Label,Shape__Length",
            "returnGeometry": "true",
            "outSR": "4326",
        },
    )
    features = data.get("features", [])
    found = {feature["attributes"]["OBJECTID"] for feature in features}
    missing = set(objectids) - found
    if missing:
        raise RuntimeError(f"Configured street OBJECTIDs not returned: {sorted(missing)}")
    return features


def line_from_feature(feature: dict[str, Any]) -> LineString:
    paths = feature["geometry"]["paths"]
    if len(paths) != 1:
        raise RuntimeError("Expected one path per selected street feature")
    return LineString(paths[0])


def transform_geometry(geometry: Any, src_crs: Any, dst_crs: Any) -> Any:
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return transform(transformer.transform, geometry)


def point_at_distance(line: LineString, distance: float) -> np.ndarray:
    point = line.interpolate(distance)
    return np.array([point.x, point.y], dtype=float)


def unit_tangent(line: LineString, distance: float, eps: float) -> np.ndarray:
    before = point_at_distance(line, max(distance - eps, 0.0))
    after = point_at_distance(line, min(distance + eps, line.length))
    vector = after - before
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        return np.array([1.0, 0.0])
    return vector / norm


def inward_normal(line: LineString, distance: float, loop_polygon: Polygon, eps: float) -> np.ndarray:
    point = point_at_distance(line, distance)
    tangent = unit_tangent(line, distance, eps)
    normal = np.array([-tangent[1], tangent[0]])
    plus = Point(*(point + normal * eps * 4.0))
    minus = Point(*(point - normal * eps * 4.0))

    if loop_polygon.contains(plus) and not loop_polygon.contains(minus):
        return normal
    if loop_polygon.contains(minus) and not loop_polygon.contains(plus):
        return -normal

    centroid = np.array([loop_polygon.centroid.x, loop_polygon.centroid.y])
    return normal if np.dot(centroid - point, normal) >= 0 else -normal


def transect_direction_vector(
    center: np.ndarray,
    fallback_normal: np.ndarray,
    loop_polygon: Polygon,
    eps: float,
    mode: str,
    target_xy: np.ndarray | None,
) -> np.ndarray:
    if mode == "normal" or target_xy is None:
        return fallback_normal
    if mode != "radial-center":
        raise ValueError(f"Unknown transect direction: {mode}")

    vector = target_xy - center
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-9:
        return fallback_normal
    radial = vector / norm
    if loop_polygon.contains(Point(*(center + radial * eps * 4.0))):
        return radial
    return fallback_normal


def unique_vectors(vectors: list[np.ndarray], min_angle_deg: float = 3.0) -> list[np.ndarray]:
    unique: list[np.ndarray] = []
    min_angle = math.radians(min_angle_deg)
    for vector in vectors:
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-9:
            continue
        unit = vector / norm
        if any(math.acos(float(np.clip(np.dot(unit, kept), -1.0, 1.0))) < min_angle for kept in unique):
            continue
        unique.append(unit)
    return unique


def build_multi_ray_targets(loop_polygon: Polygon, anchor_xy: np.ndarray, grid_size: int) -> list[np.ndarray]:
    targets = [
        anchor_xy,
        np.array([loop_polygon.centroid.x, loop_polygon.centroid.y], dtype=float),
    ]
    minx, miny, maxx, maxy = loop_polygon.bounds
    if grid_size > 1:
        xs = np.linspace(minx, maxx, grid_size)
        ys = np.linspace(miny, maxy, grid_size)
        for x in xs:
            for y in ys:
                point = Point(float(x), float(y))
                if loop_polygon.contains(point):
                    targets.append(np.array([float(x), float(y)], dtype=float))

    unique: list[np.ndarray] = []
    for target in targets:
        if not loop_polygon.contains(Point(float(target[0]), float(target[1]))):
            continue
        if any(float(np.linalg.norm(target - kept)) < 0.5 for kept in unique):
            continue
        unique.append(target)
    return unique


def multi_ray_directions(
    center: np.ndarray,
    fallback_normal: np.ndarray,
    loop_polygon: Polygon,
    eps: float,
    targets_xy: list[np.ndarray],
    max_rays: int,
) -> list[np.ndarray]:
    vectors = [fallback_normal]
    for target in targets_xy:
        vector = target - center
        norm = float(np.linalg.norm(vector))
        if norm <= 1e-9:
            continue
        unit = vector / norm
        if np.dot(unit, fallback_normal) <= 0.04:
            continue
        if not loop_polygon.contains(Point(*(center + unit * eps * 4.0))):
            continue
        vectors.append(unit)

    unique = unique_vectors(vectors)
    if len(unique) <= max_rays:
        return unique

    angles = np.array([math.atan2(vector[1], vector[0]) for vector in unique])
    fallback_angle = math.atan2(fallback_normal[1], fallback_normal[0])
    angular_distance = np.abs(np.angle(np.exp(1j * (angles - fallback_angle))))
    ordered = [unique[index] for index in np.argsort(angular_distance)]
    keep_indexes = np.linspace(0, len(ordered) - 1, max_rays).round().astype(int)
    return [ordered[int(index)] for index in sorted(set(keep_indexes))]


def unit_conversion_to_meters(crs: CRS) -> float:
    return float(crs.axis_info[0].unit_conversion_factor)


def meters_to_units(meters: float, unit_to_meter: float) -> float:
    return meters / unit_to_meter


def units_to_meters(units: float, unit_to_meter: float) -> float:
    return units * unit_to_meter


def stretch_rgb(array: np.ndarray) -> np.ndarray:
    rgb = array[:3].astype(float)
    output = np.empty_like(rgb, dtype=np.uint8)
    for index in range(3):
        band = rgb[index]
        lo, hi = np.percentile(band[band > 0], [1, 99]) if np.any(band > 0) else (0, 255)
        if hi <= lo:
            hi = lo + 1
        output[index] = np.clip((band - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
    return np.moveaxis(output, 0, -1)


def sample_raster_bilinear(array: np.ndarray, inverse_transform: Any, x: float, y: float) -> np.ndarray | None:
    col, row = inverse_transform * (x, y)
    height, width = array.shape[1], array.shape[2]
    if col < 0 or row < 0 or col >= width - 1 or row >= height - 1:
        return None

    x0 = int(math.floor(col))
    y0 = int(math.floor(row))
    dx = col - x0
    dy = row - y0
    top = array[:, y0, x0] * (1 - dx) + array[:, y0, x0 + 1] * dx
    bottom = array[:, y0 + 1, x0] * (1 - dx) + array[:, y0 + 1, x0 + 1] * dx
    return top * (1 - dy) + bottom * dy


def spectral_features(samples: np.ndarray) -> dict[str, np.ndarray]:
    rgb = samples[:, :3]
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    nir = samples[:, 3] if samples.shape[1] >= 4 else np.zeros_like(r)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    chroma = np.max(rgb, axis=1) - np.min(rgb, axis=1)
    grayness = luminance - 1.15 * chroma
    ndvi = (nir - r) / (nir + r + 1e-6)
    green_excess = 2.0 * g - r - b
    return {
        "r": r,
        "g": g,
        "b": b,
        "nir": nir,
        "luminance": luminance,
        "chroma": chroma,
        "grayness": grayness,
        "ndvi": ndvi,
        "green_excess": green_excess,
    }


def robust_zscore_matrix(values: np.ndarray) -> np.ndarray:
    center = np.nanmedian(values, axis=0)
    mad = np.nanmedian(np.abs(values - center), axis=0)
    scale = np.where(mad > 1e-6, 1.4826 * mad, np.nanstd(values, axis=0))
    scale = np.where(scale > 1e-6, scale, 1.0)
    return np.clip((values - center) / scale, -5.0, 5.0)


def transect_feature_matrix(samples: np.ndarray) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    features = spectral_features(samples)
    columns = [
        gaussian_filter1d(features["r"], sigma=0.9),
        gaussian_filter1d(features["g"], sigma=0.9),
        gaussian_filter1d(features["b"], sigma=0.9),
        gaussian_filter1d(features["nir"], sigma=0.9),
        gaussian_filter1d(features["luminance"], sigma=0.9),
        gaussian_filter1d(features["chroma"], sigma=0.9),
        gaussian_filter1d(features["ndvi"], sigma=0.9),
        gaussian_filter1d(features["green_excess"], sigma=0.9),
    ]
    return robust_zscore_matrix(np.column_stack(columns)), features


def median_in_window(values: np.ndarray, distances: np.ndarray, lo: float, hi: float) -> float:
    mask = (distances >= lo) & (distances <= hi)
    if np.count_nonzero(mask) == 0:
        return float("nan")
    return float(np.nanmedian(values[mask]))


def curb_score_profile(samples: np.ndarray, distances: np.ndarray) -> dict[str, np.ndarray]:
    features = spectral_features(samples)
    luminance = gaussian_filter1d(features["luminance"], sigma=1.0)
    grayness = gaussian_filter1d(features["grayness"], sigma=1.0)
    chroma = gaussian_filter1d(features["chroma"], sigma=1.0)
    ndvi = gaussian_filter1d(features["ndvi"], sigma=1.0)
    green = gaussian_filter1d(features["green_excess"], sigma=1.0)
    gray_gradient = np.gradient(grayness, distances)

    score = np.full_like(distances, -2.0, dtype=float)
    for index in range(len(distances)):
        distance = float(distances[index])
        road_gray = median_in_window(grayness, distances, distance - 2.0, distance - 0.45)
        road_lum = median_in_window(luminance, distances, distance - 2.0, distance - 0.45)
        inner_ndvi = median_in_window(ndvi, distances, distance + 0.45, distance + 2.0)
        inner_green = median_in_window(green, distances, distance + 0.45, distance + 2.0)
        if not np.isfinite(road_gray) or not np.isfinite(road_lum):
            continue
        if not np.isfinite(inner_ndvi):
            inner_ndvi = 0.0
        if not np.isfinite(inner_green):
            inner_green = 0.0

        road_contrast = grayness[index] - road_gray
        light_contrast = luminance[index] - road_lum
        vegetation_support = max(0.0, inner_ndvi - ndvi[index]) + 0.25 * max(0.0, inner_green)
        vegetation_penalty = max(0.0, ndvi[index] - 0.18)
        chroma_penalty = max(0.0, chroma[index] - 0.16)
        peak_score = grayness[index] - 0.5 * (
            grayness[max(index - 4, 0)] + grayness[min(index + 4, len(grayness) - 1)]
        )

        score[index] = (
            1.5 * grayness[index]
            + 1.8 * max(0.0, road_contrast)
            + 1.0 * max(0.0, light_contrast)
            + 0.45 * vegetation_support
            + 0.35 * max(0.0, peak_score)
            + 0.15 * max(0.0, gray_gradient[index])
            - 1.7 * vegetation_penalty
            - 0.9 * chroma_penalty
        )

    return {
        "score": gaussian_filter1d(score, sigma=0.75),
        "luminance": luminance,
        "grayness": grayness,
        "chroma": chroma,
        "ndvi": ndvi,
        "green_excess": green,
        "gray_gradient": gray_gradient,
    }


def local_peak_indexes(values: np.ndarray, search_indexes: np.ndarray) -> list[int]:
    peaks: list[int] = []
    search_set = set(int(index) for index in search_indexes)
    for index in search_indexes:
        i = int(index)
        if i - 1 not in search_set or i + 1 not in search_set:
            continue
        if values[i] >= values[i - 1] and values[i] >= values[i + 1]:
            peaks.append(i)
    return peaks


def change_point_profile(
    samples: np.ndarray,
    distances: np.ndarray,
    unit_to_meter: float,
    left_window_m: float = 0.85,
    right_window_m: float = 1.05,
    gap_m: float = 0.05,
) -> dict[str, np.ndarray]:
    matrix, features = transect_feature_matrix(samples)
    smoothed = np.apply_along_axis(gaussian_filter1d, 0, matrix, sigma=0.75)
    left_window = meters_to_units(left_window_m, unit_to_meter)
    right_window = meters_to_units(right_window_m, unit_to_meter)
    gap = meters_to_units(gap_m, unit_to_meter)
    score = np.full(len(distances), np.nan, dtype=float)
    contrast = np.full(len(distances), np.nan, dtype=float)
    pooled_variability = np.full(len(distances), np.nan, dtype=float)
    direction_support = np.full(len(distances), 0.0, dtype=float)

    weights = np.array([0.75, 0.75, 0.75, 0.95, 0.45, 0.45, 1.25, 0.75])
    for index, distance in enumerate(distances):
        left_mask = (distances >= distance - left_window - gap) & (distances <= distance - gap)
        right_mask = (distances >= distance + gap) & (distances <= distance + right_window + gap)
        if np.count_nonzero(left_mask) < 5 or np.count_nonzero(right_mask) < 5:
            continue

        left = smoothed[left_mask]
        right = smoothed[right_mask]
        delta = np.nanmean(right, axis=0) - np.nanmean(left, axis=0)
        left_var = np.nanmedian(np.nanvar(left, axis=0))
        right_var = np.nanmedian(np.nanvar(right, axis=0))
        pooled = float(max(0.0, left_var + right_var))
        raw_contrast = float(np.sqrt(np.sum(weights * delta**2) / np.sum(weights)))

        left_ndvi = float(np.nanmedian(features["ndvi"][left_mask]))
        right_ndvi = float(np.nanmedian(features["ndvi"][right_mask]))
        left_green = float(np.nanmedian(features["green_excess"][left_mask]))
        right_green = float(np.nanmedian(features["green_excess"][right_mask]))
        left_lum = float(np.nanmedian(features["luminance"][left_mask]))
        right_lum = float(np.nanmedian(features["luminance"][right_mask]))
        vegetation_gain = max(0.0, right_ndvi - left_ndvi) / 0.18 + 0.35 * max(0.0, right_green - left_green)
        brightness_change = abs(right_lum - left_lum) / 0.12

        contrast[index] = raw_contrast
        pooled_variability[index] = pooled
        direction_support[index] = 0.22 * vegetation_gain + 0.08 * brightness_change
        score[index] = raw_contrast / math.sqrt(0.35 + pooled) + direction_support[index]

    return {
        "score": gaussian_filter1d(np.nan_to_num(score, nan=-1.0), sigma=0.65),
        "contrast": contrast,
        "pooled_variability": pooled_variability,
        "direction_support": direction_support,
        "ndvi": features["ndvi"],
        "green_excess": features["green_excess"],
        "luminance": features["luminance"],
        "chroma": features["chroma"],
    }


def change_point_candidates(
    samples: np.ndarray,
    distances: np.ndarray,
    min_search: float,
    max_search: float,
    unit_to_meter: float,
    max_candidates: int = 6,
) -> list[dict[str, Any]]:
    profile = change_point_profile(samples, distances, unit_to_meter)
    score = profile["score"]
    search_indexes = np.where((distances >= min_search) & (distances <= max_search))[0]
    if search_indexes.size == 0:
        return []

    search_scores = score[search_indexes]
    finite_scores = search_scores[np.isfinite(search_scores)]
    if finite_scores.size == 0:
        return []

    floor = max(float(np.percentile(finite_scores, 68)), float(np.max(finite_scores) - 0.55))
    peaks = local_peak_indexes(score, search_indexes)
    if not peaks:
        peaks = [int(search_indexes[int(np.argmax(search_scores))])]

    candidates: list[dict[str, Any]] = []
    for peak in sorted(peaks, key=lambda idx: score[idx], reverse=True)[: max_candidates * 4]:
        peak_score = float(score[peak])
        if peak_score < floor:
            continue

        band_floor = max(floor, peak_score - 0.32)
        left = peak
        right = peak
        while left > search_indexes[0] and score[left - 1] >= band_floor:
            left -= 1
        while right < search_indexes[-1] and score[right + 1] >= band_floor:
            right += 1

        width = float(distances[right] - distances[left])
        width_m = units_to_meters(width, unit_to_meter)
        if width_m < 0.08 or width_m > 1.65:
            continue

        band_slice = slice(left, right + 1)
        weights = np.maximum(score[band_slice] - band_floor + 1e-4, 1e-4)
        midpoint = float(np.average(distances[band_slice], weights=weights))
        mid_index = int(np.argmin(np.abs(distances - midpoint)))
        local_contrast = float(profile["contrast"][peak])
        local_variability = float(profile["pooled_variability"][peak])
        support = float(profile["direction_support"][peak])
        confidence = peak_score + 0.18 * local_contrast - 0.12 * local_variability
        accepted_by_signal = bool(confidence >= 1.18 and local_contrast >= 0.65 and local_variability <= 3.8)
        candidates.append(
            {
                "method": "multiband_changepoint",
                "left_distance_units": float(distances[left]),
                "right_distance_units": float(distances[right]),
                "mid_distance_units": midpoint,
                "width_units": width,
                "left_distance_m": units_to_meters(float(distances[left]), unit_to_meter),
                "right_distance_m": units_to_meters(float(distances[right]), unit_to_meter),
                "mid_distance_m": units_to_meters(midpoint, unit_to_meter),
                "width_m": width_m,
                "score": float(confidence),
                "peak_score": peak_score,
                "change_contrast": local_contrast,
                "pooled_variability": local_variability,
                "direction_support": support,
                "accepted_by_signal": accepted_by_signal,
                "accepted_by_spectrum": accepted_by_signal,
                "luminance": float(profile["luminance"][mid_index]),
                "chroma": float(profile["chroma"][mid_index]),
                "ndvi": float(profile["ndvi"][mid_index]),
                "green_excess": float(profile["green_excess"][mid_index]),
            }
        )

    candidates.sort(key=lambda item: item["score"], reverse=True)
    deduped: list[dict[str, Any]] = []
    min_separation = meters_to_units(0.25, unit_to_meter)
    for candidate in candidates:
        if any(abs(candidate["mid_distance_units"] - kept["mid_distance_units"]) < min_separation for kept in deduped):
            continue
        deduped.append(candidate)
        if len(deduped) >= max_candidates:
            break
    return deduped


def curb_band_candidates(
    samples: np.ndarray,
    distances: np.ndarray,
    min_search: float,
    max_search: float,
    unit_to_meter: float,
    max_candidates: int = 5,
) -> list[dict[str, Any]]:
    profiles = curb_score_profile(samples, distances)
    score = profiles["score"]
    search_indexes = np.where((distances >= min_search) & (distances <= max_search))[0]
    if search_indexes.size == 0:
        return []

    search_scores = score[search_indexes]
    finite_scores = search_scores[np.isfinite(search_scores)]
    if finite_scores.size == 0:
        return []

    floor = max(float(np.percentile(finite_scores, 72)), float(np.max(finite_scores) - 0.22))
    peaks = local_peak_indexes(score, search_indexes)
    if not peaks:
        peaks = [int(search_indexes[int(np.argmax(search_scores))])]

    candidates: list[dict[str, Any]] = []
    for peak in sorted(peaks, key=lambda idx: score[idx], reverse=True)[: max_candidates * 3]:
        peak_score = float(score[peak])
        if peak_score < floor:
            continue
        band_floor = max(floor, peak_score - 0.18)
        left = peak
        right = peak
        while left > search_indexes[0] and score[left - 1] >= band_floor:
            left -= 1
        while right < search_indexes[-1] and score[right + 1] >= band_floor:
            right += 1

        width = float(distances[right] - distances[left])
        width_m = units_to_meters(width, unit_to_meter)
        if width_m < 0.12 or width_m > 1.05:
            continue

        band_slice = slice(left, right + 1)
        weights = np.maximum(score[band_slice] - band_floor + 1e-4, 1e-4)
        midpoint = float(np.average(distances[band_slice], weights=weights))
        mid_index = int(np.argmin(np.abs(distances - midpoint)))
        road_lum = median_in_window(profiles["luminance"], distances, midpoint - 2.0, midpoint - 0.45)
        road_gray = median_in_window(profiles["grayness"], distances, midpoint - 2.0, midpoint - 0.45)
        inner_ndvi = median_in_window(profiles["ndvi"], distances, midpoint + 0.45, midpoint + 2.0)
        inner_green = median_in_window(profiles["green_excess"], distances, midpoint + 0.45, midpoint + 2.0)
        if not np.isfinite(road_lum):
            road_lum = 0.0
        if not np.isfinite(road_gray):
            road_gray = 0.0
        if not np.isfinite(inner_ndvi):
            inner_ndvi = 0.0
        if not np.isfinite(inner_green):
            inner_green = 0.0

        road_contrast = float(profiles["grayness"][mid_index] - road_gray)
        light_contrast = float(profiles["luminance"][mid_index] - road_lum)
        vegetation_after = float(max(0.0, inner_ndvi - profiles["ndvi"][mid_index]) + 0.2 * max(0.0, inner_green))
        width_penalty = abs(width_m - 0.38) * 0.45
        confidence = (
            peak_score
            + 1.1 * max(0.0, road_contrast)
            + 0.7 * max(0.0, light_contrast)
            + 0.35 * vegetation_after
            - width_penalty
        )
        accepted_by_spectrum = bool(
            confidence > 0.34
            and light_contrast > 0.012
            and road_contrast > 0.006
            and profiles["chroma"][mid_index] < 0.28
            and profiles["ndvi"][mid_index] < 0.38
        )
        candidates.append(
            {
                "left_distance_units": float(distances[left]),
                "right_distance_units": float(distances[right]),
                "mid_distance_units": midpoint,
                "width_units": width,
                "left_distance_m": units_to_meters(float(distances[left]), unit_to_meter),
                "right_distance_m": units_to_meters(float(distances[right]), unit_to_meter),
                "mid_distance_m": units_to_meters(midpoint, unit_to_meter),
                "width_m": width_m,
                "score": float(confidence),
                "peak_score": peak_score,
                "accepted_by_spectrum": accepted_by_spectrum,
                "luminance": float(profiles["luminance"][mid_index]),
                "grayness": float(profiles["grayness"][mid_index]),
                "chroma": float(profiles["chroma"][mid_index]),
                "ndvi": float(profiles["ndvi"][mid_index]),
                "road_luminance": float(road_lum),
                "road_grayness": float(road_gray),
                "inner_ndvi": float(inner_ndvi),
                "inner_green_excess": float(inner_green),
            }
        )

    candidates.sort(key=lambda item: item["score"], reverse=True)
    deduped: list[dict[str, Any]] = []
    min_separation = meters_to_units(0.25, unit_to_meter)
    for candidate in candidates:
        if any(abs(candidate["mid_distance_units"] - kept["mid_distance_units"]) < min_separation for kept in deduped):
            continue
        deduped.append(candidate)
        if len(deduped) >= max_candidates:
            break
    return deduped


def sample_one_ray(
    raster: np.ndarray,
    inverse_transform: Any,
    center: np.ndarray,
    direction: np.ndarray,
    transect_distances: np.ndarray,
) -> tuple[np.ndarray, np.ndarray] | None:
    samples: list[np.ndarray] = []
    valid_distances: list[float] = []
    for transect_distance in transect_distances:
        xy = center + direction * transect_distance
        pixel = sample_raster_bilinear(raster, inverse_transform, xy[0], xy[1])
        if pixel is None:
            continue
        samples.append(pixel)
        valid_distances.append(float(transect_distance))

    if len(samples) < 12:
        return None
    return np.vstack(samples), np.array(valid_distances)


def aggregate_multi_ray_candidates(
    center: np.ndarray,
    reference: np.ndarray,
    ray_candidates: list[dict[str, Any]],
    unit_to_meter: float,
    min_search: float,
    max_search: float,
    cluster_step_m: float,
    lateral_tolerance_m: float,
    min_votes: int,
    max_candidates: int = 6,
) -> list[dict[str, Any]]:
    accepted_votes: list[dict[str, Any]] = []
    cluster_step = meters_to_units(cluster_step_m, unit_to_meter)
    lateral_tolerance = meters_to_units(lateral_tolerance_m, unit_to_meter)
    for vote in ray_candidates:
        if not vote.get("accepted_by_signal", False):
            continue
        xy = np.array(vote["midpoint_xy"], dtype=float)
        delta = xy - center
        projected = float(np.dot(delta, reference))
        lateral_vector = delta - reference * projected
        lateral = float(np.linalg.norm(lateral_vector))
        if projected < min_search or projected > max_search:
            continue
        if lateral > lateral_tolerance * 2.0:
            continue
        angle_weight = max(0.0, float(np.dot(np.array(vote["ray_direction_xy"]), reference)))
        lateral_weight = math.exp(-0.5 * (lateral / max(lateral_tolerance, 1e-9)) ** 2)
        weight = max(0.0, float(vote["score"])) * max(angle_weight, 0.05) * max(lateral_weight, 0.05)
        accepted_votes.append(
            {
                **vote,
                "projected_distance_units": projected,
                "projected_distance_m": units_to_meters(projected, unit_to_meter),
                "lateral_offset_units": lateral,
                "lateral_offset_m": units_to_meters(lateral, unit_to_meter),
                "consensus_weight": weight,
                "angle_weight": angle_weight,
                "lateral_weight": lateral_weight,
            }
        )

    if not accepted_votes:
        return []

    accepted_votes.sort(key=lambda item: item["projected_distance_units"])
    clusters: list[list[dict[str, Any]]] = []
    for vote in accepted_votes:
        if not clusters or abs(vote["projected_distance_units"] - np.mean([item["projected_distance_units"] for item in clusters[-1]])) > cluster_step:
            clusters.append([vote])
        else:
            clusters[-1].append(vote)

    candidates: list[dict[str, Any]] = []
    for cluster in clusters:
        if len(cluster) < min_votes:
            continue
        weights = np.array([max(item["consensus_weight"], 1e-6) for item in cluster], dtype=float)
        projected = np.array([item["projected_distance_units"] for item in cluster], dtype=float)
        lateral = np.array([item["lateral_offset_units"] for item in cluster], dtype=float)
        score_values = np.array([float(item["score"]) for item in cluster], dtype=float)
        midpoint = float(np.average(projected, weights=weights))
        spread = float(np.sqrt(np.average((projected - midpoint) ** 2, weights=weights)))
        mean_lateral = float(np.average(lateral, weights=weights))
        confidence = float(np.sum(weights) / math.sqrt(len(cluster)) + np.max(score_values) - units_to_meters(spread, unit_to_meter))
        xy = center + reference * midpoint
        vote_summary = [
            {
                "ray_index": item["ray_index"],
                "ray_angle_deg": item["ray_angle_deg"],
                "score": item["score"],
                "projected_distance_m": item["projected_distance_m"],
                "lateral_offset_m": item["lateral_offset_m"],
                "mid_distance_m": item["mid_distance_m"],
            }
            for item in sorted(cluster, key=lambda item: item["consensus_weight"], reverse=True)[:12]
        ]
        candidates.append(
            {
                "method": "multi_ray_consensus",
                "left_distance_units": float(np.min(projected)),
                "right_distance_units": float(np.max(projected)),
                "mid_distance_units": midpoint,
                "width_units": float(max(np.max(projected) - np.min(projected), cluster_step)),
                "left_distance_m": units_to_meters(float(np.min(projected)), unit_to_meter),
                "right_distance_m": units_to_meters(float(np.max(projected)), unit_to_meter),
                "mid_distance_m": units_to_meters(midpoint, unit_to_meter),
                "width_m": units_to_meters(float(max(np.max(projected) - np.min(projected), cluster_step)), unit_to_meter),
                "score": confidence,
                "peak_score": float(np.max(score_values)),
                "vote_count": len(cluster),
                "raw_ray_candidate_count": len(ray_candidates),
                "mean_lateral_offset_m": units_to_meters(mean_lateral, unit_to_meter),
                "projected_spread_m": units_to_meters(spread, unit_to_meter),
                "accepted_by_signal": bool(len(cluster) >= min_votes and confidence > 0.45),
                "accepted_by_spectrum": bool(len(cluster) >= min_votes and confidence > 0.45),
                "midpoint_xy": [float(xy[0]), float(xy[1])],
                "votes": vote_summary,
            }
        )

    candidates.sort(key=lambda item: (item["vote_count"], item["score"]), reverse=True)
    return candidates[:max_candidates]


def sample_curb_candidates(
    line: LineString,
    loop_polygon: Polygon,
    raster: np.ndarray,
    inverse_transform: Any,
    unit_to_meter: float,
    transect_direction: str,
    transect_target_xy: np.ndarray | None,
    multi_ray_targets_xy: list[np.ndarray],
    multi_ray_max_rays: int,
    multi_ray_cluster_step_m: float,
    multi_ray_lateral_tolerance_m: float,
    multi_ray_min_votes: int,
    sample_spacing_m: float,
    endpoint_margin_m: float,
    min_search_m: float,
    max_search_m: float,
    transect_step_m: float,
) -> list[dict[str, Any]]:
    sample_spacing = meters_to_units(sample_spacing_m, unit_to_meter)
    endpoint_margin = meters_to_units(endpoint_margin_m, unit_to_meter)
    min_search = meters_to_units(min_search_m, unit_to_meter)
    max_search = meters_to_units(max_search_m, unit_to_meter)
    transect_step = meters_to_units(transect_step_m, unit_to_meter)
    tangent_eps = meters_to_units(0.5, unit_to_meter)

    distances_along = np.arange(endpoint_margin, max(line.length - endpoint_margin, endpoint_margin + 0.1), sample_spacing)
    transect_distances = np.arange(0.0, max_search + 0.001, transect_step)
    transects: list[dict[str, Any]] = []

    for distance_along in distances_along:
        center = point_at_distance(line, float(distance_along))
        fallback_normal = inward_normal(line, float(distance_along), loop_polygon, tangent_eps)
        reference = transect_direction_vector(
            center,
            fallback_normal,
            loop_polygon,
            tangent_eps,
            "radial-center" if transect_direction == "multi-ray" else transect_direction,
            transect_target_xy,
        )
        ray_count = 1
        if transect_direction == "multi-ray":
            directions = multi_ray_directions(
                center,
                fallback_normal,
                loop_polygon,
                tangent_eps,
                multi_ray_targets_xy,
                multi_ray_max_rays,
            )
            if not directions:
                directions = [reference]
            ray_candidates: list[dict[str, Any]] = []
            reference_angle = math.atan2(reference[1], reference[0])
            for ray_index, direction in enumerate(directions):
                sampled = sample_one_ray(raster, inverse_transform, center, direction, transect_distances)
                if sampled is None:
                    continue
                samples_array, distances_array = sampled
                ray_results = change_point_candidates(samples_array, distances_array, min_search, max_search, unit_to_meter)
                ray_angle = math.atan2(direction[1], direction[0])
                ray_angle_deg = math.degrees(math.atan2(math.sin(ray_angle - reference_angle), math.cos(ray_angle - reference_angle)))
                for candidate in ray_results:
                    midpoint_xy = center + direction * candidate["mid_distance_units"]
                    ray_candidates.append(
                        {
                            **candidate,
                            "ray_index": ray_index,
                            "ray_angle_deg": ray_angle_deg,
                            "ray_direction_xy": [float(direction[0]), float(direction[1])],
                            "midpoint_xy": [float(midpoint_xy[0]), float(midpoint_xy[1])],
                        }
                    )
            ray_count = len(directions)
            candidates = aggregate_multi_ray_candidates(
                center,
                reference,
                ray_candidates,
                unit_to_meter,
                min_search,
                max_search,
                multi_ray_cluster_step_m,
                multi_ray_lateral_tolerance_m,
                multi_ray_min_votes,
            )
        else:
            sampled = sample_one_ray(raster, inverse_transform, center, reference, transect_distances)
            if sampled is None:
                continue
            samples_array, distances_array = sampled
            candidates = change_point_candidates(samples_array, distances_array, min_search, max_search, unit_to_meter)
            for candidate in candidates:
                midpoint_xy = center + reference * candidate["mid_distance_units"]
                candidate["midpoint_xy"] = [float(midpoint_xy[0]), float(midpoint_xy[1])]

        transects.append(
            {
                "distance_along_units": float(distance_along),
                "distance_along_m": units_to_meters(float(distance_along), unit_to_meter),
                "center_xy": [float(center[0]), float(center[1])],
                "normal_xy": [float(reference[0]), float(reference[1])],
                "transect_direction": transect_direction,
                "ray_count": ray_count,
                "candidates": candidates,
            }
        )
    return transects


def evidence_on_grid(transect: dict[str, Any], distance_grid: np.ndarray, unit_to_meter: float) -> np.ndarray:
    evidence = np.zeros_like(distance_grid, dtype=float)
    sigma = meters_to_units(0.22, unit_to_meter)
    for candidate in transect["candidates"]:
        if not candidate.get("accepted_by_signal", candidate.get("accepted_by_spectrum", False)):
            continue
        amplitude = max(0.0, candidate["score"])
        bump = amplitude * np.exp(-0.5 * ((distance_grid - candidate["mid_distance_units"]) / sigma) ** 2)
        evidence = np.maximum(evidence, bump)
    return evidence


def signal_mask(path_evidence: np.ndarray) -> np.ndarray:
    return np.isfinite(path_evidence) & (path_evidence > 1e-9)


def hampel_keep_mask(
    values_m: np.ndarray,
    initial_mask: np.ndarray,
    window: int = 9,
    n_sigma: float = 3.0,
    floor_m: float = 0.42,
) -> np.ndarray:
    keep = initial_mask.copy()
    indexes = np.arange(len(values_m))
    for index in indexes[initial_mask]:
        lo = max(0, index - window)
        hi = min(len(values_m), index + window + 1)
        neighborhood = initial_mask[lo:hi]
        if np.count_nonzero(neighborhood) < 5:
            continue
        local_values = values_m[lo:hi][neighborhood]
        median = float(np.median(local_values))
        mad = float(np.median(np.abs(local_values - median)))
        threshold = max(n_sigma * 1.4826 * mad, floor_m)
        if abs(float(values_m[index]) - median) > threshold:
            keep[index] = False
    return keep


def interpolate_kept_values(x: np.ndarray, values: np.ndarray, keep: np.ndarray, fallback: np.ndarray) -> np.ndarray:
    if np.count_nonzero(keep) < 2:
        return fallback.copy()
    return np.interp(x, x[keep], values[keep])


def polynomial_design(x_units: np.ndarray, degree: int) -> tuple[np.ndarray, float, float]:
    center = float(np.mean(x_units))
    scale = float((np.max(x_units) - np.min(x_units)) / 2.0)
    if scale <= 0:
        scale = 1.0
    z = (x_units - center) / scale
    design = np.column_stack([z**power for power in range(degree + 1)])
    return design, center, scale


def robust_ridge_fit(
    x_units: np.ndarray,
    y_m: np.ndarray,
    base_weights: np.ndarray,
    degree: int = 6,
    alpha: float = 0.18,
    iterations: int = 6,
) -> np.ndarray:
    usable = base_weights > 0
    if np.count_nonzero(usable) < degree + 2:
        return y_m.copy()

    x_train = x_units[usable]
    y_train = y_m[usable]
    weights = base_weights[usable].astype(float)
    design_train, center, scale = polynomial_design(x_train, degree)
    z_all = (x_units - center) / scale
    design_all = np.column_stack([z_all**power for power in range(degree + 1)])
    beta = np.zeros(degree + 1, dtype=float)
    penalty = np.eye(degree + 1) * alpha
    penalty[0, 0] = 0.0

    for _ in range(iterations):
        sqrt_w = np.sqrt(np.maximum(weights, 1e-6))
        xw = design_train * sqrt_w[:, None]
        yw = y_train * sqrt_w
        beta = np.linalg.solve(xw.T @ xw + penalty, xw.T @ yw)
        residual = y_train - design_train @ beta
        mad = float(np.median(np.abs(residual - np.median(residual))))
        scale_resid = max(1.4826 * mad, 0.18)
        huber = np.minimum(1.0, (1.345 * scale_resid) / (np.abs(residual) + 1e-9))
        weights = np.maximum(base_weights[usable], 1e-6) * huber

    return design_all @ beta


def soft_threshold(value: float, threshold: float) -> float:
    if value > threshold:
        return value - threshold
    if value < -threshold:
        return value + threshold
    return 0.0


def elastic_net_fit(
    x_units: np.ndarray,
    y_m: np.ndarray,
    base_weights: np.ndarray,
    degree: int = 10,
    l1: float = 0.025,
    l2: float = 0.08,
    iterations: int = 900,
) -> np.ndarray:
    usable = base_weights > 0
    if np.count_nonzero(usable) < degree + 2:
        return y_m.copy()

    x_train = x_units[usable]
    y_train = y_m[usable]
    weights = base_weights[usable].astype(float)
    design_train, center, scale = polynomial_design(x_train, degree)
    z_all = (x_units - center) / scale
    design_all = np.column_stack([z_all**power for power in range(degree + 1)])

    means = np.zeros(degree + 1)
    stds = np.ones(degree + 1)
    for column in range(1, degree + 1):
        means[column] = float(np.average(design_train[:, column], weights=weights))
        variance = float(np.average((design_train[:, column] - means[column]) ** 2, weights=weights))
        stds[column] = math.sqrt(max(variance, 1e-9))
    design_train = (design_train - means) / stds
    design_train[:, 0] = 1.0
    design_all = (design_all - means) / stds
    design_all[:, 0] = 1.0

    sqrt_w = np.sqrt(np.maximum(weights, 1e-6))
    xw = design_train * sqrt_w[:, None]
    yw = y_train * sqrt_w
    beta = np.zeros(degree + 1, dtype=float)
    beta[0] = float(np.average(y_train, weights=weights))

    for _ in range(iterations):
        prediction = xw @ beta
        for column in range(degree + 1):
            residual = yw - prediction + xw[:, column] * beta[column]
            rho = float(np.dot(xw[:, column], residual))
            z = float(np.dot(xw[:, column], xw[:, column])) + (0.0 if column == 0 else l2)
            if column == 0:
                new_beta = rho / max(z, 1e-9)
            else:
                new_beta = soft_threshold(rho, l1) / max(z, 1e-9)
            prediction += xw[:, column] * (new_beta - beta[column])
            beta[column] = new_beta

    return design_all @ beta


def postprocess_distance_path(
    transects: list[dict[str, Any]],
    raw_path: np.ndarray,
    path_evidence: np.ndarray,
    distance_grid: np.ndarray,
    unit_to_meter: float,
    method: str,
    smoothing_m: float,
    corner_zone_m: float,
) -> np.ndarray:
    x_units = np.array([transect["distance_along_units"] for transect in transects], dtype=float)
    x_m = np.array([transect["distance_along_m"] for transect in transects], dtype=float)
    raw_m = np.array([units_to_meters(float(item), unit_to_meter) for item in raw_path], dtype=float)
    usable = signal_mask(path_evidence)
    evidence_weights = np.zeros_like(path_evidence, dtype=float)
    if np.any(usable):
        finite = path_evidence[usable]
        lo = float(np.percentile(finite, 10))
        hi = float(np.percentile(finite, 90))
        if hi <= lo:
            hi = lo + 1.0
        evidence_weights[usable] = np.clip((path_evidence[usable] - lo) / (hi - lo), 0.15, 1.0)

    if method == "spline":
        weights = np.maximum(path_evidence, 0.03)
        smoothing_units = meters_to_units(smoothing_m, unit_to_meter)
        spline = UnivariateSpline(x_units, raw_path, w=weights, s=len(transects) * smoothing_units**2)
        fitted_units = spline(x_units)
    elif method == "viterbi":
        fitted_units = raw_path.copy()
    elif method == "hampel":
        keep = hampel_keep_mask(raw_m, usable)
        fitted_m = interpolate_kept_values(x_m, raw_m, keep, raw_m)
        fitted_units = np.array([meters_to_units(float(item), unit_to_meter) for item in fitted_m])
    elif method == "tv":
        keep = hampel_keep_mask(raw_m, usable, window=7, n_sigma=3.5, floor_m=0.55)
        interpolated = interpolate_kept_values(x_m, raw_m, keep, raw_m)
        fitted_m = denoise_tv_chambolle(interpolated, weight=0.18)
        fitted_units = np.array([meters_to_units(float(item), unit_to_meter) for item in fitted_m])
    elif method == "ridge":
        keep = hampel_keep_mask(raw_m, usable, window=9, n_sigma=3.2, floor_m=0.55)
        weights = evidence_weights * keep
        fitted_m = robust_ridge_fit(x_units, raw_m, weights, degree=6, alpha=0.20)
        fitted_units = np.array([meters_to_units(float(item), unit_to_meter) for item in fitted_m])
    elif method == "elastic-net":
        keep = hampel_keep_mask(raw_m, usable, window=9, n_sigma=3.2, floor_m=0.60)
        weights = evidence_weights * keep
        fitted_m = elastic_net_fit(x_units, raw_m, weights, degree=10, l1=0.035, l2=0.11)
        fitted_units = np.array([meters_to_units(float(item), unit_to_meter) for item in fitted_m])
    elif method == "hybrid":
        keep = hampel_keep_mask(raw_m, usable, window=9, n_sigma=3.0, floor_m=0.48)
        corner = (x_m <= corner_zone_m) | (x_m >= float(np.max(x_m)) - corner_zone_m)
        corner_keep = hampel_keep_mask(raw_m, usable & corner, window=5, n_sigma=4.5, floor_m=0.75)
        keep = (keep & ~corner) | (corner_keep & corner)
        fitted_m = interpolate_kept_values(x_m, raw_m, keep, raw_m)
        if np.count_nonzero(corner_keep) >= 2:
            corner_fitted = interpolate_kept_values(x_m, raw_m, corner_keep, fitted_m)
            fitted_m[corner] = corner_fitted[corner]
        fitted_units = np.array([meters_to_units(float(item), unit_to_meter) for item in fitted_m])
    else:
        raise ValueError(f"Unknown postprocess method: {method}")

    return np.clip(fitted_units, float(distance_grid[0]), float(distance_grid[-1]))


def optimize_distance_path(
    transects: list[dict[str, Any]],
    unit_to_meter: float,
    min_search_m: float,
    max_search_m: float,
    grid_step_m: float,
    jump_penalty: float,
    smoothing_m: float,
    postprocess_method: str,
    corner_zone_m: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    distance_grid = np.arange(
        meters_to_units(min_search_m, unit_to_meter),
        meters_to_units(max_search_m, unit_to_meter) + meters_to_units(grid_step_m, unit_to_meter) * 0.5,
        meters_to_units(grid_step_m, unit_to_meter),
    )
    n = len(distance_grid)
    if n < 3:
        raise RuntimeError("Distance grid is too small")

    evidence = np.vstack([evidence_on_grid(transect, distance_grid, unit_to_meter) for transect in transects])
    transition = jump_penalty * (units_to_meters(distance_grid[:, None] - distance_grid[None, :], unit_to_meter) ** 2)

    costs = np.zeros((len(transects), n), dtype=float)
    back = np.zeros((len(transects), n), dtype=int)
    costs[0] = 0.2 - evidence[0]
    for i in range(1, len(transects)):
        candidate_costs = costs[i - 1][:, None] + transition
        back[i] = np.argmin(candidate_costs, axis=0)
        costs[i] = candidate_costs[back[i], np.arange(n)] + 0.2 - evidence[i]

    path_indexes = np.zeros(len(transects), dtype=int)
    path_indexes[-1] = int(np.argmin(costs[-1]))
    for i in range(len(transects) - 1, 0, -1):
        path_indexes[i - 1] = back[i, path_indexes[i]]
    raw_path = distance_grid[path_indexes]

    path_evidence = evidence[np.arange(len(transects)), path_indexes]
    fitted_path = postprocess_distance_path(
        transects,
        raw_path,
        path_evidence,
        distance_grid,
        unit_to_meter,
        postprocess_method,
        smoothing_m,
        corner_zone_m,
    )
    return fitted_path, raw_path, path_evidence, distance_grid


def nearest_candidates_to_path(
    transects: list[dict[str, Any]],
    fitted_distances: np.ndarray,
    unit_to_meter: float,
    max_distance_m: float,
) -> tuple[list[dict[str, Any] | None], np.ndarray, np.ndarray]:
    nearest: list[dict[str, Any] | None] = []
    residuals = np.full(len(transects), np.nan)
    accepted = np.zeros(len(transects), dtype=bool)
    max_distance = meters_to_units(max_distance_m, unit_to_meter)
    for index, (transect, fitted_distance) in enumerate(zip(transects, fitted_distances, strict=True)):
        candidates = transect["candidates"]
        if not candidates:
            nearest.append(None)
            continue
        distances = np.array([candidate["mid_distance_units"] for candidate in candidates], dtype=float)
        best_index = int(np.argmin(np.abs(distances - fitted_distance)))
        candidate = candidates[best_index]
        residual = float(candidate["mid_distance_units"] - fitted_distance)
        residuals[index] = residual
        accepted[index] = bool(
            candidate.get("accepted_by_signal", candidate.get("accepted_by_spectrum", False)) and abs(residual) <= max_distance
        )
        nearest.append(candidate)
    return nearest, accepted, residuals


def build_edge_line_from_fit(transects: list[dict[str, Any]], fitted_distances: np.ndarray) -> LineString:
    points = []
    for transect, distance in zip(transects, fitted_distances, strict=True):
        center = np.array(transect["center_xy"], dtype=float)
        normal = np.array(transect["normal_xy"], dtype=float)
        xy = center + normal * float(distance)
        points.append((float(xy[0]), float(xy[1])))
    return LineString(points).simplify(0.35, preserve_topology=False)


def fit_circle(points: np.ndarray) -> tuple[np.ndarray, float] | None:
    if len(points) < 3:
        return None
    x = points[:, 0]
    y = points[:, 1]
    design = np.column_stack([x, y, np.ones(len(points))])
    rhs = -(x**2 + y**2)
    try:
        a, b, c = np.linalg.lstsq(design, rhs, rcond=None)[0]
    except np.linalg.LinAlgError:
        return None
    center = np.array([-a / 2.0, -b / 2.0], dtype=float)
    radius_sq = float(np.sum(center**2) - c)
    if radius_sq <= 0:
        return None
    return center, math.sqrt(radius_sq)


def robust_circle_arc(
    points: list[list[float]],
    start: tuple[float, float],
    end: tuple[float, float],
    unit_to_meter: float,
    min_radius_m: float = 2.0,
    max_radius_m: float = 22.0,
) -> list[tuple[float, float]] | None:
    all_points = np.array([start, *points, end], dtype=float)
    if len(all_points) < 7:
        return None

    working = all_points.copy()
    for _ in range(4):
        fit = fit_circle(working)
        if fit is None:
            return None
        center, radius = fit
        residual = np.abs(np.linalg.norm(working - center, axis=1) - radius)
        threshold = max(meters_to_units(0.85, unit_to_meter), float(np.percentile(residual, 70)))
        keep = residual <= threshold
        if np.count_nonzero(keep) < 5 or np.all(keep):
            break
        working = working[keep]

    fit = fit_circle(working)
    if fit is None:
        return None
    center, radius = fit
    radius_m = units_to_meters(radius, unit_to_meter)
    if radius_m < min_radius_m or radius_m > max_radius_m:
        return None

    start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
    end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
    point_angles = np.array([math.atan2(point[1] - center[1], point[0] - center[0]) for point in np.array(points)])

    def ccw_span(a: float, b: float) -> float:
        return (b - a) % (2.0 * math.pi)

    def count_inside(a: float, span: float) -> int:
        shifted = (point_angles - a) % (2.0 * math.pi)
        return int(np.count_nonzero(shifted <= span + 1e-6))

    span_ccw = ccw_span(start_angle, end_angle)
    span_cw = ccw_span(end_angle, start_angle)
    count_ccw = count_inside(start_angle, span_ccw)
    count_cw = count_inside(end_angle, span_cw)
    use_ccw = (count_ccw, -span_ccw) >= (count_cw, -span_cw)
    if use_ccw:
        span = span_ccw
        angles = start_angle + np.linspace(0.0, span, 24)
    else:
        span = span_cw
        angles = start_angle - np.linspace(0.0, span, 24)
    if span > math.pi * 1.25:
        return None

    arc = [(float(center[0] + radius * math.cos(angle)), float(center[1] + radius * math.sin(angle))) for angle in angles]
    return arc


def corner_evidence_points(
    transects: list[dict[str, Any]],
    nearest: list[dict[str, Any] | None],
    accepted: np.ndarray,
    at_end: bool,
    corner_zone_m: float,
    min_distance_m: float,
) -> list[list[float]]:
    if not transects:
        return []
    max_s = max(float(transect["distance_along_m"]) for transect in transects)
    points: list[list[float]] = []
    for transect, candidate, keep in zip(transects, nearest, accepted, strict=True):
        if not keep or candidate is None:
            continue
        s = float(transect["distance_along_m"])
        in_zone = s >= max_s - corner_zone_m if at_end else s <= corner_zone_m
        if in_zone and float(candidate["mid_distance_m"]) >= min_distance_m:
            points.append(candidate["midpoint_xy"])
    return points


def build_curb_ring_coords(
    edge_lines: dict[str, LineString],
    transects_by_label: dict[str, list[dict[str, Any]]],
    nearest_by_label: dict[str, list[dict[str, Any] | None]],
    accepted_by_label: dict[str, np.ndarray],
    unit_to_meter: float,
    corner_model: str,
    corner_zone_m: float,
    corner_arc_min_distance_m: float,
) -> list[tuple[float, float]]:
    north = list(edge_lines["Loop N"].coords)
    south = list(edge_lines["Loop S"].coords)
    if corner_model != "arc":
        return north + list(reversed(south))

    north_start_south_start_points = (
        corner_evidence_points(
            transects_by_label["Loop N"],
            nearest_by_label["Loop N"],
            accepted_by_label["Loop N"],
            False,
            corner_zone_m,
            corner_arc_min_distance_m,
        )
        + corner_evidence_points(
            transects_by_label["Loop S"],
            nearest_by_label["Loop S"],
            accepted_by_label["Loop S"],
            False,
            corner_zone_m,
            corner_arc_min_distance_m,
        )
    )
    north_end_south_end_points = (
        corner_evidence_points(
            transects_by_label["Loop N"],
            nearest_by_label["Loop N"],
            accepted_by_label["Loop N"],
            True,
            corner_zone_m,
            corner_arc_min_distance_m,
        )
        + corner_evidence_points(
            transects_by_label["Loop S"],
            nearest_by_label["Loop S"],
            accepted_by_label["Loop S"],
            True,
            corner_zone_m,
            corner_arc_min_distance_m,
        )
    )
    end_arc = robust_circle_arc(north_end_south_end_points, north[-1], south[-1], unit_to_meter)
    start_arc = robust_circle_arc(north_start_south_start_points, south[0], north[0], unit_to_meter)
    if end_arc is None:
        end_arc = [north[-1], south[-1]]
    if start_arc is None:
        start_arc = [south[0], north[0]]

    return north + end_arc[1:] + list(reversed(south)) + start_arc[1:]


def geojson_feature(geometry: Any, properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "properties": properties, "geometry": mapping(geometry)}


def write_geojson(path: Path, features: list[dict[str, Any]]) -> None:
    write_json(path, {"type": "FeatureCollection", "features": features})


def line_parts(geometry: Any) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    return []


def plot_line(ax: Any, geometry: Any, *args: Any, **kwargs: Any) -> None:
    for line in line_parts(geometry):
        xs, ys = line.xy
        ax.plot(xs, ys, *args, **kwargs)


def plot_points(
    ax: Any,
    transects_by_label: dict[str, list[dict[str, Any]]],
    nearest_by_label: dict[str, list[dict[str, Any] | None]],
    accepted_by_label: dict[str, np.ndarray],
) -> None:
    for label, transects in transects_by_label.items():
        nearest = nearest_by_label[label]
        accepted = accepted_by_label[label]
        nearest_ids = {id(candidate) for candidate in nearest if candidate is not None}
        other_points: list[list[float]] = []
        accepted_points: list[list[float]] = []
        omitted_points: list[list[float]] = []

        for index, transect in enumerate(transects):
            selected = nearest[index]
            if selected is not None:
                if accepted[index]:
                    accepted_points.append(selected["midpoint_xy"])
                else:
                    omitted_points.append(selected["midpoint_xy"])
            for candidate in transect["candidates"]:
                if id(candidate) not in nearest_ids:
                    other_points.append(candidate["midpoint_xy"])

        if other_points:
            raw = np.array(other_points, dtype=float)
            ax.scatter(
                raw[:, 0],
                raw[:, 1],
                s=3,
                color="#f8f8f2",
                alpha=0.28,
                linewidths=0,
                label="alternate changepoint candidates",
            )
        if accepted_points:
            raw = np.array(accepted_points, dtype=float)
            ax.scatter(raw[:, 0], raw[:, 1], s=7, color="#30f2ff", alpha=0.82, label="accepted curb evidence")
        if omitted_points:
            raw = np.array(omitted_points, dtype=float)
            ax.scatter(
                raw[:, 0],
                raw[:, 1],
                s=11,
                marker="x",
                color="#ff6b3a",
                alpha=0.92,
                linewidths=0.9,
                label="omitted/gap evidence",
            )


def build_overlay(
    rgb: np.ndarray,
    bounds: Any,
    centerlines: list[LineString],
    curb_edge: LineString,
    running_line: Any,
    transects_by_label: dict[str, list[dict[str, Any]]],
    nearest_by_label: dict[str, list[dict[str, Any] | None]],
    accepted_by_label: dict[str, np.ndarray],
    output_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(10, 9), dpi=180)
    ax.imshow(rgb, extent=[bounds.left, bounds.right, bounds.bottom, bounds.top])
    for line in centerlines:
        xs, ys = line.xy
        ax.plot(xs, ys, color="#ffd84d", linewidth=1.1, linestyle="--", alpha=0.85, label="NYS Streets centerline")

    plot_points(ax, transects_by_label, nearest_by_label, accepted_by_label)
    plot_line(ax, curb_edge, color="#00d9ff", linewidth=2.3, label="curb candidate")
    plot_line(ax, running_line, color="#ff2fad", linewidth=2.0, label="2 ft offset candidate")

    ax.set_aspect("equal")
    ax.set_axis_off()
    handles, labels = ax.get_legend_handles_labels()
    unique = dict(zip(labels, handles, strict=False))
    ax.legend(unique.values(), unique.keys(), loc="lower right", framealpha=0.88, fontsize=8)
    fig.tight_layout(pad=0)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0)
    plt.close(fig)


def build_distance_plot(
    transects_by_label: dict[str, list[dict[str, Any]]],
    fitted_by_label: dict[str, np.ndarray],
    raw_path_by_label: dict[str, np.ndarray],
    path_evidence_by_label: dict[str, np.ndarray],
    nearest_by_label: dict[str, list[dict[str, Any] | None]],
    accepted_by_label: dict[str, np.ndarray],
    unit_to_meter: float,
    output_path: Path,
) -> None:
    labels = list(transects_by_label)
    fig, axes = plt.subplots(len(labels), 1, figsize=(11, 3.1 * len(labels)), dpi=170, sharex=False)
    if len(labels) == 1:
        axes = [axes]

    for ax, label in zip(axes, labels, strict=True):
        transects = transects_by_label[label]
        s = np.array([transect["distance_along_m"] for transect in transects], dtype=float)
        fitted_m = np.array([units_to_meters(float(item), unit_to_meter) for item in fitted_by_label[label]])
        raw_path_m = np.array([units_to_meters(float(item), unit_to_meter) for item in raw_path_by_label[label]])
        evidence = path_evidence_by_label[label]
        accepted_mask = accepted_by_label[label]
        nearest = nearest_by_label[label]

        ax.plot(s, raw_path_m, color="#a0a7b3", linewidth=1.0, alpha=0.55, label="chosen changepoint path")
        ax.plot(s, fitted_m, color="#008fb3", linewidth=2.0, label="final curb distance")

        accepted_s: list[float] = []
        accepted_d: list[float] = []
        omitted_s: list[float] = []
        omitted_d: list[float] = []
        alternate_s: list[float] = []
        alternate_d: list[float] = []
        nearest_ids = {id(candidate) for candidate in nearest if candidate is not None}
        for index, transect in enumerate(transects):
            selected = nearest[index]
            if selected is not None:
                if accepted_mask[index]:
                    accepted_s.append(float(s[index]))
                    accepted_d.append(float(selected["mid_distance_m"]))
                else:
                    omitted_s.append(float(s[index]))
                    omitted_d.append(float(selected["mid_distance_m"]))
            for candidate in transect["candidates"]:
                if id(candidate) not in nearest_ids:
                    alternate_s.append(float(s[index]))
                    alternate_d.append(float(candidate["mid_distance_m"]))

        if alternate_s:
            ax.scatter(alternate_s, alternate_d, s=8, color="#c2c7d0", alpha=0.25, linewidths=0, label="alternate changepoints")
        if accepted_s:
            ax.scatter(accepted_s, accepted_d, s=13, color="#00bcd4", alpha=0.85, linewidths=0, label="accepted changepoints")
        if omitted_s:
            ax.scatter(
                omitted_s,
                omitted_d,
                s=18,
                marker="x",
                color="#ff6b3a",
                alpha=0.9,
                linewidths=0.9,
                label="omitted/gap changepoints",
            )

        evidence_scaled = np.interp(evidence, (float(np.min(evidence)), float(np.max(evidence)) + 1e-9), (0.0, 1.0))
        ymin, ymax = ax.get_ylim()
        evidence_y = ymin + evidence_scaled * max((ymax - ymin) * 0.16, 0.1)
        ax.fill_between(s, ymin, evidence_y, color="#ffe08a", alpha=0.28, linewidth=0, label="relative evidence")
        ax.set_ylim(ymin, ymax)
        ax.set_title(label)
        ax.set_ylabel("centerline to curb (m)")
        ax.grid(True, color="#d8dde6", linewidth=0.6, alpha=0.7)
        handles, legend_labels = ax.get_legend_handles_labels()
        unique = dict(zip(legend_labels, handles, strict=False))
        ax.legend(unique.values(), unique.keys(), loc="upper right", fontsize=7, framealpha=0.9)

    axes[-1].set_xlabel("distance along NYS Streets centerline (m)")
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)


def lap_counts(length_m: float) -> dict[str, float]:
    milestones = {
        "five_k": 5000.0,
        "ten_k": 10000.0,
        "half_marathon": 21097.5,
        "marathon": 42195.0,
    }
    return {name: target / length_m for name, target in milestones.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--raster", type=Path, default=PROJECT_ROOT / "data" / "interim" / "ortho" / "example-2024-loop-tight.tif")
    parser.add_argument("--postprocess", choices=POSTPROCESS_METHODS, default="hybrid")
    parser.add_argument("--transect-direction", choices=TRANSECT_DIRECTIONS, default="multi-ray")
    parser.add_argument("--overlay", type=Path, default=None)
    parser.add_argument("--distance-plot", type=Path, default=None)
    parser.add_argument("--diagnostics", type=Path, default=None)
    parser.add_argument("--curb-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-candidate.geojson")
    parser.add_argument("--running-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "running-line-2ft-candidate.geojson")
    parser.add_argument("--centerline-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "loop-centerlines.geojson")
    parser.add_argument("--sample-spacing-m", type=float, default=0.75)
    parser.add_argument("--endpoint-margin-m", type=float, default=0.75)
    parser.add_argument("--min-search-m", type=float, default=2.4)
    parser.add_argument("--max-search-m", type=float, default=8.0)
    parser.add_argument("--transect-step-m", type=float, default=0.10)
    parser.add_argument("--grid-step-m", type=float, default=0.05)
    parser.add_argument("--jump-penalty", type=float, default=6.0)
    parser.add_argument("--smoothing-m", type=float, default=0.8)
    parser.add_argument("--corner-zone-m", type=float, default=28.0)
    parser.add_argument("--corner-model", choices=("none", "arc"), default=None)
    parser.add_argument("--corner-arc-min-distance-m", type=float, default=3.8)
    parser.add_argument("--max-residual-m", type=float, default=0.38)
    parser.add_argument("--multi-ray-grid-size", type=int, default=5)
    parser.add_argument("--multi-ray-max-rays", type=int, default=17)
    parser.add_argument("--multi-ray-cluster-step-m", type=float, default=0.22)
    parser.add_argument("--multi-ray-lateral-tolerance-m", type=float, default=1.25)
    parser.add_argument("--multi-ray-min-votes", type=int, default=2)
    args = parser.parse_args()
    if args.corner_model is None:
        args.corner_model = "arc" if args.postprocess == "hybrid" else "none"
    method_slug = args.postprocess.replace("_", "-")
    direction_slug = args.transect_direction.replace("_", "-")
    if args.overlay is None:
        args.overlay = PROJECT_ROOT / "build" / f"curb-{method_slug}-{direction_slug}-overlay.png"
    if args.distance_plot is None:
        args.distance_plot = PROJECT_ROOT / "build" / f"curb-{method_slug}-{direction_slug}-distance-diagnostics.png"
    if args.diagnostics is None:
        args.diagnostics = PROJECT_ROOT / "build" / f"curb-{method_slug}-{direction_slug}-diagnostics.json"

    config = load_yaml(args.config)
    features = query_loop_centerlines(config)
    label_order = {"Loop N": 0, "Loop S": 1}
    features = sorted(features, key=lambda feature: label_order.get(feature["attributes"]["Label"], 99))
    centerlines_4326 = [line_from_feature(feature) for feature in features]

    with rasterio.open(args.raster) as dataset:
        raster_crs = CRS.from_user_input(dataset.crs)
        unit_to_meter = unit_conversion_to_meters(raster_crs)
        raster = dataset.read().astype(float) / 255.0
        rgb = stretch_rgb(dataset.read())
        inverse_transform = ~dataset.transform
        bounds = dataset.bounds

    centerlines_native = [transform_geometry(line, "EPSG:4326", raster_crs) for line in centerlines_4326]
    site_center = config["site"]["center"]
    site_center_4326 = Point(float(site_center["longitude"]), float(site_center["latitude"]))
    site_center_native = transform_geometry(site_center_4326, "EPSG:4326", raster_crs)
    transect_target_xy = np.array([site_center_native.x, site_center_native.y], dtype=float)
    centerline_by_label = {
        feature["attributes"]["Label"]: line for feature, line in zip(features, centerlines_native, strict=True)
    }
    north_line = centerline_by_label["Loop N"]
    south_line = centerline_by_label["Loop S"]
    loop_polygon = Polygon(list(north_line.coords) + list(reversed(south_line.coords)))
    if not loop_polygon.is_valid:
        loop_polygon = loop_polygon.buffer(0)
    if loop_polygon.area <= 0:
        raise RuntimeError("Could not build valid centerline loop polygon")
    multi_ray_targets_xy = build_multi_ray_targets(loop_polygon, transect_target_xy, args.multi_ray_grid_size)

    transects_by_label: dict[str, list[dict[str, Any]]] = {}
    fitted_by_label: dict[str, np.ndarray] = {}
    raw_path_by_label: dict[str, np.ndarray] = {}
    path_evidence_by_label: dict[str, np.ndarray] = {}
    nearest_by_label: dict[str, list[dict[str, Any] | None]] = {}
    accepted_by_label: dict[str, np.ndarray] = {}
    residual_by_label: dict[str, np.ndarray] = {}
    edge_lines: dict[str, LineString] = {}

    for feature in features:
        label = feature["attributes"]["Label"]
        line = centerline_by_label[label]
        transects = sample_curb_candidates(
            line,
            loop_polygon,
            raster,
            inverse_transform,
            unit_to_meter,
            args.transect_direction,
            transect_target_xy,
            multi_ray_targets_xy,
            args.multi_ray_max_rays,
            args.multi_ray_cluster_step_m,
            args.multi_ray_lateral_tolerance_m,
            args.multi_ray_min_votes,
            args.sample_spacing_m,
            args.endpoint_margin_m,
            args.min_search_m,
            args.max_search_m,
            args.transect_step_m,
        )
        candidate_count = sum(len(transect["candidates"]) for transect in transects)
        if len(transects) < 6 or candidate_count < 6:
            raise RuntimeError(f"Not enough curb evidence for {label}: {candidate_count} candidates on {len(transects)} transects")

        fitted_distances, raw_path, path_evidence, _distance_grid = optimize_distance_path(
            transects,
            unit_to_meter,
            args.min_search_m,
            args.max_search_m,
            args.grid_step_m,
            args.jump_penalty,
            args.smoothing_m,
            args.postprocess,
            args.corner_zone_m,
        )
        nearest, accepted_mask, residual = nearest_candidates_to_path(
            transects,
            fitted_distances,
            unit_to_meter,
            args.max_residual_m,
        )
        transects_by_label[label] = transects
        fitted_by_label[label] = fitted_distances
        raw_path_by_label[label] = raw_path
        path_evidence_by_label[label] = path_evidence
        nearest_by_label[label] = nearest
        accepted_by_label[label] = accepted_mask
        residual_by_label[label] = residual
        edge_lines[label] = build_edge_line_from_fit(transects, fitted_distances)

    curb_ring_coords = build_curb_ring_coords(
        edge_lines,
        transects_by_label,
        nearest_by_label,
        accepted_by_label,
        unit_to_meter,
        args.corner_model,
        args.corner_zone_m,
        args.corner_arc_min_distance_m,
    )
    curb_polygon_native = Polygon(curb_ring_coords)
    if not curb_polygon_native.is_valid:
        curb_polygon_native = curb_polygon_native.buffer(0)
    curb_edge_native = LineString(list(curb_polygon_native.exterior.coords))

    measurement_crs = config["measurement"]["crs"]["preferred_measurement"]
    curb_polygon_measure = transform_geometry(curb_polygon_native, raster_crs, measurement_crs)
    offset_m = float(config["measurement"]["intended_offset"]["meters"])
    running_polygon_measure = curb_polygon_measure.buffer(-offset_m, join_style=1, resolution=24)
    running_line_measure = running_polygon_measure.boundary
    running_line_native = transform_geometry(running_line_measure, measurement_crs, raster_crs)

    curb_edge_4326 = transform_geometry(curb_edge_native, raster_crs, "EPSG:4326")
    running_line_4326 = transform_geometry(running_line_measure, measurement_crs, "EPSG:4326")
    centerlines_native_to_4326 = [transform_geometry(line, raster_crs, "EPSG:4326") for line in centerlines_native]

    length_m = float(running_line_measure.length)
    write_geojson(
        args.curb_geojson,
        [
            geojson_feature(
                curb_edge_4326,
                {
                    "name": "candidate granite curb / road edge",
                    "source_raster": str(args.raster),
                    "source_crs": str(raster_crs),
                    "geojson_crs": "EPSG:4326",
                    "detector": "multiband_changepoint_smooth_distance_path",
                    "postprocess": args.postprocess,
                    "transect_direction": args.transect_direction,
                    "corner_model": args.corner_model,
                    "review_status": "needs_review",
                },
            )
        ],
    )
    write_geojson(
        args.running_geojson,
        [
            geojson_feature(
                running_line_4326,
                {
                    "name": "candidate running line",
                    "offset_m": offset_m,
                    "length_m": length_m,
                    "measurement_crs": measurement_crs,
                    "detector": "multiband_changepoint_smooth_distance_path",
                    "postprocess": args.postprocess,
                    "transect_direction": args.transect_direction,
                    "corner_model": args.corner_model,
                    "review_status": "needs_review",
                },
            )
        ],
    )
    write_geojson(
        args.centerline_geojson,
        [
            geojson_feature(
                line,
                {
                    **feature["attributes"],
                    "name": feature["attributes"]["Label"],
                    "geojson_crs": "EPSG:4326",
                },
            )
            for feature, line in zip(features, centerlines_native_to_4326, strict=True)
        ],
    )

    diagnostics = {
        "candidate": True,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_raster": str(args.raster),
        "source_crs": str(raster_crs),
        "unit_to_meter": unit_to_meter,
        "measurement_crs": measurement_crs,
        "offset_m": offset_m,
        "lap_length_m": length_m,
        "lap_length_km": length_m / 1000.0,
        "lap_length_miles": length_m / 1609.344,
        "lap_length_feet": length_m * 3.280839895,
        "lap_counts": lap_counts(length_m),
        "algorithm": {
            "postprocess": args.postprocess,
            "transect_direction": args.transect_direction,
            "transect_target": {
                "longitude": float(site_center["longitude"]),
                "latitude": float(site_center["latitude"]),
            },
            "multi_ray_target_count": len(multi_ray_targets_xy),
            "multi_ray_grid_size": args.multi_ray_grid_size,
            "multi_ray_max_rays": args.multi_ray_max_rays,
            "multi_ray_cluster_step_m": args.multi_ray_cluster_step_m,
            "multi_ray_lateral_tolerance_m": args.multi_ray_lateral_tolerance_m,
            "multi_ray_min_votes": args.multi_ray_min_votes,
            "corner_model": args.corner_model,
            "sample_spacing_m": args.sample_spacing_m,
            "endpoint_margin_m": args.endpoint_margin_m,
            "min_search_m": args.min_search_m,
            "max_search_m": args.max_search_m,
            "transect_step_m": args.transect_step_m,
            "grid_step_m": args.grid_step_m,
            "jump_penalty": args.jump_penalty,
            "smoothing_m": args.smoothing_m,
            "corner_zone_m": args.corner_zone_m,
            "corner_arc_min_distance_m": args.corner_arc_min_distance_m,
            "max_residual_m": args.max_residual_m,
            "detector": "multiband transect changepoints plus a smooth centerline-to-curb distance path",
            "signal_target": "local transition between road-like and non-road-like transect neighborhoods",
        },
        "labels": {},
        "note": "Candidate only. Requires visual review of the overlay before use.",
    }

    def finite_or_none(value: float) -> float | None:
        return float(value) if np.isfinite(value) else None

    for label, transects in transects_by_label.items():
        accepted_mask = accepted_by_label[label]
        residual = residual_by_label[label]
        nearest = nearest_by_label[label]
        fitted_m = np.array([units_to_meters(float(item), unit_to_meter) for item in fitted_by_label[label]])
        raw_path_m = np.array([units_to_meters(float(item), unit_to_meter) for item in raw_path_by_label[label]])
        residual_m = np.array([units_to_meters(float(item), unit_to_meter) for item in residual])
        finite_residual_m = residual_m[np.isfinite(residual_m)]
        nearest_distances_m = np.array(
            [float(candidate["mid_distance_m"]) for candidate in nearest if candidate is not None],
            dtype=float,
        )
        candidate_count = sum(len(transect["candidates"]) for transect in transects)
        signal_candidate_count = sum(
            1 for transect in transects for candidate in transect["candidates"] if candidate["accepted_by_signal"]
        )
        diagnostics["labels"][label] = {
            "transect_count": len(transects),
            "candidate_count": candidate_count,
            "signal_candidate_count": signal_candidate_count,
            "accepted_count": int(np.count_nonzero(accepted_mask)),
            "omitted_or_gap_count": int(len(accepted_mask) - np.count_nonzero(accepted_mask)),
            "median_nearest_candidate_distance_m": finite_or_none(float(np.median(nearest_distances_m)))
            if nearest_distances_m.size
            else None,
            "median_raw_path_distance_m": finite_or_none(float(np.median(raw_path_m))),
            "median_fitted_distance_m": finite_or_none(float(np.median(fitted_m))),
            "median_abs_residual_m": finite_or_none(float(np.median(np.abs(finite_residual_m))))
            if finite_residual_m.size
            else None,
            "transects": [
                {
                    "distance_along_m": float(transect["distance_along_m"]),
                    "fitted_centerline_to_curb_m": float(fitted_m[index]),
                    "raw_path_centerline_to_curb_m": float(raw_path_m[index]),
                    "path_evidence": float(path_evidence_by_label[label][index]),
                    "accepted_after_smoothing": bool(accepted_mask[index]),
                    "residual_m": finite_or_none(float(residual_m[index])),
                    "nearest_candidate": nearest[index],
                    "candidate_count": len(transect["candidates"]),
                    "candidates": transect["candidates"],
                }
                for index, transect in enumerate(transects)
            ],
        }
    write_json(args.diagnostics, diagnostics)
    write_json(PROJECT_ROOT / "build" / "lap-distance-candidate.json", diagnostics)

    build_overlay(
        rgb,
        bounds,
        centerlines_native,
        curb_edge_native,
        running_line_native,
        transects_by_label,
        nearest_by_label,
        accepted_by_label,
        args.overlay,
    )
    build_distance_plot(
        transects_by_label,
        fitted_by_label,
        raw_path_by_label,
        path_evidence_by_label,
        nearest_by_label,
        accepted_by_label,
        unit_to_meter,
        args.distance_plot,
    )

    print(f"wrote {args.overlay}")
    print(f"wrote {args.distance_plot}")
    print(f"wrote {args.curb_geojson}")
    print(f"wrote {args.running_geojson}")
    print(f"candidate lap length: {length_m:.3f} m")
    for label in ["Loop N", "Loop S"]:
        accepted = accepted_by_label[label]
        print(f"{label}: accepted {int(np.count_nonzero(accepted))}/{len(accepted)} curb evidence points")


if __name__ == "__main__":
    main()
