#!/usr/bin/env python3
"""Build a running line by offsetting the production curb candidate."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Polygon, mapping, shape
from shapely.ops import transform


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEET_TO_METERS = 0.3048


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


def load_line_feature(path: Path) -> tuple[LineString, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        collection = json.load(handle)
    for feature in collection["features"]:
        geometry = shape(feature["geometry"])
        if isinstance(geometry, LineString):
            return geometry, dict(feature.get("properties", {}))
    raise ValueError(f"no LineString feature found in {path}")


def polygon_from_closed_line(line: LineString) -> Polygon:
    coords = list(line.coords)
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    polygon = Polygon(coords)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        raise ValueError("curb line did not form a valid polygon")
    if polygon.geom_type == "MultiPolygon":
        polygon = max(polygon.geoms, key=lambda item: item.area)
    return polygon


def offset_slug(offset_m: float, offset_feet: float) -> str:
    if abs(offset_m - round(offset_m)) < 1e-9:
        return f"{int(round(offset_m))}m"
    if abs(offset_feet - round(offset_feet)) < 1e-9:
        return f"{int(round(offset_feet))}ft"
    if abs(offset_m * 10.0 - round(offset_m * 10.0)) < 1e-9:
        return f"{offset_m:g}m".replace(".", "p")
    return f"{offset_feet:g}ft".replace(".", "p")


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


def lap_count_table(length_m: float, milestones_m: dict[str, float]) -> dict[str, dict[str, float]]:
    table: dict[str, dict[str, float]] = {}
    for name, distance_m in milestones_m.items():
        laps = distance_m / length_m
        floor_laps = int(math.floor(laps))
        ceiling_laps = int(math.ceil(laps))
        nearest_laps = int(round(laps))
        table[name] = {
            "distance_m": float(distance_m),
            "laps": float(laps),
            "floor_laps": floor_laps,
            "distance_at_floor_laps_m": float(floor_laps * length_m),
            "floor_shortfall_m": float(distance_m - floor_laps * length_m),
            "ceiling_laps": ceiling_laps,
            "distance_at_ceiling_laps_m": float(ceiling_laps * length_m),
            "ceiling_surplus_m": float(ceiling_laps * length_m - distance_m),
            "nearest_whole_laps": nearest_laps,
            "distance_at_nearest_whole_laps_m": float(nearest_laps * length_m),
        }
    return table


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--curb-geojson", type=Path, default=PROJECT_ROOT / "geometry" / "curb-edge-visual-candidate.geojson")
    parser.add_argument("--output-geojson", type=Path, default=None)
    parser.add_argument("--diagnostics", type=Path, default=None)
    parser.add_argument("--offset-feet", type=float, default=None)
    parser.add_argument("--offset-meters", type=float, default=None)
    args = parser.parse_args()

    config = load_yaml(args.config)
    measurement_crs = CRS.from_user_input(config["measurement"]["crs"]["preferred_measurement"])
    configured_offset = config["measurement"]["intended_offset"]
    offset_m = float(args.offset_meters) if args.offset_meters is not None else float(configured_offset["meters"])
    offset_feet = float(args.offset_feet) if args.offset_feet is not None else offset_m / FEET_TO_METERS
    if args.offset_feet is not None and args.offset_meters is None:
        offset_m = offset_feet * FEET_TO_METERS
    if args.offset_meters is not None and args.offset_feet is None:
        offset_feet = offset_m / FEET_TO_METERS
    slug = offset_slug(offset_m, offset_feet)
    output_geojson = args.output_geojson or PROJECT_ROOT / "geometry" / f"running-line-{slug}-visual-candidate.geojson"
    diagnostics_path = args.diagnostics or PROJECT_ROOT / "build" / "fusion" / f"running-line-{slug}-visual-diagnostics.json"

    curb_4326, curb_properties = load_line_feature(args.curb_geojson)
    curb_measure = transform_geometry(curb_4326, "EPSG:4326", measurement_crs)
    curb_polygon = polygon_from_closed_line(curb_measure)
    running_polygon = curb_polygon.buffer(-offset_m)
    if running_polygon.is_empty:
        raise ValueError(f"offset of {offset_m:g} m collapsed the curb polygon")
    if running_polygon.geom_type == "MultiPolygon":
        running_polygon = max(running_polygon.geoms, key=lambda item: item.area)
    running_line = LineString(running_polygon.exterior.coords)
    length_m = float(running_line.length)
    properties = {
        "method": "configured_curb_offset",
        "source": str(args.curb_geojson),
        "curb_method": curb_properties.get("method"),
        "offset_feet": float(offset_feet),
        "offset_m": float(offset_m),
        "length_m": length_m,
        "length_km": length_m / 1000.0,
        "length_miles": length_m / 1609.344,
        "length_feet": length_m / FEET_TO_METERS,
    }
    write_geojson(output_geojson, [geojson_feature(running_line, properties, measurement_crs)])

    diagnostics = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": str(args.config),
        "curb_geojson": str(args.curb_geojson),
        "output_geojson": str(output_geojson),
        "measurement_crs": str(measurement_crs),
        "offset_feet": float(offset_feet),
        "offset_m": float(offset_m),
        "curb_length_m": float(curb_measure.length),
        "running_line": properties,
        "lap_counts": lap_count_table(length_m, config["measurement"]["milestones_m"]),
    }
    write_json(diagnostics_path, diagnostics)
    print(f"wrote {output_geojson}")
    print(f"wrote {diagnostics_path}")
    print(f"offset: {offset_feet:.3f} ft / {offset_m:.4f} m")
    print(f"running length: {length_m:.3f} m / {length_m / 1609.344:.6f} mi")


if __name__ == "__main__":
    main()
