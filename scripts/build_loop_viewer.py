#!/usr/bin/env python3
"""Build an interactive local viewer for curb-path review and annotation."""

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
from PIL import Image
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform

from build_fused_segmentation import (
    build_seed_masks,
    grid_lidar_to_ortho,
    load_centerlines,
    load_ortho,
    loop_polygon_from_centerlines,
    transform_geometry,
    unit_conversion_to_meters,
)
from refine_data_aware_boundary import (
    bilinear_sample,
    build_green_score,
    feature_stack_from_sources,
    normalize,
    signed_area,
)
from analyze_heading_motion import (
    local_window_summary,
    sample_at_station as sample_motion_at_station,
    trajectory_metrics,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
        handle.write("\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def selected_feature(
    path: Path,
    selector: dict[str, Any] | None = None,
    selected_ok: bool = True,
) -> tuple[Any, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    fallback: tuple[Any, dict[str, Any]] | None = None
    for feature in collection["features"]:
        props = dict(feature.get("properties", {}))
        geometry = shape(feature["geometry"])
        if selector and any(props.get(key) != value for key, value in selector.items()):
            continue
        if selected_ok and props.get("selected") is True:
            return geometry, props
        if fallback is None:
            fallback = (geometry, props)
    if fallback is not None:
        return fallback
    raise ValueError(f"no feature matched {selector or {}} in {path}")


def all_line_features(path: Path) -> list[tuple[Any, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    return [(shape(feature["geometry"]), dict(feature.get("properties", {}))) for feature in collection["features"]]


def units_to_meters(units: float, unit_to_meter: float) -> float:
    return units * unit_to_meter


def meters_to_units(meters: float, unit_to_meter: float) -> float:
    return meters / unit_to_meter


def geometry_to_segments_px(geometry: Any, src_crs: Any, dst_crs: Any, transform_affine: Any) -> list[list[list[float]]]:
    native = transform_geometry(geometry, src_crs, dst_crs)
    lines: list[LineString]
    if native.geom_type == "LineString":
        lines = [native]
    elif native.geom_type == "MultiLineString":
        lines = list(native.geoms)
    else:
        return []
    inverse_transform = ~transform_affine
    segments: list[list[list[float]]] = []
    for line in lines:
        coords = np.asarray(line.coords, dtype=float)
        cols, rows = inverse_transform * (coords[:, 0], coords[:, 1])
        segments.append([[round(float(col), 3), round(float(row), 3)] for col, row in zip(cols, rows)])
    return segments


def line_length_m(geometry: Any, src_crs: Any, measurement_crs: Any) -> float:
    return float(transform_geometry(geometry, src_crs, measurement_crs).length)


def rounded(value: float | int | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, digits)


def offset_label(properties: dict[str, Any]) -> str:
    offset_m = properties.get("offset_m")
    if offset_m is not None:
        offset_m = float(offset_m)
        if abs(offset_m - round(offset_m)) < 1e-9:
            return f"{round(offset_m):g} m"
    offset_feet = properties.get("offset_feet")
    if offset_feet is not None:
        return f"{float(offset_feet):g} ft"
    if offset_m is not None:
        return f"{offset_m:g} m"
    return "configured"


def transform_points(points: np.ndarray, transformer: Transformer) -> np.ndarray:
    x, y = transformer.transform(points[:, 0], points[:, 1])
    return np.column_stack([x, y])


def closed_tangents_normals(coords: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    tangent = np.roll(coords, -1, axis=0) - np.roll(coords, 1, axis=0)
    norm = np.linalg.norm(tangent, axis=1)
    tangent = tangent / np.maximum(norm[:, None], 1e-9)
    left_normal = np.column_stack([-tangent[:, 1], tangent[:, 0]])
    orientation = 1.0 if signed_area(coords) >= 0 else -1.0
    return tangent, left_normal * orientation


def resample_closed_line_measurement(line: LineString, spacing_m: float) -> tuple[np.ndarray, np.ndarray]:
    total_length = float(line.length)
    n_samples = max(64, int(round(total_length / spacing_m)))
    stations = np.linspace(0.0, total_length, n_samples, endpoint=False)
    coords = np.array([[line.interpolate(float(station)).x, line.interpolate(float(station)).y] for station in stations], dtype=float)
    return coords, stations


def sample_grid_from_measurement(
    grid: np.ndarray,
    coords_measurement: np.ndarray,
    measurement_to_ortho: Transformer,
    inverse_transform: Any,
) -> np.ndarray:
    coords_ortho = transform_points(coords_measurement, measurement_to_ortho)
    return bilinear_sample(grid, inverse_transform, coords_ortho)


def contrast_from_measurement(
    score: np.ndarray,
    coords_measurement: np.ndarray,
    normals_measurement: np.ndarray,
    probes_m: np.ndarray,
    measurement_to_ortho: Transformer,
    inverse_transform: Any,
) -> np.ndarray:
    inside_values: list[np.ndarray] = []
    outside_values: list[np.ndarray] = []
    for probe in probes_m:
        inside_values.append(
            sample_grid_from_measurement(score, coords_measurement + normals_measurement * probe, measurement_to_ortho, inverse_transform)
        )
        outside_values.append(
            sample_grid_from_measurement(score, coords_measurement - normals_measurement * probe, measurement_to_ortho, inverse_transform)
        )
    inside = np.nanmean(np.vstack(inside_values), axis=0)
    outside = np.nanmean(np.vstack(outside_values), axis=0)
    return inside - outside


def save_rgb(path: Path, rgb: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb.astype(np.uint8), mode="RGB").save(path)


def save_cmap(path: Path, values: np.ndarray, cmap_name: str, vmin: float, vmax: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmap = plt.get_cmap(cmap_name)
    scaled = np.clip((values - vmin) / max(vmax - vmin, 1e-9), 0.0, 1.0)
    rgba = cmap(np.nan_to_num(scaled, nan=0.5))
    rgba[..., 3] = np.where(np.isfinite(values), 1.0, 0.0)
    Image.fromarray((rgba * 255).astype(np.uint8), mode="RGBA").save(path)


def path_record(
    path_id: str,
    label: str,
    geometry: Any,
    src_crs: Any,
    ortho_crs: Any,
    measurement_crs: Any,
    transform_affine: Any,
    color: str,
    visible: bool,
    width: float,
    dashed: bool = False,
) -> dict[str, Any]:
    return {
        "id": path_id,
        "label": label,
        "color": color,
        "visible": visible,
        "width": width,
        "dashed": dashed,
        "lengthM": rounded(line_length_m(geometry, src_crs, measurement_crs), 3),
        "segments": geometry_to_segments_px(geometry, src_crs, ortho_crs, transform_affine),
    }


def build_samples(
    data_aware_4326: LineString,
    prior_4326: LineString,
    centerlines_4326: list[LineString],
    centerline_labels: list[str],
    fused_score: np.ndarray,
    image_score: np.ndarray,
    lidar_score: np.ndarray,
    lidar_layers: dict[str, np.ndarray],
    ortho_crs: CRS,
    measurement_crs: CRS,
    transform_affine: Any,
    spacing_m: float,
    probes_m: np.ndarray,
) -> list[dict[str, Any]]:
    data_line_measure = transform_geometry(data_aware_4326, "EPSG:4326", measurement_crs)
    prior_line_measure = transform_geometry(prior_4326, "EPSG:4326", measurement_crs)
    centerlines_measure = [transform_geometry(line, "EPSG:4326", measurement_crs) for line in centerlines_4326]
    coords_measure, stations = resample_closed_line_measurement(data_line_measure, spacing_m)
    _tangents, normals_measure = closed_tangents_normals(coords_measure)
    measurement_to_ortho = Transformer.from_crs(measurement_crs, ortho_crs, always_xy=True)
    measurement_to_4326 = Transformer.from_crs(measurement_crs, "EPSG:4326", always_xy=True)
    inverse_transform = ~transform_affine
    coords_ortho = transform_points(coords_measure, measurement_to_ortho)
    cols, rows = inverse_transform * (coords_ortho[:, 0], coords_ortho[:, 1])
    lon, lat = measurement_to_4326.transform(coords_measure[:, 0], coords_measure[:, 1])

    fused_at = sample_grid_from_measurement(fused_score, coords_measure, measurement_to_ortho, inverse_transform)
    image_at = sample_grid_from_measurement(image_score, coords_measure, measurement_to_ortho, inverse_transform)
    lidar_at = sample_grid_from_measurement(lidar_score, coords_measure, measurement_to_ortho, inverse_transform)
    fused_contrast = contrast_from_measurement(fused_score, coords_measure, normals_measure, probes_m, measurement_to_ortho, inverse_transform)
    image_contrast = contrast_from_measurement(image_score, coords_measure, normals_measure, probes_m, measurement_to_ortho, inverse_transform)
    lidar_contrast = contrast_from_measurement(lidar_score, coords_measure, normals_measure, probes_m, measurement_to_ortho, inverse_transform)
    elevation = sample_grid_from_measurement(lidar_layers["raw_ground_elevation"], coords_measure, measurement_to_ortho, inverse_transform)
    relief = sample_grid_from_measurement(lidar_layers["raw_local_relief"], coords_measure, measurement_to_ortho, inverse_transform)
    density = sample_grid_from_measurement(lidar_layers["raw_density"], coords_measure, measurement_to_ortho, inverse_transform)
    gradient = sample_grid_from_measurement(lidar_layers["elevation_gradient"], coords_measure, measurement_to_ortho, inverse_transform)
    intensity = sample_grid_from_measurement(lidar_layers["intensity"], coords_measure, measurement_to_ortho, inverse_transform)

    signed_prior_offset: list[float] = []
    centerline_distances: list[float] = []
    nearest_centerlines: list[str] = []
    for coord, normal in zip(coords_measure, normals_measure):
        point = Point(float(coord[0]), float(coord[1]))
        prior_point = prior_line_measure.interpolate(prior_line_measure.project(point))
        delta = np.array([point.x - prior_point.x, point.y - prior_point.y], dtype=float)
        signed_prior_offset.append(float(np.dot(delta, normal)))
        per_line = np.array([line.distance(point) for line in centerlines_measure], dtype=float)
        nearest_index = int(np.argmin(per_line))
        centerline_distances.append(float(per_line[nearest_index]))
        nearest_centerlines.append(centerline_labels[nearest_index])

    total = float(data_line_measure.length)
    samples: list[dict[str, Any]] = []
    for index, station in enumerate(stations):
        samples.append(
            {
                "index": index,
                "stationM": rounded(float(station), 3),
                "lapPct": rounded(100.0 * float(station) / total, 2),
                "px": [rounded(float(cols[index]), 3), rounded(float(rows[index]), 3)],
                "lon": rounded(float(lon[index]), 8),
                "lat": rounded(float(lat[index]), 8),
                "offsetFromPriorM": rounded(signed_prior_offset[index], 3),
                "centerlineDistanceM": rounded(centerline_distances[index], 3),
                "nearestCenterline": nearest_centerlines[index],
                "fusedScore": rounded(float(fused_at[index]), 3),
                "imageScore": rounded(float(image_at[index]), 3),
                "lidarScore": rounded(float(lidar_at[index]), 3),
                "fusedContrast": rounded(float(fused_contrast[index]), 3),
                "imageContrast": rounded(float(image_contrast[index]), 3),
                "lidarContrast": rounded(float(lidar_contrast[index]), 3),
                "groundElevationM": rounded(float(elevation[index]), 3),
                "localReliefM": rounded(float(relief[index]), 3),
                "elevationGradient": rounded(float(gradient[index]), 3),
                "lidarDensityPtsM2": rounded(float(density[index]), 2),
                "lidarIntensityNorm": rounded(float(intensity[index]), 3),
            }
        )
    return samples


def add_motion_metrics(samples: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    stations = metrics["stations"]
    heading_residual_deg = np.degrees(metrics["heading_residual_rad"])
    curvature_residual = metrics["curvature_residual_1_per_m"]
    course_score = metrics["course_correction_score"]
    for sample in samples:
        station = float(sample["stationM"])
        heading_window = local_window_summary(stations, heading_residual_deg, station, half_window_m=2.0)
        curvature_window = local_window_summary(stations, curvature_residual, station, half_window_m=2.0)
        sample["headingResidualDeg"] = rounded(sample_motion_at_station(stations, heading_residual_deg, station), 2)
        sample["headingWindowPeakToPeakDeg"] = rounded(heading_window["max"] - heading_window["min"], 2)
        sample["curvatureResidual1PerM"] = rounded(sample_motion_at_station(stations, curvature_residual, station), 4)
        sample["curvatureWindowPeakToPeak1PerM"] = rounded(curvature_window["max"] - curvature_window["min"], 4)
        sample["courseCorrectionScore"] = rounded(sample_motion_at_station(stations, course_score, station), 2)


def course_correction_peaks(
    samples: list[dict[str, Any]],
    metrics: dict[str, Any],
    max_peaks: int,
    min_separation_m: float,
    min_score: float,
) -> list[dict[str, Any]]:
    stations = metrics["stations"]
    scores = metrics["course_correction_score"]
    heading_residual_deg = np.degrees(metrics["heading_residual_rad"])
    curvature_residual = metrics["curvature_residual_1_per_m"]
    spacing = float(stations[1] - stations[0])
    lap_length = float(stations[-1] + spacing)
    local_maxima: list[int] = []
    for index in range(scores.size):
        prev_index = (index - 1) % scores.size
        next_index = (index + 1) % scores.size
        if scores[index] >= scores[prev_index] and scores[index] >= scores[next_index] and scores[index] >= min_score:
            local_maxima.append(index)
    local_maxima.sort(key=lambda item: scores[item], reverse=True)
    selected: list[int] = []
    for candidate in local_maxima:
        station = float(stations[candidate])
        separated = True
        for kept in selected:
            delta = abs(station - float(stations[kept]))
            if min(delta, lap_length - delta) < min_separation_m:
                separated = False
                break
        if separated:
            selected.append(candidate)
        if len(selected) >= max_peaks:
            break

    peaks: list[dict[str, Any]] = []
    for rank, index in enumerate(selected, start=1):
        station = float(stations[index])
        nearest = min(samples, key=lambda sample: abs(float(sample["stationM"]) - station))
        heading_window = local_window_summary(stations, heading_residual_deg, station, half_window_m=2.0)
        curvature_window = local_window_summary(stations, curvature_residual, station, half_window_m=2.0)
        peaks.append(
            {
                "rank": rank,
                "stationM": rounded(station, 3),
                "score": rounded(float(scores[index]), 2),
                "headingResidualDeg": rounded(float(heading_residual_deg[index]), 2),
                "headingWindowPeakToPeakDeg": rounded(heading_window["max"] - heading_window["min"], 2),
                "curvatureResidual1PerM": rounded(float(curvature_residual[index]), 4),
                "curvatureWindowPeakToPeak1PerM": rounded(curvature_window["max"] - curvature_window["min"], 4),
                "px": nearest["px"],
                "lon": nearest["lon"],
                "lat": nearest["lat"],
                "nearestSampleStationM": nearest["stationM"],
            }
        )
    return peaks


def html_text() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Example Loop Review</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <main class="shell">
    <section class="viewer">
      <div class="toolbar">
        <label class="control">Layer <select id="layerSelect"></select></label>
        <div id="overlayControls" class="overlay-controls"></div>
        <button id="resetView" type="button" title="Reset view">Reset</button>
      </div>
      <canvas id="mapCanvas"></canvas>
    </section>
    <aside class="panel">
      <h1>Loop Review</h1>
      <section>
        <h2>Hover</h2>
        <dl id="hoverFields" class="fields"></dl>
      </section>
      <section>
        <h2>Markers</h2>
        <div class="marker-actions">
          <button id="copyMarkers" type="button">Copy JSON</button>
          <button id="downloadMarkers" type="button">Download</button>
          <button id="clearMarkers" type="button">Clear</button>
        </div>
        <ol id="markerList" class="markers"></ol>
      </section>
      <section>
        <h2>Course Peaks</h2>
        <ol id="coursePeakList" class="markers"></ol>
      </section>
      <section>
        <h2>Lengths</h2>
        <dl id="lengthFields" class="fields compact"></dl>
      </section>
    </aside>
  </main>
  <script src="viewer-data.js"></script>
  <script src="viewer.js"></script>
</body>
</html>
"""


def css_text() -> str:
    return """:root {
  color-scheme: dark;
  --bg: #111418;
  --panel: #181d22;
  --panel-2: #20262c;
  --text: #eef2f5;
  --muted: #aab5bd;
  --line: #323b43;
  --accent: #28c4d8;
  --warn: #f0c64f;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  letter-spacing: 0;
}

button, select {
  color: var(--text);
  background: var(--panel-2);
  border: 1px solid var(--line);
  border-radius: 6px;
  font: inherit;
}

button {
  min-height: 32px;
  padding: 0 10px;
  cursor: pointer;
}

button:hover, select:hover { border-color: var(--accent); }

select {
  min-height: 32px;
  padding: 0 28px 0 8px;
}

.shell {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  height: 100vh;
  min-height: 0;
}

.viewer {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  min-width: 0;
  min-height: 0;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px;
  border-bottom: 1px solid var(--line);
  background: #151a1f;
  overflow-x: auto;
}

.control {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  white-space: nowrap;
}

.overlay-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}

.overlay-controls label {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--muted);
  padding: 4px 7px;
  border: 1px solid var(--line);
  border-radius: 6px;
}

input[type="checkbox"] { accent-color: var(--accent); }

#mapCanvas {
  width: 100%;
  height: 100%;
  display: block;
  cursor: crosshair;
  background: #080a0c;
}

.panel {
  min-width: 0;
  overflow: auto;
  border-left: 1px solid var(--line);
  background: var(--panel);
  padding: 14px;
}

h1 {
  margin: 0 0 14px;
  font-size: 18px;
  font-weight: 700;
}

h2 {
  margin: 18px 0 8px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}

.fields {
  display: grid;
  grid-template-columns: minmax(112px, auto) minmax(0, 1fr);
  gap: 6px 10px;
  margin: 0;
  font-size: 13px;
}

.fields dt { color: var(--muted); }
.fields dd { margin: 0; overflow-wrap: anywhere; }
.fields.compact { grid-template-columns: minmax(140px, auto) minmax(0, 1fr); }

.marker-actions {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-bottom: 8px;
}

.markers {
  margin: 0;
  padding-left: 24px;
  font-size: 13px;
}

.markers li {
  padding: 7px 0;
  border-top: 1px solid var(--line);
}

.marker-main {
  color: var(--warn);
  font-weight: 700;
}

.marker-sub {
  color: var(--muted);
  margin-top: 2px;
}

@media (max-width: 900px) {
  .shell {
    grid-template-columns: 1fr;
    grid-template-rows: minmax(0, 1fr) 42vh;
  }

  .panel {
    border-left: 0;
    border-top: 1px solid var(--line);
  }
}
"""


def js_text() -> str:
    return """const data = window.LOOP_VIEWER_DATA;

const canvas = document.getElementById("mapCanvas");
const ctx = canvas.getContext("2d");
const layerSelect = document.getElementById("layerSelect");
const overlayControls = document.getElementById("overlayControls");
const hoverFields = document.getElementById("hoverFields");
const lengthFields = document.getElementById("lengthFields");
const markerList = document.getElementById("markerList");
const coursePeakList = document.getElementById("coursePeakList");
const resetButton = document.getElementById("resetView");
const clearButton = document.getElementById("clearMarkers");
const copyButton = document.getElementById("copyMarkers");
const downloadButton = document.getElementById("downloadMarkers");

const state = {
  scale: 1,
  tx: 0,
  ty: 0,
  activeLayer: data.layers[0].id,
  overlays: Object.fromEntries(data.paths.map((path) => [path.id, path.visible])),
  showCoursePeaks: true,
  hover: null,
  markers: [],
  dragging: false,
  last: null
};

const images = {};
let loaded = 0;

for (const layer of data.layers) {
  const option = document.createElement("option");
  option.value = layer.id;
  option.textContent = layer.label;
  layerSelect.appendChild(option);

  const image = new Image();
  image.onload = () => {
    loaded += 1;
    if (loaded === data.layers.length) {
      fitView();
      draw();
    }
  };
  image.src = layer.file;
  images[layer.id] = image;
}

for (const path of data.paths) {
  const label = document.createElement("label");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = path.visible;
  checkbox.addEventListener("change", () => {
    state.overlays[path.id] = checkbox.checked;
    draw();
  });
  const swatch = document.createElement("span");
  swatch.textContent = path.label;
  swatch.style.color = path.color;
  label.append(checkbox, swatch);
  overlayControls.appendChild(label);
}

if (data.courseCorrectionPeaks?.length) {
  const label = document.createElement("label");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = state.showCoursePeaks;
  checkbox.addEventListener("change", () => {
    state.showCoursePeaks = checkbox.checked;
    draw();
  });
  const swatch = document.createElement("span");
  swatch.textContent = "Course peaks";
  swatch.style.color = "#ffea61";
  label.append(checkbox, swatch);
  overlayControls.appendChild(label);
}

layerSelect.addEventListener("change", () => {
  state.activeLayer = layerSelect.value;
  draw();
});

resetButton.addEventListener("click", () => {
  fitView();
  draw();
});

clearButton.addEventListener("click", () => {
  state.markers = [];
  renderMarkers();
  draw();
});

copyButton.addEventListener("click", async () => {
  const text = JSON.stringify(state.markers, null, 2);
  await navigator.clipboard.writeText(text);
});

downloadButton.addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(state.markers, null, 2) + "\\n"], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "loop-review-markers.json";
  link.click();
  URL.revokeObjectURL(url);
});

function fitView() {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  const sx = canvas.width / data.image.width;
  const sy = canvas.height / data.image.height;
  state.scale = Math.min(sx, sy) * 0.96;
  state.tx = (canvas.width - data.image.width * state.scale) / 2;
  state.ty = (canvas.height - data.image.height * state.scale) / 2;
}

function resizeCanvasKeepView() {
  const oldWidth = canvas.width || 1;
  const oldHeight = canvas.height || 1;
  const oldCenter = screenToWorld(oldWidth / 2, oldHeight / 2);
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  state.tx = canvas.width / 2 - oldCenter.x * state.scale;
  state.ty = canvas.height / 2 - oldCenter.y * state.scale;
  draw();
}

window.addEventListener("resize", resizeCanvasKeepView);

function worldToScreen(x, y) {
  return {x: x * state.scale + state.tx, y: y * state.scale + state.ty};
}

function screenToWorld(x, y) {
  return {x: (x - state.tx) / state.scale, y: (y - state.ty) / state.scale};
}

function eventPoint(event) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  return {
    x: (event.clientX - rect.left) * ratio,
    y: (event.clientY - rect.top) * ratio
  };
}

canvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  const point = eventPoint(event);
  const world = screenToWorld(point.x, point.y);
  const factor = Math.exp(-event.deltaY * 0.001);
  state.scale = Math.max(0.2, Math.min(20, state.scale * factor));
  state.tx = point.x - world.x * state.scale;
  state.ty = point.y - world.y * state.scale;
  draw();
}, {passive: false});

canvas.addEventListener("pointerdown", (event) => {
  state.dragging = true;
  state.last = eventPoint(event);
  canvas.setPointerCapture(event.pointerId);
});

canvas.addEventListener("pointerup", (event) => {
  const now = eventPoint(event);
  const hover = nearestSample(now.x, now.y);
  if (hover) state.hover = hover;
  if (state.hover && state.last) {
    const moved = Math.hypot(now.x - state.last.x, now.y - state.last.y);
    if (moved < 4) {
      state.markers.push({...state.hover, createdAt: new Date().toISOString()});
      renderMarkers();
      draw();
    }
  }
  state.dragging = false;
  state.last = null;
});

canvas.addEventListener("pointermove", (event) => {
  const point = eventPoint(event);
  if (state.dragging && state.last) {
    const dx = point.x - state.last.x;
    const dy = point.y - state.last.y;
    if (Math.hypot(dx, dy) > 2) {
      state.tx += dx;
      state.ty += dy;
      state.last = point;
      draw();
      return;
    }
  }
  state.hover = nearestSample(point.x, point.y);
  renderHover();
  draw();
});

