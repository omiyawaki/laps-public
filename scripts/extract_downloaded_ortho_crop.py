#!/usr/bin/env python3
"""Extract a loop crop from a downloadable NYS orthoimagery zip."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import rasterio
import requests
import yaml
from PIL import Image
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.merge import merge
from rasterio.transform import from_bounds
from rasterio.warp import reproject


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RASTER_EXTENSIONS = (".tif", ".tiff", ".jp2", ".img")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def raster_members(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as archive:
        return [
            info.filename
            for info in archive.infolist()
            if not info.is_dir() and info.filename.lower().endswith(RASTER_EXTENSIONS)
        ]


def zip_raster_path(zip_path: Path, member: str) -> str:
    return f"zip://{zip_path}!{member}"


def transform_bbox(bbox: list[float], src_crs: Any, dst_crs: Any) -> tuple[float, float, float, float]:
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    minx, miny, maxx, maxy = bbox
    corners = [
        transformer.transform(minx, miny),
        transformer.transform(minx, maxy),
        transformer.transform(maxx, miny),
        transformer.transform(maxx, maxy),
    ]
    xs = [corner[0] for corner in corners]
    ys = [corner[1] for corner in corners]
    return min(xs), min(ys), max(xs), max(ys)


def arcgis_query(url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = requests.get(url, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS query failed: {data['error']}")
    return data


def derive_loop_bbox_3857(config: dict[str, Any]) -> list[float]:
    crop_config = config["imagery_crop"]
    objectids = crop_config["selected_street_objectids"]
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
            "outSR": "3857",
        },
    )
    features = data.get("features", [])
    found = {feature["attributes"]["OBJECTID"] for feature in features}
    missing = set(objectids) - found
    if missing:
        raise RuntimeError(f"Configured street OBJECTIDs not returned: {sorted(missing)}")

    points = [
        point
        for feature in features
        for path in feature["geometry"].get("paths", [])
        for point in path
    ]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    margin_m = float(crop_config["margin_m"])
    return [min(xs) - margin_m, min(ys) - margin_m, max(xs) + margin_m, max(ys) + margin_m]


def intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def find_intersecting_members(zip_path: Path, target_bbox_3857: list[float]) -> tuple[list[str], Any, tuple[float, float, float, float]]:
    members = raster_members(zip_path)
    if not members:
        raise RuntimeError(f"No raster members found in {zip_path}")

    selected: list[str] = []
    selected_crs = None
    selected_target_bbox = None

    for member in members:
        path = zip_raster_path(zip_path, member)
        try:
            with rasterio.open(path) as dataset:
                if dataset.crs is None:
                    continue
                target_bbox = transform_bbox(target_bbox_3857, "EPSG:3857", dataset.crs)
                if intersects(tuple(dataset.bounds), target_bbox):
                    selected.append(member)
                    selected_crs = dataset.crs
                    selected_target_bbox = target_bbox
        except Exception:
            continue

    if not selected:
        raise RuntimeError("No raster tile in the zip intersects the loop crop bbox")

    return selected, selected_crs, selected_target_bbox


def crs_unit_factor_to_meters(crs: Any) -> float:
    parsed = CRS.from_user_input(crs)
    if not parsed.axis_info:
        return 1.0
    return float(parsed.axis_info[0].unit_conversion_factor)


def write_crop_geotiff(
    zip_path: Path,
    members: list[str],
    source_crop_bbox: tuple[float, float, float, float],
    target_bbox_3857: list[float],
    output_path: Path,
) -> dict[str, Any]:
    datasets = [rasterio.open(zip_raster_path(zip_path, member)) for member in members]
    try:
        source_crs = datasets[0].crs
        mosaic, source_transform = merge(datasets, bounds=source_crop_bbox)
        source_resolution = list(datasets[0].res)
        source_resolution_m = abs(float(source_resolution[0])) * crs_unit_factor_to_meters(source_crs)
        output_width = max(1, int(round((target_bbox_3857[2] - target_bbox_3857[0]) / source_resolution_m)))
        output_height = max(1, int(round((target_bbox_3857[3] - target_bbox_3857[1]) / source_resolution_m)))
        output_transform = from_bounds(*target_bbox_3857, width=output_width, height=output_height)
        output = np.zeros((mosaic.shape[0], output_height, output_width), dtype=mosaic.dtype)
        for band_index in range(mosaic.shape[0]):
            reproject(
                source=mosaic[band_index],
                destination=output[band_index],
                src_transform=source_transform,
                src_crs=source_crs,
                dst_transform=output_transform,
                dst_crs="EPSG:3857",
                resampling=Resampling.bilinear,
            )
        profile = datasets[0].profile.copy()
        profile.update(
            {
                "driver": "GTiff",
                "height": output_height,
                "width": output_width,
                "transform": output_transform,
                "crs": "EPSG:3857",
                "count": output.shape[0],
                "compress": "deflate",
                "tiled": True,
            }
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(output)

        return {
            "crs": "EPSG:3857",
            "source_crs": str(source_crs),
            "transform": list(output_transform),
            "shape": list(output.shape),
            "bounds": list(target_bbox_3857),
            "source_bounds": list(source_crop_bbox),
            "source_resolution": source_resolution,
            "source_resolution_m": source_resolution_m,
            "output_resolution_m": [
                (target_bbox_3857[2] - target_bbox_3857[0]) / output_width,
                (target_bbox_3857[3] - target_bbox_3857[1]) / output_height,
            ],
        }
    finally:
        for dataset in datasets:
            dataset.close()


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


def render_download_crop(crop_tif: Path, output_png: Path) -> dict[str, Any]:
    with rasterio.open(crop_tif) as dataset:
        array = dataset.read()
        rgb = stretch_rgb(array)
        output_png.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb).save(output_png)
        return {
            "image_size_px": [int(dataset.width), int(dataset.height)],
            "resolution": list(dataset.res),
            "band_count": int(dataset.count),
            "crs": str(dataset.crs),
        }


def render_comparison(mapserver_png: Path, downloaded_png: Path, output_png: Path) -> None:
    mapserver = np.asarray(Image.open(mapserver_png).convert("RGB"))
    downloaded = np.asarray(Image.open(downloaded_png).convert("RGB"))

    fig, axes = plt.subplots(1, 2, figsize=(12, 6), dpi=180)
    axes[0].imshow(mapserver)
    axes[0].set_title("NYS MapServer crop")
    axes[1].imshow(downloaded)
    axes[1].set_title("Downloaded NYS ortho crop")
    for ax in axes:
        ax.set_axis_off()
    fig.tight_layout()
    output_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_png, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--zip", type=Path, default=PROJECT_ROOT / "data" / "raw" / "ortho" / "city_example_sp24.zip")
    parser.add_argument("--crop-metadata", type=Path, default=None)
    parser.add_argument("--mapserver-png", type=Path, default=None)
    parser.add_argument("--output-tif", type=Path, default=PROJECT_ROOT / "data" / "interim" / "ortho" / "example-2024-loop-tight.tif")
    parser.add_argument("--output-png", type=Path, default=PROJECT_ROOT / "build" / "example-2024-download-loop-tight.png")
    parser.add_argument("--comparison-png", type=Path, default=PROJECT_ROOT / "build" / "compare" / "ortho-mapserver-vs-download.png")
    parser.add_argument("--metadata-output", type=Path, default=PROJECT_ROOT / "build" / "example-2024-download-loop-tight.json")
    args = parser.parse_args()

    config = load_yaml(args.config)
    if args.crop_metadata and args.crop_metadata.exists():
        crop_metadata = load_json(args.crop_metadata)
        target_bbox_3857 = crop_metadata.get("bbox_epsg3857") or crop_metadata["crop"]["bbox_epsg3857"]
    else:
        target_bbox_3857 = derive_loop_bbox_3857(config)

    members, source_crs, target_bbox_source = find_intersecting_members(args.zip, target_bbox_3857)
    crop_info = write_crop_geotiff(args.zip, members, target_bbox_source, target_bbox_3857, args.output_tif)
    png_info = render_download_crop(args.output_tif, args.output_png)
    comparison_png = None
    if args.mapserver_png and args.mapserver_png.exists():
        render_comparison(args.mapserver_png, args.output_png, args.comparison_png)
        comparison_png = str(args.comparison_png)

    metadata = {
        "source_zip": str(args.zip),
        "source_crs": str(source_crs),
        "selected_members": members,
        "target_bbox_epsg3857": target_bbox_3857,
        "target_bbox_source_crs": list(target_bbox_source),
        "crop_tif": str(args.output_tif),
        "crop_png": str(args.output_png),
        "comparison_png": comparison_png,
        "crop_info": crop_info,
        "png_info": png_info,
    }
    write_json(args.metadata_output, metadata)

    print(f"selected {len(members)} raster member(s)")
    for member in members:
        print(f"  {member}")
    print(f"wrote {args.output_tif}")
    print(f"wrote {args.output_png}")
    if comparison_png:
        print(f"wrote {args.comparison_png}")


if __name__ == "__main__":
    main()
