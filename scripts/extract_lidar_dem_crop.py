#!/usr/bin/env python3
"""Extract and render the FEMA 2019 DEM tile around the Example Loop."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import rasterio
import yaml
from pyproj import CRS, Transformer
from rasterio.mask import mask
from shapely.geometry import LineString, Polygon, mapping
from shapely.ops import transform

from build_baseline_geometry import query_loop_centerlines, line_from_feature


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


def normalize(values: np.ndarray, mask_values: np.ndarray | None = None) -> np.ndarray:
    sample = values[np.isfinite(values)] if mask_values is None else mask_values[np.isfinite(mask_values)]
    if sample.size == 0:
        return np.zeros_like(values, dtype=float)
    lo, hi = np.percentile(sample, [2, 98])
    if hi <= lo:
        hi = lo + 1.0
    return np.clip((values - lo) / (hi - lo), 0.0, 1.0)


def hillshade(elevation: np.ndarray, resolution: float, azimuth_deg: float = 315.0, altitude_deg: float = 45.0) -> np.ndarray:
    gy, gx = np.gradient(elevation, resolution, resolution)
    slope = np.pi / 2.0 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    azimuth = np.deg2rad(azimuth_deg)
    altitude = np.deg2rad(altitude_deg)
    shaded = np.sin(altitude) * np.sin(slope) + np.cos(altitude) * np.cos(slope) * np.cos(azimuth - aspect)
    return np.clip((shaded + 1.0) / 2.0, 0.0, 1.0)


def render_dem_preview(
    elevation: np.ndarray,
    transform_affine: Any,
    crs: CRS,
    centerlines: list[LineString],
    output_path: Path,
) -> None:
    nodata_mask = np.isfinite(elevation)
    filled = elevation.copy()
    filled[~nodata_mask] = np.nanmedian(elevation[nodata_mask])
    resolution = abs(float(transform_affine.a))
    shade = hillshade(filled, resolution)
    gy, gx = np.gradient(filled, resolution, resolution)
    slope = np.hypot(gx, gy)
    image = np.dstack(
        [
            0.72 * normalize(filled) + 0.28 * shade,
            0.62 * normalize(filled) + 0.38 * shade,
            0.55 * normalize(filled) + 0.45 * shade,
        ]
    )

    height, width = elevation.shape
    left = transform_affine.c
    top = transform_affine.f
    right = left + transform_affine.a * width
    bottom = top + transform_affine.e * height

    fig, axes = plt.subplots(1, 2, figsize=(12, 6), dpi=180)
    axes[0].imshow(image, extent=[left, right, bottom, top])
    axes[0].set_title("1 m bare-earth DEM + hillshade")
    axes[1].imshow(normalize(slope), cmap="magma", extent=[left, right, bottom, top])
    axes[1].set_title("local elevation gradient")
    for ax in axes:
        for line in centerlines:
            native = transform_geometry(line, "EPSG:4326", crs)
            xs, ys = native.xy
            ax.plot(xs, ys, color="#47d7ff", linewidth=1.0, linestyle="--")
        ax.set_aspect("equal")
        ax.set_axis_off()
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "site.yaml")
    parser.add_argument("--dem", type=Path, default=PROJECT_ROOT / "data" / "raw" / "lidar" / "fema_2019" / "18TXX000000.tif")
    parser.add_argument("--output-tif", type=Path, default=PROJECT_ROOT / "data" / "interim" / "lidar" / "fema-2019-dem-loop-tight.tif")
    parser.add_argument("--output-png", type=Path, default=PROJECT_ROOT / "build" / "lidar" / "fema-2019-dem-loop-tight.png")
    parser.add_argument("--metadata", type=Path, default=PROJECT_ROOT / "build" / "lidar" / "fema-2019-dem-loop-tight.json")
    parser.add_argument("--margin-m", type=float, default=18.0)
    args = parser.parse_args()

    config = load_yaml(args.config)
    features = query_loop_centerlines(config)
    label_order = {"Loop N": 0, "Loop S": 1}
    features = sorted(features, key=lambda feature: label_order.get(feature["attributes"]["Label"], 99))
    centerlines_4326 = [line_from_feature(feature) for feature in features]

    with rasterio.open(args.dem) as dataset:
        dem_crs = CRS.from_user_input(dataset.crs)
        native_centerlines = [transform_geometry(line, "EPSG:4326", dem_crs) for line in centerlines_4326]
        bounds_geom = Polygon(list(native_centerlines[0].coords) + list(reversed(native_centerlines[1].coords)))
        if not bounds_geom.is_valid:
            bounds_geom = bounds_geom.buffer(0)
        crop_geom = bounds_geom.buffer(args.margin_m)
        cropped, cropped_transform = mask(dataset, [mapping(crop_geom)], crop=True, nodata=dataset.nodata)
        profile = dataset.profile.copy()
        profile.update(
            {
                "height": cropped.shape[1],
                "width": cropped.shape[2],
                "transform": cropped_transform,
                "compress": "deflate",
            }
        )
        args.output_tif.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(args.output_tif, "w", **profile) as output:
            output.write(cropped)

    elevation = cropped[0].astype(float)
    elevation[elevation <= -9999] = np.nan
    render_dem_preview(elevation, cropped_transform, dem_crs, centerlines_4326, args.output_png)
    finite = elevation[np.isfinite(elevation)]
    metadata = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_dem": str(args.dem),
        "output_tif": str(args.output_tif),
        "output_png": str(args.output_png),
        "source": {
            "collection": "FEMA 2019",
            "tile": "18TXX000000.tif",
            "direct_download": "https://gisdata.ny.gov/elevation/DEM/FEMA_2019_DEM/ExampleCounty/18TXX000000.tif",
            "index_layer": "https://elevation.its.ny.gov/arcgis/rest/services/Dem_Indexes/FeatureServer/2",
            "resolution_m": 1.0,
            "type": "bare-earth DEM",
        },
        "crs": str(dem_crs),
        "shape": [int(cropped.shape[1]), int(cropped.shape[2])],
        "resolution": [float(abs(cropped_transform.a)), float(abs(cropped_transform.e))],
        "elevation_min_m": float(np.min(finite)),
        "elevation_max_m": float(np.max(finite)),
        "elevation_range_m": float(np.max(finite) - np.min(finite)),
        "note": "1 m bare-earth DEM is useful for terrain context but may smooth over curb-scale height breaks.",
    }
    write_json(args.metadata, metadata)
    print(f"wrote {args.output_tif}")
    print(f"wrote {args.output_png}")
    print(f"wrote {args.metadata}")


if __name__ == "__main__":
    main()