canvas.addEventListener("pointerleave", () => {
  state.hover = null;
  renderHover();
  draw();
});

function nearestSample(screenX, screenY) {
  let best = null;
  let bestDist = Infinity;
  for (const sample of data.samples) {
    const screen = worldToScreen(sample.px[0], sample.px[1]);
    const dist = Math.hypot(screen.x - screenX, screen.y - screenY);
    if (dist < bestDist) {
      bestDist = dist;
      best = sample;
    }
  }
  const threshold = Math.max(10, Math.min(22, 12 + state.scale * 0.5));
  return bestDist <= threshold ? {...best, screenDistancePx: Number(bestDist.toFixed(1))} : null;
}

function nearestSampleByStation(stationM) {
  let best = null;
  let bestDist = Infinity;
  for (const sample of data.samples) {
    const dist = Math.abs(sample.stationM - stationM);
    if (dist < bestDist) {
      bestDist = dist;
      best = sample;
    }
  }
  return best;
}

function focusPeak(peak) {
  const sample = nearestSampleByStation(peak.stationM);
  if (!sample) return;
  state.hover = {...sample, screenDistancePx: 0};
  state.tx = canvas.width / 2 - peak.px[0] * state.scale;
  state.ty = canvas.height / 2 - peak.px[1] * state.scale;
  renderHover();
  draw();
}

