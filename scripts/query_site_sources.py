#!/usr/bin/env python3
"""Query public NYS imagery/LiDAR indexes at a configured site point."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def query_url(service_url: str) -> str:
    base = service_url.rstrip("/")
    if base.endswith("/FeatureServer"):
        return f"{base}/0/query"
    if "/FeatureServer/" in base:
        return f"{base}/query"
    return f"{base}/query"


def fetch_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    request_url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        request_url,
        headers={"User-Agent": "laps-measurement/0.1"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("error"):
        raise RuntimeError(f"ArcGIS error from {url}: {data['error']}")
    return data


def point_query(
    service_url: str,
    lon: float,
    lat: float,
    *,
    distance_m: float | None = None,
    return_geometry: bool = False,
    out_fields: str = "*",
) -> dict[str, Any]:
    params: dict[str, str] = {
        "f": "json",
        "where": "1=1",
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        "returnGeometry": "true" if return_geometry else "false",
    }
    if distance_m is not None:
        params["distance"] = str(distance_m)
        params["units"] = "esriSRUnit_Meter"
    return fetch_json(query_url(service_url), params)


def service_metadata(service_url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{service_url.rstrip('/')}?{urllib.parse.urlencode({'f': 'json'})}",
        headers={"User-Agent": "laps-measurement/0.1"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    return {
        key: data.get(key)
        for key in [
            "serviceDescription",
            "description",
            "copyrightText",
            "spatialReference",
            "singleFusedMapCache",
            "tileInfo",
            "pixelSizeX",
            "pixelSizeY",
            "meanPixelSize",
            "maxImageHeight",
            "maxImageWidth",
            "maxRecordCount",
        ]
        if key in data
    }


def attributes(features_json: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(feature.get("attributes", {})) for feature in features_json.get("features", [])]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "build" / "site-sources" / "sources.json")
    parser.add_argument("--street-radius-m", type=float, default=250.0)
    args = parser.parse_args()

    config = load_yaml(args.config)
    site = config["site"]
    lat = float(site["center"]["latitude"])
    lon = float(site["center"]["longitude"])
    sources = config["sources"]
    ortho = sources["nys_orthoimagery"]
    lidar = sources["nys_lidar"]
    streets = sources["nys_streets"]

    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": str(args.config),
        "site": {
            "name": site["name"],
            "latitude": lat,
            "longitude": lon,
            "source_text": site["center"].get("source_text"),
        },
        "orthoimagery_2024_service": {
            "url": ortho["service_2024"],
            "metadata": service_metadata(ortho["service_2024"]),
        },
        "orthoimagery_latest_index": {
            "query_url": query_url(ortho["latest_index"]),
            "features": attributes(point_query(ortho["latest_index"], lon, lat)),
        },
        "orthoimagery_all_years_index": {
            "query_url": query_url(ortho["all_years_index"]),
            "features": attributes(point_query(ortho["all_years_index"], lon, lat)),
        },
        "lidar_latest_dem_service": {
            "url": lidar["latest_dem_service"],
            "metadata": service_metadata(lidar["latest_dem_service"]),
        },
        "lidar_latest_collections": {
            "query_url": query_url(lidar["latest_collections_layer"]),
            "features": attributes(point_query(lidar["latest_collections_layer"], lon, lat)),
        },
        "nearby_streets": {
            "query_url": query_url(streets["feature_layer"]),
            "radius_m": args.street_radius_m,
            "features": attributes(
                point_query(
                    streets["feature_layer"],
                    lon,
                    lat,
                    distance_m=args.street_radius_m,
                    out_fields="OBJECTID,CompleteStreetName,Label,LeftCountyName,RightCountyName,GeometryAccuracy,GeometrySource,Shape__Length",
                )
            ),
        },
    }
    write_json(args.output, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
