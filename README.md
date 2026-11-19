# Laps: loop measurement and a lap-logging app

This repository has two parts:

1. A reproducible geospatial pipeline that measures the length of a running
   loop to within a few centimeters per lap, using only public GIS data
   (orthoimagery, LiDAR and street centerlines).
2. **Laps**, a native macOS SwiftUI app that logs laps on measured tracks and
   turns them into distance, pace and elevation gain.

> **All geometry, constants and run data in this repository are synthetic.**
> The example loop is an arbitrary closed curve at a placeholder location
> (see `geometry/README.md`), and the sample run log in `data/app/lap-log/` is
> invented. Nothing here describes a real place or a real person's runs.

## Why

For marathon training on a short loop, a small per-lap error compounds over
more than 100 laps. A 2 m error per lap is 200 m by the end of a long run.
Consumer GPS is not accurate enough to settle that, so the loop is measured from
public orthoimagery and LiDAR instead, and the Laps app logs runs against the
result.

## How the measurement works

The workflow is described in `docs/method.md` and `docs/data-sources.md`.
In short:

1. Pull a small imagery crop and elevation data around the loop from public
   ArcGIS services (the scripts target New York State services; see
   `config/site.yaml` for the site definition).
2. Detect the curb edge. Several detectors and smoothers are compared in
   `scripts/run_geometry_experiments.py` (Hampel, total variation, spline,
   ridge, elastic net, Viterbi, hybrids).
3. Fuse imagery and LiDAR evidence, smooth the closed curve by wavelength,
   then fit sparse line and arc segments so no curve is tighter than the real
   curb.
4. Offset the curb inward by 1 m to get the running line, and compute its
   length and per-lap elevation gain.
5. Review the result in a local browser viewer
   (`scripts/build_loop_viewer.py`).

Candidate geometries are GeoJSON files under `geometry/`. The ones committed
here are the synthetic example loop. The imagery-based steps need real raw
downloads (not committed) for your own site; the downstream steps (running
line, icon generation, elevation profile, Apple Health lap extraction) work
from the GeoJSON.

## Laps app

`apps/Laps` is a SwiftUI app (macOS 26, Swift 6.2) split into:

- `LapsCore`: measurement constants, loop/lap math, date formatting. No UI and
  no external dependencies.
- `Laps`: the app. Searchable track sidebar, entry form, run log, and Swift
  Charts graphs for laps, distance, pace and elevation gain.

Built-in tracks are the synthetic Example Loop (320 m per lap) and lanes 1-6 of
a standard 400 m track.

The run log is stored outside the repository by default
(`~/Library/Application Support/Laps/runs.json`; override with `LAPS_RUNS_PATH`).
Try the app with the synthetic sample log:

```bash
cd apps/Laps
LAPS_RUNS_PATH="$PWD/../../data/app/lap-log/sample-runs.json" swift run Laps
```

## Build and test

Python pipeline (macOS arm64, uses [Pixi](https://pixi.sh)):

```bash
pixi install
pixi run check        # verifies the scientific dependencies import
python scripts/build_baseline_geometry.py --help
```

Laps app:

```bash
cd apps/Laps
swift test            # LapsCore unit tests
swift run Laps
```

`apps/Laps/Package.swift` pulls shared design-system and updater code from
[apple-app-tools-public](https://github.com/omiyawaki/apple-app-tools-public)
as a remote SwiftPM dependency. The icon and deploy scripts also run that
repository's `apple-app-tools` command-line tool; set `APPLE_APP_TOOLS_REPO` to
a local clone of it.

## Data sources and terms

The pipeline reads imagery, DEM and street centerlines from New York State and
local public GIS services (see `docs/data-sources.md`). Those datasets are
published under their providers' terms (for New York State, see
<https://gis.ny.gov>); check them before redistributing any raw downloads or
derived products. No such data is committed here.

## License

MIT. See [LICENSE](LICENSE).