function draw() {
  ctx.save();
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const image = images[state.activeLayer];
  if (image && image.complete) {
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(image, state.tx, state.ty, data.image.width * state.scale, data.image.height * state.scale);
  }
  ctx.translate(state.tx, state.ty);
  ctx.scale(state.scale, state.scale);

  for (const path of data.paths) {
    if (!state.overlays[path.id]) continue;
    ctx.save();
    ctx.strokeStyle = path.color;
    ctx.lineWidth = path.width / state.scale;
    ctx.globalAlpha = path.opacity || 1;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    if (path.dashed) ctx.setLineDash([8 / state.scale, 6 / state.scale]);
    for (const segment of path.segments) {
      if (!segment.length) continue;
      ctx.beginPath();
      ctx.moveTo(segment[0][0], segment[0][1]);
      for (let i = 1; i < segment.length; i += 1) {
        ctx.lineTo(segment[i][0], segment[i][1]);
      }
      ctx.stroke();
    }
    ctx.restore();
  }

  if (state.showCoursePeaks && data.courseCorrectionPeaks?.length) {
    for (const peak of data.courseCorrectionPeaks) {
      drawPeakLabel(peak);
    }
  }

  for (const marker of state.markers) {
    drawPoint(marker.px[0], marker.px[1], "#f0c64f", 6);
  }
  if (state.hover) {
    drawPoint(state.hover.px[0], state.hover.px[1], "#ffffff", 7);
  }
  ctx.restore();
}

