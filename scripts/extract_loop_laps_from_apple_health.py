#!/usr/bin/env python3
"""Estimate completed Loop laps from Apple Health GPX workout routes."""

from __future__ import annotations

import argparse
import csv
import json
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPORT_DIR = PROJECT_ROOT / "data" / "raw" / "apple_health" / "export" / "apple_health_export"
DEFAULT_CANDIDATES = PROJECT_ROOT / "build" / "apple-health" / "apple-health-loop-candidates.json"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "site.yaml"
DEFAULT_LOOP_GEOJSON = PROJECT_ROOT / "geometry" / "running-line-1m-visual-candidate.geojson"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "build" / "apple-health"
GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def parse_gpx_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def local_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def zoned_iso(value: datetime | None, timezone_name: str | None) -> str | None:
    if value is None:
        return None
    if not timezone_name:
        return value.isoformat()
    try:
        return value.astimezone(ZoneInfo(timezone_name)).isoformat()
    except ZoneInfoNotFoundError:
        return value.isoformat()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def transform_geometry(geometry: Any, src_crs: Any, dst_crs: Any) -> Any:
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return transform(transformer.transform, geometry)


def load_loop(path: Path, measurement_crs: CRS) -> tuple[LineString, float]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    feature = collection["features"][0]
    line_4326 = shape(feature["geometry"])
    line_measure = transform_geometry(line_4326, "EPSG:4326", measurement_crs)
    return line_measure, float(feature.get("properties", {}).get("length_m", line_measure.length))


def circular_delta(current: float, previous: float, period: float) -> float:
    return ((current - previous + 0.5 * period) % period) - 0.5 * period


def interpolate_time_at_unwrapped_station(times: list[datetime], unwrapped: list[float], target: float, direction: int) -> datetime | None:
    oriented = [direction * value for value in unwrapped]
    oriented_target = direction * target
    for index in range(1, len(oriented)):
        before = oriented[index - 1]
        after = oriented[index]
        if before <= oriented_target <= after or after <= oriented_target <= before:
            span = after - before
            if abs(span) < 1e-9:
                return times[index]
            fraction = (oriented_target - before) / span
            delta = times[index] - times[index - 1]
            return times[index - 1] + fraction * delta
    return None


