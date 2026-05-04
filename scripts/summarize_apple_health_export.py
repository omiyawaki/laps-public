#!/usr/bin/env python3
"""Summarize Apple Health workouts and timestamped workout-route GPX tracks."""

from __future__ import annotations

import argparse
import csv
import json
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPORT_DIR = PROJECT_ROOT / "data" / "raw" / "apple_health" / "export" / "apple_health_export"
DEFAULT_LOOP_GEOJSON = PROJECT_ROOT / "geometry" / "running-line-1m-visual-candidate.geojson"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "site.yaml"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "build" / "apple-health"
METERS_PER_MILE = 1609.344
GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_health_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # Apple Health XML uses strings like "2026-04-29 18:38:15 -0400".
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")


def parse_gpx_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def convert_distance_to_meters(value: str | None, unit: str | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    if unit == "mi":
        return number * METERS_PER_MILE
    if unit == "km":
        return number * 1000.0
    if unit == "m":
        return number
    return number


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


def parse_workouts(export_xml: Path) -> dict[str, dict[str, Any]]:
    route_to_workout: dict[str, dict[str, Any]] = {}
    workout_counts: dict[str, int] = {}
    for event, elem in ET.iterparse(export_xml, events=("end",)):
        if elem.tag != "Workout":
            continue
        attrs = dict(elem.attrib)
        activity = attrs.get("workoutActivityType", "")
        workout_counts[activity] = workout_counts.get(activity, 0) + 1
        route_paths = [node.attrib.get("path", "").lstrip("/") for node in elem.iter("FileReference") if node.attrib.get("path")]
        if route_paths:
            distance_m = convert_distance_to_meters(attrs.get("totalDistance"), attrs.get("totalDistanceUnit"))
            stats_distance_m = None
            stats: dict[str, dict[str, Any]] = {}
            metadata: dict[str, str] = {}
            for child in elem:
                if child.tag == "WorkoutStatistics":
                    stat_type = child.attrib.get("type")
                    if stat_type:
                        stats[stat_type] = dict(child.attrib)
                    if stat_type == "HKQuantityTypeIdentifierDistanceWalkingRunning":
                        stats_distance_m = convert_distance_to_meters(child.attrib.get("sum"), child.attrib.get("unit"))
                elif child.tag == "MetadataEntry":
                    key = child.attrib.get("key")
                    value = child.attrib.get("value")
                    if key and value is not None:
                        metadata[key] = value
            workout = {
                "workout_activity_type": activity,
                "source_name": attrs.get("sourceName"),
                "source_version": attrs.get("sourceVersion"),
                "start": iso(parse_health_datetime(attrs.get("startDate"))),
                "end": iso(parse_health_datetime(attrs.get("endDate"))),
                "duration_min": float(attrs["duration"]) if attrs.get("duration") else None,
                "duration_unit": attrs.get("durationUnit"),
                "total_distance_m": distance_m,
                "distance_walking_running_m": stats_distance_m,
                "metadata": metadata,
                "statistics_types": sorted(stats),
            }
            for route_path in route_paths:
                route_to_workout[route_path] = workout
        elem.clear()
    route_to_workout["__workout_counts__"] = workout_counts
    return route_to_workout


def route_name(path: Path, export_dir: Path) -> str:
    return str(path.relative_to(export_dir))


def parse_gpx(path: Path, measurement_crs: CRS, loop_line: LineString, loop_length_m: float, export_dir: Path) -> dict[str, Any]:
    tree = ET.parse(path)
    root = tree.getroot()
    points: list[tuple[float, float]] = []
    times: list[datetime] = []
    speeds: list[float] = []
    haccs: list[float] = []
    for trkpt in root.findall(".//gpx:trkpt", GPX_NS):
        lon = float(trkpt.attrib["lon"])
        lat = float(trkpt.attrib["lat"])
        points.append((lon, lat))
        time_node = trkpt.find("gpx:time", GPX_NS)
        parsed_time = parse_gpx_datetime(time_node.text if time_node is not None else None)
        if parsed_time is not None:
            times.append(parsed_time)
        for child in trkpt:
            if local_name(child.tag) != "extensions":
                continue
            for ext in child:
                name = local_name(ext.tag)
                if ext.text is None:
                    continue
                if name == "speed":
                    speeds.append(float(ext.text))
                elif name == "hAcc":
                    haccs.append(float(ext.text))

    transformer = Transformer.from_crs("EPSG:4326", measurement_crs, always_xy=True)
    if points:
        x, y = transformer.transform([point[0] for point in points], [point[1] for point in points])
        coords = list(zip(x, y, strict=True))
    else:
        coords = []
    line = LineString(coords) if len(coords) >= 2 else LineString()
    length_m = float(line.length) if not line.is_empty else 0.0
    distances = [loop_line.distance(line.interpolate(distance)) for distance in [i * length_m / 200.0 for i in range(201)]] if length_m > 0 else []
    points_near_loop = 0
    for coord in coords:
        if loop_line.distance(Point(coord)) <= 30.0:
            points_near_loop += 1
    near_fraction = points_near_loop / len(coords) if coords else 0.0
    if line.is_empty:
        bbox = None
    else:
        bbox = {
            "min_x": float(line.bounds[0]),
            "min_y": float(line.bounds[1]),
            "max_x": float(line.bounds[2]),
            "max_y": float(line.bounds[3]),
        }
    return {
        "route_path": route_name(path, export_dir),
        "file_name": path.name,
        "point_count": len(points),
        "start_time_utc": iso(min(times).astimezone(timezone.utc) if times else None),
        "end_time_utc": iso(max(times).astimezone(timezone.utc) if times else None),
        "duration_s_from_points": (max(times) - min(times)).total_seconds() if len(times) >= 2 else None,
        "gpx_length_m": length_m,
        "estimated_laps_by_gpx_length": length_m / loop_length_m if loop_length_m else None,
        "mean_speed_mps": sum(speeds) / len(speeds) if speeds else None,
        "mean_hacc_m": sum(haccs) / len(haccs) if haccs else None,
        "median_loop_distance_m": float(np_median(distances)) if distances else None,
        "min_loop_distance_m": min(distances) if distances else None,
        "near_loop_point_fraction_30m": near_fraction,
        "bbox_measurement_crs": bbox,
    }


def np_median(values: list[float]) -> float:
    sorted_values = sorted(values)
    n = len(sorted_values)
    if n == 0:
        return math.nan
    mid = n // 2
    if n % 2:
        return sorted_values[mid]
    return 0.5 * (sorted_values[mid - 1] + sorted_values[mid])


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
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--loop-geojson", type=Path, default=DEFAULT_LOOP_GEOJSON)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--loop-near-fraction", type=float, default=0.25)
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    loop_line, loop_length_m = load_loop(args.loop_geojson, measurement_crs)
    export_xml = args.export_dir / "export.xml"
    route_dir = args.export_dir / "workout-routes"
    route_to_workout = parse_workouts(export_xml)
    workout_counts = route_to_workout.pop("__workout_counts__", {})

    rows: list[dict[str, Any]] = []
    for path in sorted(route_dir.glob("*.gpx")):
        row = parse_gpx(path, measurement_crs, loop_line, loop_length_m, args.export_dir)
        workout = route_to_workout.get(row["route_path"], {})
        row.update(
            {
                "workout_activity_type": workout.get("workout_activity_type"),
                "workout_start": workout.get("start"),
                "workout_end": workout.get("end"),
                "workout_duration_min": workout.get("duration_min"),
                "workout_distance_m": workout.get("total_distance_m") or workout.get("distance_walking_running_m"),
                "source_name": workout.get("source_name"),
                "timezone": (workout.get("metadata") or {}).get("HKTimeZone"),
            }
        )
        row["is_running_workout"] = row.get("workout_activity_type") == "HKWorkoutActivityTypeRunning"
        row["is_loop_candidate"] = bool(row["is_running_workout"] and row["near_loop_point_fraction_30m"] >= args.loop_near_fraction)
        rows.append(row)

    loop_rows = [row for row in rows if row["is_loop_candidate"]]
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "export_dir": str(args.export_dir),
        "export_xml": str(export_xml),
        "route_dir": str(route_dir),
        "route_count": len(rows),
        "matched_workout_route_count": sum(1 for row in rows if row.get("workout_activity_type")),
        "workout_counts_by_type": workout_counts,
        "running_route_count": sum(1 for row in rows if row.get("is_running_workout")),
        "loop_candidate_count": len(loop_rows),
        "loop_geojson": str(args.loop_geojson),
        "loop_length_m": loop_length_m,
    }

    write_json(args.output_dir / "apple-health-route-summary.json", {"summary": summary, "routes": rows})
    write_json(args.output_dir / "apple-health-loop-candidates.json", {"summary": summary, "routes": loop_rows})
    write_csv(args.output_dir / "apple-health-route-summary.csv", rows)
    write_csv(args.output_dir / "apple-health-loop-candidates.csv", loop_rows)
    print(json.dumps(summary, indent=2))
    print(f"wrote {args.output_dir / 'apple-health-route-summary.csv'}")
    print(f"wrote {args.output_dir / 'apple-health-loop-candidates.csv'}")


if __name__ == "__main__":
    main()