function drawPoint(x, y, color, radiusScreen) {
  ctx.save();
  ctx.fillStyle = color;
  ctx.strokeStyle = "#111418";
  ctx.lineWidth = 2 / state.scale;
  ctx.beginPath();
  ctx.arc(x, y, radiusScreen / state.scale, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

function drawPeakLabel(peak) {
  const x = peak.px[0];
  const y = peak.px[1];
  const radius = 6 / state.scale;
  const label = `${peak.rank}: ${peak.stationM}m`;
  ctx.save();
  ctx.fillStyle = "#ffea61";
  ctx.strokeStyle = "#111418";
  ctx.lineWidth = 2 / state.scale;
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();

  ctx.font = `${12 / state.scale}px ui-sans-serif, system-ui, sans-serif`;
  ctx.textBaseline = "middle";
  const paddingX = 5 / state.scale;
  const paddingY = 3 / state.scale;
  const textWidth = ctx.measureText(label).width;
  const boxX = x + 8 / state.scale;
  const boxY = y - 10 / state.scale;
  const boxW = textWidth + paddingX * 2;
  const boxH = 18 / state.scale;
  ctx.fillStyle = "rgba(17, 20, 24, 0.84)";
  ctx.strokeStyle = "rgba(255, 234, 97, 0.9)";
  ctx.lineWidth = 1 / state.scale;
  ctx.beginPath();
  ctx.roundRect(boxX, boxY, boxW, boxH, 4 / state.scale);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = "#ffea61";
  ctx.fillText(label, boxX + paddingX, boxY + boxH / 2 + paddingY * 0.15);
  ctx.restore();
}

function renderFields(target, rows) {
  target.replaceChildren();
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value ?? "-";
    target.append(dt, dd);
  }
}

function fmt(value, suffix = "") {
  return value === null || value === undefined ? "-" : `${value}${suffix}`;
}

function renderHover() {
  const s = state.hover;
  if (!s) {
    renderFields(hoverFields, [["Station", "-"], ["Lat/lon", "-"], ["Evidence", "-"]]);
    return;
  }
    renderFields(hoverFields, [
    ["Station", `${s.stationM} m (${s.lapPct}%)`],
    ["Lat/lon", `${s.lat}, ${s.lon}`],
    ["Prior offset", fmt(s.offsetFromPriorM, " m")],
    ["Centerline dist.", fmt(s.centerlineDistanceM, " m")],
    ["Nearest centerline", fmt(s.nearestCenterline)],
    ["Heading residual", fmt(s.headingResidualDeg, " deg")],
    ["Heading p-p", fmt(s.headingWindowPeakToPeakDeg, " deg")],
    ["Curvature residual", fmt(s.curvatureResidual1PerM)],
    ["Course score", fmt(s.courseCorrectionScore)],
    ["Fused contrast", fmt(s.fusedContrast)],
    ["Imagery contrast", fmt(s.imageContrast)],
    ["LiDAR contrast", fmt(s.lidarContrast)],
    ["Fused score", fmt(s.fusedScore)],
    ["Imagery score", fmt(s.imageScore)],
    ["LiDAR score", fmt(s.lidarScore)],
    ["Ground elev.", fmt(s.groundElevationM, " m")],
    ["Local relief", fmt(s.localReliefM, " m")],
    ["Elev. gradient", fmt(s.elevationGradient)],
    ["LiDAR density", fmt(s.lidarDensityPtsM2, " pts/m2")],
    ["LiDAR intensity", fmt(s.lidarIntensityNorm)]
  ]);
}

function renderMarkers() {
  markerList.replaceChildren();
  for (const marker of state.markers) {
    const item = document.createElement("li");
    const main = document.createElement("div");
    main.className = "marker-main";
    main.textContent = `${marker.stationM} m (${marker.lapPct}%)`;
    const sub = document.createElement("div");
    sub.className = "marker-sub";
    sub.textContent = `image ${marker.imageContrast}, lidar ${marker.lidarContrast}, relief ${marker.localReliefM} m`;
    item.append(main, sub);
    markerList.appendChild(item);
  }
}

function renderCoursePeaks() {
  coursePeakList.replaceChildren();
  for (const peak of data.courseCorrectionPeaks || []) {
    const item = document.createElement("li");
    const main = document.createElement("div");
    main.className = "marker-main";
    main.textContent = `${peak.rank}. ${peak.stationM} m`;
    const sub = document.createElement("div");
    sub.className = "marker-sub";
    sub.textContent = `score ${peak.score}, heading p-p ${peak.headingWindowPeakToPeakDeg} deg`;
    item.style.cursor = "pointer";
    item.title = "Center this course-correction peak";
    item.addEventListener("click", () => focusPeak(peak));
    item.append(main, sub);
    coursePeakList.appendChild(item);
  }
}

renderFields(lengthFields, data.paths
  .filter((path) => path.lengthM)
  .map((path) => [path.label, `${path.lengthM} m`]));
renderHover();
renderMarkers();
renderCoursePeaks();
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--ortho", type=Path, default=PROJECT_ROOT / "data" / "interim" / "ortho" / "example-2024-loop-tight.tif")
    parser.add_argument("--lidar-npz", type=Path, default=PROJECT_ROOT / "data" / "interim" / "lidar" / "fema-2019-las-loop-tight.npz")
    parser.add_argument("--centerline-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "loop-centerlines.geojson")
    parser.add_argument("--data-aware-curb", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-data-aware-candidate.geojson")
    parser.add_argument("--data-aware-running", type=Path, default=PROJECT_ROOT / "geometry" / "running-line-2ft-data-aware-candidate.geojson")
    parser.add_argument("--visual-curb", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-visual-candidate.geojson")
    parser.add_argument("--visual-running", type=Path, default=PROJECT_ROOT / "geometry" / "running-line-1m-visual-candidate.geojson")
    parser.add_argument("--piecewise-curb", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-piecewise-curvature-candidates.geojson")
    parser.add_argument("--fused-curb", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-fused-segmentation-candidates.geojson")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "build" / "viewer")
    parser.add_argument("--sample-spacing-m", type=float, default=0.45)
    parser.add_argument("--probe-distances-m", nargs="+", type=float, default=[0.45, 0.75, 1.05, 1.35])
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
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

    lidar_layers = grid_lidar_to_ortho(args.lidar_npz, output_shape, transform_affine, ortho_crs, "EPSG:6347", resolution_m)
    feature_stack, feature_names = feature_stack_from_sources(ortho_array, lidar_layers)
    fused_score, fused_diag = build_green_score(feature_stack, road_mask, green_mask, feature_names)
    image_score, image_diag = build_green_score(feature_stack[:, :, :9], road_mask, green_mask, feature_names[:9])
    lidar_score, lidar_diag = build_green_score(feature_stack[:, :, 9:], road_mask, green_mask, feature_names[9:])

    output_dir = args.output_dir
    save_rgb(output_dir / "ortho.png", rgb)
    save_cmap(output_dir / "score-fused.png", fused_score, "PiYG", -1.5, 1.5)
    save_cmap(output_dir / "score-imagery.png", image_score, "PiYG", -1.5, 1.5)
    save_cmap(output_dir / "score-lidar.png", lidar_score, "PiYG", -1.5, 1.5)
    save_cmap(output_dir / "lidar-relief.png", lidar_layers["raw_local_relief"], "RdBu_r", -0.25, 0.25)
    save_cmap(output_dir / "lidar-gradient.png", lidar_layers["elevation_gradient"], "magma", 0.0, 1.0)
    save_cmap(output_dir / "lidar-density.png", normalize(lidar_layers["raw_density"]), "viridis", 0.0, 1.0)

    data_aware_curb, data_aware_props = selected_feature(args.data_aware_curb, selected_ok=False)
    data_aware_running, _running_props = selected_feature(args.data_aware_running, selected_ok=False)
    visual_curb: Any | None = None
    visual_running: Any | None = None
    visual_running_props: dict[str, Any] = {}
    if args.visual_curb.exists():
        visual_curb, _visual_props = selected_feature(args.visual_curb, selected_ok=False)
    if args.visual_running.exists():
        visual_running, visual_running_props = selected_feature(args.visual_running, selected_ok=False)
    piecewise_curb, _piecewise_props = selected_feature(args.piecewise_curb)
    fused_curb, _fused_props = selected_feature(args.fused_curb, selector={"method": "fused"}, selected_ok=False)

    review_curb = visual_curb if visual_curb is not None else data_aware_curb
    review_prior = data_aware_curb if visual_curb is not None else piecewise_curb

    paths = []
    if visual_curb is not None:
        paths.append(path_record("visual-curb", "Production visual curb", visual_curb, "EPSG:4326", ortho_crs, measurement_crs, transform_affine, "#00e676", True, 3.0))
    if visual_running is not None:
        paths.append(path_record("visual-running-line", f"{offset_label(visual_running_props)} visual running line", visual_running, "EPSG:4326", ortho_crs, measurement_crs, transform_affine, "#ffd84d", True, 2.2))
    else:
        paths.append(path_record("running-line", "Configured running line", data_aware_running, "EPSG:4326", ortho_crs, measurement_crs, transform_affine, "#f0c64f", True, 2.4))
    paths.extend(
        [
            path_record("data-aware-curb", "Data-aware prior curb", data_aware_curb, "EPSG:4326", ortho_crs, measurement_crs, transform_affine, "#00d9ff", visual_curb is None, 2.4),
            path_record("piecewise-prior", "Piecewise prior", piecewise_curb, "EPSG:4326", ortho_crs, measurement_crs, transform_affine, "#ff5f99", False, 1.8, dashed=True),
            path_record("fused-raw", "Fused raw", fused_curb, "EPSG:4326", ortho_crs, measurement_crs, transform_affine, "#67d967", False, 1.6, dashed=True),
            path_record("data-aware-running-line", "2 ft data-aware line", data_aware_running, "EPSG:4326", ortho_crs, measurement_crs, transform_affine, "#f0a000", False, 1.8),
        ]
    )
    for index, line in enumerate(centerlines_4326, start=1):
        paths.append(
            path_record(
                f"centerline-{index}",
                f"Centerline {index}",
                line,
                "EPSG:4326",
                ortho_crs,
                measurement_crs,
                transform_affine,
                "#ffffff",
                False,
                1.3,
                dashed=True,
            )
        )

    samples = build_samples(
        review_curb,
        review_prior,
        centerlines_4326,
        ["Loop N", "Loop S"],
        fused_score,
        image_score,
        lidar_score,
        lidar_layers,
        ortho_crs,
        measurement_crs,
        transform_affine,
        args.sample_spacing_m,
        np.asarray(args.probe_distances_m, dtype=float),
    )
    motion_metrics = trajectory_metrics(
        review_curb,
        measurement_crs,
        sample_spacing_m=0.1,
        coordinate_smooth_m=0.45,
        trend_wavelength_m=18.0,
        energy_window_m=4.0,
    )
    add_motion_metrics(samples, motion_metrics)
    peaks = course_correction_peaks(
        samples,
        motion_metrics,
        max_peaks=20,
        min_separation_m=3.0,
        min_score=5.0,
    )

    viewer_data = {
        "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "image": {"width": int(rgb.shape[1]), "height": int(rgb.shape[0]), "resolutionM": round(float(resolution_m), 4)},
        "layers": [
            {"id": "ortho", "label": "2024 ortho", "file": "ortho.png"},
            {"id": "score-fused", "label": "Fused score", "file": "score-fused.png"},
            {"id": "score-imagery", "label": "Imagery score", "file": "score-imagery.png"},
            {"id": "score-lidar", "label": "LiDAR score", "file": "score-lidar.png"},
            {"id": "lidar-relief", "label": "LiDAR relief", "file": "lidar-relief.png"},
            {"id": "lidar-gradient", "label": "LiDAR gradient", "file": "lidar-gradient.png"},
            {"id": "lidar-density", "label": "LiDAR density", "file": "lidar-density.png"},
        ],
        "paths": paths,
        "samples": samples,
        "courseCorrectionPeaks": peaks,
        "diagnostics": {
            "ortho": str(args.ortho),
            "lidarNpz": str(args.lidar_npz),
            "resolutionM": round(float(resolution_m), 4),
            "sampleSpacingM": float(args.sample_spacing_m),
            "probeDistancesM": [float(value) for value in args.probe_distances_m],
            "motionMetric": {
                "sampleSpacingM": 0.1,
                "coordinateSmoothM": 0.45,
                "trendWavelengthM": 18.0,
                "energyWindowM": 4.0,
                "peakMinSeparationM": 3.0,
                "peakMinScore": 5.0,
                "peakCount": len(peaks),
            },
            "seedMetadata": seed_metadata,
            "reviewCurb": "visual" if visual_curb is not None else "data-aware",
            "dataAwareProperties": data_aware_props,
            "fusedScoreMedian": fused_diag["score_seed_median"],
            "imageScoreMedian": image_diag["score_seed_median"],
            "lidarScoreMedian": lidar_diag["score_seed_median"],
            "fusedFeatureWeights": dict(zip(feature_names, fused_diag["feature_weights"], strict=True)),
        },
    }

    write_json(output_dir / "viewer-data.json", viewer_data)
    write_text(output_dir / "viewer-data.js", "window.LOOP_VIEWER_DATA = " + json.dumps(viewer_data, separators=(",", ":")) + ";\n")
    write_text(output_dir / "index.html", html_text())
    write_text(output_dir / "styles.css", css_text())
    write_text(output_dir / "viewer.js", js_text())

    print(f"wrote {output_dir / 'index.html'}")
    print(f"wrote {output_dir / 'viewer-data.json'}")
    print(f"samples: {len(samples)}")
    print(f"review curb: {'visual' if visual_curb is not None else 'data-aware'}")
    print(f"review curb length: {line_length_m(review_curb, 'EPSG:4326', measurement_crs):.3f} m")
    running_path = next((path for path in paths if "running" in path["id"] and path["visible"]), None)
    if running_path is not None:
        print(f"{running_path['label']} length: {running_path['lengthM']} m")


if __name__ == "__main__":
    main()
