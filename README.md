# Loop Measurement

## Goal

Estimate the reproducible one-lap distance of the running loop around the
green space bounded by Loop N and Loop S between First Cross Street and
Second Cross Street in Example City, New York.

The current working path is offset from the curb by 1 m, similar to a
sidewalk separated from a road by a narrow grass strip. The target use case is
marathon training, where small per-lap errors compound over more than 100 laps.

## Location

- Center: `placeholder coordinates`
- Decimal: `0.0010000, 0.0010000`
- Roads: Loop N and Loop S
- Cross-streets: First Cross Street and Second Cross Street

## Project Files

- `config/site.yaml` records the AOI, offset, target milestones, CRS choices,
  and primary public source endpoints.
- `docs/data-sources.md` records the first pass through public GIS sources and
  explains which layers are usable, which are only sanity checks, and which
  still need follow-up.
- `docs/method.md` describes the proposed reproducible measurement workflow
  and the main accuracy constraints.
- `scripts/query_sources.rb` refreshes the source-discovery snapshot from the
  configured public ArcGIS REST endpoints.
- `scripts/export_ortho.rb` exports a reproducible 2024 orthoimagery crop for
  the AOI.
- `scripts/extract_downloaded_ortho_crop.py` extracts the matching crop from
  the downloadable the city 2024 orthoimagery zip and renders a
  comparison against the web-service crop.
- `scripts/build_baseline_geometry.py` builds the first reviewable curb/edge
  and 2 ft offset candidate from the tight crop, plus a distance-function
  diagnostic plot for review.
- `scripts/run_geometry_experiments.py` runs the fixed changepoint detector
  through several geometry/noise postprocessors for side-by-side review.
- `scripts/extract_lidar_dem_crop.py` and
  `scripts/extract_lidar_las_crop.py` isolate FEMA 2019 elevation/LiDAR data
  around the loop and render review previews.
- `scripts/build_fused_segmentation.py` builds a first two-dimensional curb
  candidate from seeded superpixel graph segmentations using imagery-only,
  LiDAR-only, and fused feature sets.
- `scripts/smooth_fused_contour.py` and
  `scripts/analyze_curb_curvature.py` apply wavelength-based closed-curve
  filtering and diagnose whether the smoothing respects the tightest physical
  curb bend.
- `scripts/fit_piecewise_curvature.py` fits sparse line/arc/flexible-arc
  geometry to the smoothed curb to remove residual local humps without using
  a global blur.
- `scripts/refine_data_aware_boundary.py` refines the piecewise curb against
  the original ortho and LiDAR evidence rather than treating the previous
  curve as ground truth.
- `scripts/build_loop_viewer.py` builds a local browser review UI with
  hover/click metadata along the current curb path, including separate imagery
  and LiDAR evidence at each station.
- `scripts/analyze_centerline_distance.py` plots nearest Loop centerline
  distance along the current curb path as a diagnostic for residual humps.
- `scripts/analyze_heading_motion.py` treats the curb as a trajectory and
  plots heading residual, curvature residual, and local course-correction
  energy around suspect stations.
- `scripts/refine_visual_curb_edge.py` searches a narrow corridor around the
  current curb for a tangent-continuous visual curb edge using imagery-derived
  road/grass contrast, gray curb-line evidence, and smooth offset penalties.
- `scripts/build_running_line.py` offsets the accepted production curb by the
  configured running distance and writes lap-count diagnostics.
- `scripts/derive_track_elevation.py` samples the accepted running line against
  the FEMA 2019 1 m bare-earth DEM and derives per-lap elevation gain/loss.
- `docs/lap-log-macos-app.md` sketches the native macOS lap logging app that
  will use the measured loop length.
- `docs/apple-health-backfill.md` records Apple Health export extraction and
  route-summary findings for historical Loop runs.
- `apps/Laps/` contains the native macOS lap logging app with default all-track
  aggregation and a searchable measured-track sidebar.
- `scripts/generate_laps_icon.py` generates the app icon from the
  production running-line GeoJSON.
- `scripts/summarize_apple_health_export.py` summarizes Apple Health workout
  routes and flags likely Loop runs for backfill.
- `scripts/extract_loop_laps_from_apple_health.py` estimates complete
  Loop laps and lap-only elapsed time from candidate GPX routes.
- `scripts/import_loop_lap_estimates.py` imports high-confidence Apple
  Health lap estimates into the official project-local run log.
- `scripts/deploy_laps_app.sh` builds and installs the app at
  `~/Applications/Laps.app`.
- `geometry/` contains small candidate GeoJSON files that are suitable for
  review and versioning.
- `requirements.txt` records the Python packages used by the baseline geometry
  workflow.

## Initial Measurement Strategy

1. Define a small area of interest around the green space.
2. Look first for authoritative vector data, especially curb lines, edge of
   pavement, sidewalk, planimetric, or road-surface polygons from Example City,
   the county, New York State, or other public sources.
3. If authoritative vector data is not available, acquire public orthorectified
   imagery and derive the road/curb edge with a scriptable, human-reviewed
   workflow.
4. Generate the running path as an offset from the selected curb/edge geometry.
   The current configured offset is `1 m = 3.280839895 ft`.
5. Measure the offset path in an appropriate projected coordinate reference
   system, then report distance per lap and lap counts for common milestones.
6. Record assumptions, source dates, coordinate systems, smoothing tolerances,
   and uncertainty estimates so the calculation can be rerun.

## Candidate Inputs

- NYS 2024 orthoimagery as the first public imagery base.
- NYS Streets centerlines for naming, bounding, and sanity checks.
- NYS LiDAR/elevation products if they help distinguish curb/road edges.
- Local/county/city GIS layers for curbs, edge of pavement, sidewalks, roads,
  or planimetric features if a scriptable public source is found.
- USGS/The National Map and USDA NAIP imagery as national fallback sources.

## Outputs

- Curated curb or road-edge geometry.
- Configurable offset path geometry.
- One-lap distance in meters, kilometers, miles, and feet.
- Lap-count table for 5K, 10K, half marathon, marathon, and custom targets.
- Notes on uncertainty and data provenance.
- Review overlays and diagnostics showing accepted/omitted curb evidence.

## Current Measurement

- Production curb: `geometry/curb-edge-visual-candidate.geojson`
- Production running line: `geometry/running-line-1m-visual-candidate.geojson`
- Offset: `1.0 m`
- One lap: `320.000 m`, `0.198839 mi`, `1049.869 ft`
- One-lap elevation gain/loss: `1.500 m`, `4.921 ft`, derived from the FEMA
  2019 1 m bare-earth DEM with 10 m closed-profile smoothing.
- Elevation diagnostics:
  `geometry/running-line-1m-elevation-profile.json`
- Milestone diagnostics:
  `build/fusion/running-line-1m-visual-diagnostics.json`
- Lap log data:
  `data/app/lap-log/runs.json`

## Open Questions

- Does Example City or the county publish curb, sidewalk, or edge-of-pavement data
  outside the public maps checked so far?
- Is the intended running line always 1 m from the curb, or does it vary around
  corners and entrances?
- Should the loop follow rounded corner geometry, a runner's natural tangent,
  or a strict geometric offset from the curb?
