#!/usr/bin/env python3
"""Run geometry post-processing experiments against the fixed changepoint evidence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw


PROJECT_ROOT = Path(__file__).resolve().parents[1]
METHODS = (
    "spline",
    "viterbi",
    "hampel",
    "tv",
    "ridge",
    "elastic-net",
    "hybrid",
)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def make_montage(summary: dict, output_path: Path) -> None:
    tiles: list[Image.Image] = []
    for result in summary["results"]:
        path = Path(result["overlay"])
        image = Image.open(path).convert("RGB")
        target_width = 680
        target_height = round(image.height * target_width / image.width)
        image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
        header_height = 54
        tile = Image.new("RGB", (target_width, target_height + header_height), "white")
        tile.paste(image, (0, header_height))
        draw = ImageDraw.Draw(tile)
        text = (
            f"{result['method']}  "
            f"{result['lap_length_m']:.3f} m  "
            f"N {result['labels']['Loop N']['accepted_count']}/"
            f"{result['labels']['Loop N']['transect_count']}  "
            f"S {result['labels']['Loop S']['accepted_count']}/"
            f"{result['labels']['Loop S']['transect_count']}"
        )
        draw.text((14, 16), text, fill=(0, 0, 0))
        tiles.append(tile)

    columns = 2
    rows = (len(tiles) + columns - 1) // columns
    width = columns * max(tile.width for tile in tiles)
    height = rows * max(tile.height for tile in tiles)
    montage = Image.new("RGB", (width, height), "white")
    for index, tile in enumerate(tiles):
        x = (index % columns) * tile.width
        y = (index // columns) * tile.height
        montage.paste(tile, (x, y))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    montage.save(output_path)


def main() -> None:
    build_dir = PROJECT_ROOT / "build" / "experiments"
    geometry_dir = PROJECT_ROOT / "geometry" / "experiments"
    build_dir.mkdir(parents=True, exist_ok=True)
    geometry_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    script = PROJECT_ROOT / "scripts" / "build_baseline_geometry.py"
    for method in METHODS:
        slug = method.replace("_", "-")
        diagnostics = build_dir / f"{slug}-diagnostics.json"
        overlay = build_dir / f"{slug}-overlay.png"
        distance_plot = build_dir / f"{slug}-distance-diagnostics.png"
        curb_geojson = geometry_dir / f"{slug}-curb.geojson"
        running_geojson = geometry_dir / f"{slug}-running.geojson"
        command = [
            sys.executable,
            str(script),
            "--postprocess",
            method,
            "--overlay",
            str(overlay),
            "--distance-plot",
            str(distance_plot),
            "--diagnostics",
            str(diagnostics),
            "--curb-geojson",
            str(curb_geojson),
            "--running-geojson",
            str(running_geojson),
        ]
        print(f"running {method}")
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
        data = json.loads(diagnostics.read_text(encoding="utf-8"))
        results.append(
            {
                "method": method,
                "lap_length_m": data["lap_length_m"],
                "lap_length_miles": data["lap_length_miles"],
                "lap_counts": data["lap_counts"],
                "labels": {
                    label: {
                        "transect_count": info["transect_count"],
                        "candidate_count": info["candidate_count"],
                        "signal_candidate_count": info["signal_candidate_count"],
                        "accepted_count": info["accepted_count"],
                        "omitted_or_gap_count": info["omitted_or_gap_count"],
                        "median_fitted_distance_m": info["median_fitted_distance_m"],
                        "median_abs_residual_m": info["median_abs_residual_m"],
                    }
                    for label, info in data["labels"].items()
                },
                "overlay": str(overlay),
                "distance_plot": str(distance_plot),
                "diagnostics": str(diagnostics),
                "curb_geojson": str(curb_geojson),
                "running_geojson": str(running_geojson),
            }
        )

    summary = {"results": results}
    summary_path = build_dir / "summary.json"
    write_json(summary_path, summary)
    montage_path = build_dir / "overlay-montage.png"
    make_montage(summary, montage_path)
    print(f"wrote {summary_path}")
    print(f"wrote {montage_path}")


if __name__ == "__main__":
    main()