def median(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    mid = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[mid]
    return 0.5 * (sorted_values[mid - 1] + sorted_values[mid])


def parse_gpx_points(path: Path, measurement_crs: CRS) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot()
    transformer = Transformer.from_crs("EPSG:4326", measurement_crs, always_xy=True)
    rows: list[dict[str, Any]] = []
    for trkpt in root.findall(".//gpx:trkpt", GPX_NS):
        lon = float(trkpt.attrib["lon"])
        lat = float(trkpt.attrib["lat"])
        x, y = transformer.transform(lon, lat)
        time_node = trkpt.find("gpx:time", GPX_NS)
        time = parse_gpx_datetime(time_node.text if time_node is not None else None)
        speed = None
        hacc = None
        for child in trkpt:
            if child.tag.rsplit("}", 1)[-1] != "extensions":
                continue
            for ext in child:
                name = ext.tag.rsplit("}", 1)[-1]
                if ext.text is None:
                    continue
                if name == "speed":
                    speed = float(ext.text)
                elif name == "hAcc":
                    hacc = float(ext.text)
        if time is not None:
            rows.append({"lon": lon, "lat": lat, "x": x, "y": y, "time": time, "speed": speed, "hacc": hacc})
    return rows


def near_segments(
    points: list[dict[str, Any]],
    near_flags: list[bool],
    max_gap_points: int,
    max_gap_seconds: float,
) -> list[tuple[int, int]]:
    near_indexes = [index for index, flag in enumerate(near_flags) if flag]
    if not near_indexes:
        return []
    segments: list[tuple[int, int]] = []
    start = near_indexes[0]
    previous = near_indexes[0]
    for index in near_indexes[1:]:
        index_gap = index - previous
        time_gap = (points[index]["time"] - points[previous]["time"]).total_seconds()
        if index_gap > max_gap_points or time_gap > max_gap_seconds:
            segments.append((start, previous))
            start = index
        previous = index
    segments.append((start, previous))
    return segments


def segment_score(points: list[dict[str, Any]], start: int, end: int) -> float:
    duration = (points[end]["time"] - points[start]["time"]).total_seconds()
    return max(0.0, duration) * max(1, end - start + 1)


def estimate_laps_for_route(
    route_file: Path,
    route_metadata: dict[str, Any],
    measurement_crs: CRS,
    loop_line: LineString,
    loop_length_m: float,
    near_threshold_m: float,
    max_gap_points: int,
    max_gap_seconds: float,
    completion_tolerance_laps: float,
) -> dict[str, Any]:
    points = parse_gpx_points(route_file, measurement_crs)
    if len(points) < 2:
        return {"file_name": route_file.name, "error": "not enough timestamped GPX points"}

    distances = [float(loop_line.distance(Point(point["x"], point["y"]))) for point in points]
    stations = [float(loop_line.project(Point(point["x"], point["y"]))) for point in points]
    near_flags = [distance <= near_threshold_m for distance in distances]
    segments = near_segments(points, near_flags, max_gap_points=max_gap_points, max_gap_seconds=max_gap_seconds)
    if not segments:
        return {"file_name": route_file.name, "error": "no near-loop segment"}
    start, end = max(segments, key=lambda pair: segment_score(points, pair[0], pair[1]))
    segment_indexes = list(range(start, end + 1))
    segment_times = [points[index]["time"] for index in segment_indexes]
    segment_stations = [stations[index] for index in segment_indexes]
    segment_distances = [distances[index] for index in segment_indexes]
    segment_near_fraction = sum(1 for index in segment_indexes if near_flags[index]) / len(segment_indexes)

    unwrapped = [segment_stations[0]]
    deltas: list[float] = []
    for current, previous in zip(segment_stations[1:], segment_stations[:-1], strict=True):
        delta = circular_delta(current, previous, loop_length_m)
        deltas.append(delta)
        unwrapped.append(unwrapped[-1] + delta)
    total_progress_m = unwrapped[-1] - unwrapped[0]
    direction = 1 if total_progress_m >= 0 else -1
    observed_laps = abs(total_progress_m) / loop_length_m
    complete_laps = int(math.floor(observed_laps + completion_tolerance_laps))
    complete_lap_end_time = None
    if complete_laps > 0:
        target = unwrapped[0] + direction * complete_laps * loop_length_m
        complete_lap_end_time = interpolate_time_at_unwrapped_station(segment_times, unwrapped, target, direction)
        if complete_lap_end_time is None and observed_laps + 1e-9 >= complete_laps - completion_tolerance_laps:
            complete_lap_end_time = segment_times[-1]
    total_abs_delta = sum(abs(delta) for delta in deltas)
    aligned_delta = sum(direction * delta for delta in deltas if delta * direction > 0)
    monotonicity = aligned_delta / total_abs_delta if total_abs_delta > 0 else None
    lap_elapsed_s = (complete_lap_end_time - segment_times[0]).total_seconds() if complete_lap_end_time is not None else None
    workout_distance_m = route_metadata.get("workout_distance_m")
    timezone_name = route_metadata.get("timezone")
    return {
        "route_path": route_metadata.get("route_path"),
        "file_name": route_file.name,
        "workout_start": route_metadata.get("workout_start"),
        "workout_end": route_metadata.get("workout_end"),
        "workout_duration_min": float(route_metadata["workout_duration_min"]) if route_metadata.get("workout_duration_min") else None,
        "workout_distance_m": float(workout_distance_m) if workout_distance_m else None,
        "gpx_length_m": float(route_metadata["gpx_length_m"]) if route_metadata.get("gpx_length_m") else None,
        "gpx_length_laps": float(route_metadata["estimated_laps_by_gpx_length"]) if route_metadata.get("estimated_laps_by_gpx_length") else None,
        "near_loop_point_fraction_30m": float(route_metadata["near_loop_point_fraction_30m"]) if route_metadata.get("near_loop_point_fraction_30m") else None,
        "lap_segment_start_time_utc": iso(segment_times[0]),
        "lap_segment_end_time_utc": iso(segment_times[-1]),
        "lap_segment_start_time_local": zoned_iso(segment_times[0], timezone_name),
        "lap_segment_end_time_local": zoned_iso(segment_times[-1], timezone_name),
        "lap_segment_elapsed_s": (segment_times[-1] - segment_times[0]).total_seconds(),
        "complete_lap_start_time_utc": iso(segment_times[0]) if complete_laps > 0 else None,
        "complete_lap_end_time_utc": iso(complete_lap_end_time),
        "complete_lap_start_time_local": zoned_iso(segment_times[0], timezone_name) if complete_laps > 0 else None,
        "complete_lap_end_time_local": zoned_iso(complete_lap_end_time, timezone_name),
        "complete_lap_elapsed_s": lap_elapsed_s,
        "average_complete_lap_seconds": lap_elapsed_s / complete_laps if lap_elapsed_s is not None and complete_laps > 0 else None,
        "complete_laps": complete_laps,
        "observed_laps_on_near_segment": observed_laps,
        "extra_partial_laps_after_complete": max(0.0, observed_laps - complete_laps),
        "direction": "increasing_station" if direction > 0 else "decreasing_station",
        "median_loop_distance_m": median(segment_distances),
        "max_loop_distance_m": max(segment_distances),
        "near_segment_point_count": len(segment_indexes),
        "near_segment_near_fraction": segment_near_fraction,
        "station_monotonicity": monotonicity,
        "mean_hacc_m": float(route_metadata["mean_hacc_m"]) if route_metadata.get("mean_hacc_m") else None,
        "quality_flag": "ok" if complete_laps > 0 and (monotonicity or 0) >= 0.70 and segment_near_fraction >= 0.75 else "review",
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys and not isinstance(row[key], (dict, list)):
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in keys})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--loop-geojson", type=Path, default=DEFAULT_LOOP_GEOJSON)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--near-threshold-m", type=float, default=35.0)
    parser.add_argument("--max-gap-points", type=int, default=18)
    parser.add_argument("--max-gap-seconds", type=float, default=24.0)
    parser.add_argument("--completion-tolerance-laps", type=float, default=0.08)
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    loop_line, loop_length_m = load_loop(args.loop_geojson, measurement_crs)
    with args.candidates.open("r", encoding="utf-8") as handle:
        candidates_doc = json.load(handle)
    rows: list[dict[str, Any]] = []
    for metadata in candidates_doc["routes"]:
        route_file = args.export_dir / metadata["route_path"]
        rows.append(
            estimate_laps_for_route(
                route_file,
                metadata,
                measurement_crs,
                loop_line,
                loop_length_m,
                near_threshold_m=args.near_threshold_m,
                max_gap_points=args.max_gap_points,
                max_gap_seconds=args.max_gap_seconds,
                completion_tolerance_laps=args.completion_tolerance_laps,
            )
        )
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(rows),
        "estimated_route_count": sum(1 for row in rows if not row.get("error")),
        "ok_count": sum(1 for row in rows if row.get("quality_flag") == "ok"),
        "review_count": sum(1 for row in rows if row.get("quality_flag") == "review"),
        "near_threshold_m": args.near_threshold_m,
        "max_gap_points": args.max_gap_points,
        "max_gap_seconds": args.max_gap_seconds,
        "completion_tolerance_laps": args.completion_tolerance_laps,
        "loop_length_m": loop_length_m,
    }
    output = {"summary": summary, "routes": rows}
    write_json(args.output_dir / "apple-health-loop-lap-estimates.json", output)
    write_csv(args.output_dir / "apple-health-loop-lap-estimates.csv", rows)
    print(json.dumps(summary, indent=2))
    print(f"wrote {args.output_dir / 'apple-health-loop-lap-estimates.csv'}")


if __name__ == "__main__":
    main()
