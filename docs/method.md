# Measurement Method

## Principle

The measurement should be reproducible from public inputs, but not fully
automatic in the brittle sense. The right target is a scriptable pipeline with
versioned human review points:

1. Fetch public source metadata and imagery.
2. Extract or trace the curb/road-edge geometry.
3. Review the traced edge over the source imagery.
4. Generate the configured offset path.
5. Measure the path in a local projected CRS.
6. Produce lap-count tables and uncertainty notes.

## Proposed Open-Source Tooling

- Ruby standard library for lightweight source discovery and downloads.
- GDAL/Rasterio for geospatial raster handling.
- GeoPandas, Shapely, and PyProj for vector geometry, offsets, CRS
  transformations, and length calculations.
- OpenCV or scikit-image for classical edge/segmentation experiments.
- Segment Anything-style image segmentation as an optional assistive parser,
  with prompts and model settings saved so the workflow is rerunnable.

The project should keep the authoritative geometry as vector files and keep
large rasters, generated overlays, and caches outside git.

## Geometry Choices To Record

- Source imagery year and service endpoint.
- Coordinate reference system used for offset and length.
- Which curb/road edge is being traced.
- Whether the path is offset inward or outward from the traced edge.
- Offset distance in meters.
- Corner treatment: strict geometric offset, rounded runner line, or adjusted
  tangents.
- Smoothing and simplification tolerances.
- Manual edits made after algorithmic extraction.

## Accuracy Notes

The configured offset is currently `1.0 m` (`3.280839895 ft`). For a simple closed curve, changing the
offset by a small amount changes the loop length by roughly `2 * pi * offset`
in the direction of expansion. That means the running offset itself is a
meaningful part of the distance, not just a visual detail.

The current working 2024 ortho crop is the downloadable the city source
imagery at `0.5 ft/px` (`0.1524 m/px`) with 4 bands. This is a better basis
than the earlier web-service crop, but final uncertainty will still depend on
how sharply the curb can be identified under tree shadows and at intersections.

The FEMA 2019 LiDAR source adds independent evidence, but it has a different
failure mode than imagery. The cropped LAS point cloud has strong all-return
density (`8.87 points/m^2`), while the classified ground returns are thinner
(`2.90 points/m^2`, about `0.59 m` mean spacing). That makes it useful for
checking broad road/grass breaks and local relief, but probably too sparse to
serve as the only curb source.

For marathon training, the amplification is real: if the lap is about `300 m`,
a marathon is roughly `141` laps. A `1 m` per-lap error would become about
`141 m` over the full marathon. A practical target is therefore to drive the
per-lap uncertainty below `0.5 m` if possible, or at least report clearly if
the public data cannot support that.

## First Baseline Workflow

1. Run `scripts/query_sources.rb --output build/source-discovery.json`.
2. Run `scripts/extract_downloaded_ortho_crop.py` after downloading the City of
   Example City 2024 ortho zip.
3. Derive the image crop from the configured NYS Streets Loop N/S
   loop-forming centerline features plus the configured margin in
   `config/site.yaml`.
4. Run a curb/edge extraction script to create an initial candidate trace over
   the downloaded 0.5-ft 4-band crop.
5. Render an overlay showing:
   - source imagery,
   - traced curb/edge,
   - configured-offset running line,
   - start/end closure point,
   - measured loop distance.
6. Review the overlay before treating the geometry as the baseline.

Expected candidate outputs:

- `geometry/curb-edge-candidate.geojson`
- `geometry/running-line-2ft-candidate.geojson`
- `geometry/loop-centerlines.geojson`
- `build/curb-hybrid-overlay.png`
- `build/curb-hybrid-distance-diagnostics.png`
- `build/lap-distance-candidate.json`

These should remain candidates until the overlay has been visually reviewed.

Latest generated candidate:

- Overlay: `build/curb-hybrid-overlay.png`
- Distance diagnostic: `build/curb-hybrid-distance-diagnostics.png`
- Candidate lap length: `326.283 m`
- Accepted evidence: `206/231` transects on Loop N and `213/272` transects
  on Loop S.
- Post-processing: changepoint Viterbi path with Hampel-style outlier removal
  and linear interpolation through rejected/gap regions.
- Marathon estimate: `122.097` laps.

Experiment comparison artifacts:

- Summary: `build/experiments/summary.json`
- Overlay montage: `build/experiments/overlay-montage.png`
- Per-method overlays and diagnostics: `build/experiments/`

## Current Curb Extraction Approach

The earlier RGB/grassiness detector and the later light-gray curb detector were
useful probes, but both were too dependent on a fixed appearance target. The
current detector treats each transect as a one-dimensional signal and looks for
a local changepoint between relatively homogeneous road-side pixels and
relatively homogeneous non-road/grass-side pixels.

Implemented approach:

1. Work in the downloaded raster's native projected CRS, `EPSG:6537`, so
   transect distances are real local feet/meters.
2. Use the Loop N/S centerlines only as guide rails for sampling inward
   transects and defining a plausible curb-distance band.
3. Along each transect, sample from the road centerline to beyond the expected
   curb/grass transition.
4. Normalize the local 4-band signal and derived features within the transect
   so the detector is less sensitive to absolute brightness and shadow.
5. Score candidate positions with a two-window changepoint statistic: one
   window on the road side, one window on the non-road side. Strong candidates
   have a clean distributional split and relatively low within-window noise.
6. Treat the centerline-to-curb distance as the hidden state `d(s)`, where `s`
   is distance along the NYS Streets centerline.
7. Pick a plausible `d(s)` path through the changepoint candidates using
   dynamic programming with a penalty on sudden jumps between adjacent
   transects.
8. Reject noisy selected points with local robust statistics instead of using a
   global smoothing spline as the default estimator.
9. Fill rejected/gap regions with linear interpolation between trusted
   observations, then draw the curb from `centerline(s) + normal(s) * d(s)`.
10. Allow endpoint/corner zones to retain higher centerline-to-curb distances
   when the changepoint evidence supports them.
11. Mark evidence points that are too far from the final path as omitted/gap
   evidence instead of forcing the curve through them.

The important change is to separate evidence selection from curve manipulation.
The detector proposes evidence points; the geometry model should remove bad
points and preserve real corner behavior instead of smoothing it away.

Current tuned defaults:

- Curb search band: `2.4 m` to `6.4 m` inward from the NYS Streets centerline.
- Transect spacing along the road: `0.75 m`.
- Pixel sampling interval along each transect: `0.10 m`.
- Distance-state grid spacing: `0.05 m`.
- Jump penalty: `6.0`.
- Default postprocessor: `hybrid`.
- Rejection residual threshold floor: `0.38 m`.

Postprocessors tested:

- `spline`: previous weighted smoothing spline after Viterbi.
- `viterbi`: raw dynamic-programming path, no smoothing.
- `hampel`: local outlier rejection plus linear interpolation.
- `tv`: total-variation denoising after robust gap filling.
- `ridge`: robust polynomial ridge regression.
- `elastic-net`: polynomial elastic-net regression.
- `hybrid`: Hampel/interpolation with corner-aware endpoint handling.

Previous light-gray curb detector for comparison:

- Overlay: `build/curb-gray-baseline-overlay.png`
- Distance diagnostic: `build/curb-distance-diagnostics.png`
- Candidate lap length: `328.500 m`
- Accepted evidence: `107/231` transects on Loop N and `84/272` transects
  on Loop S.

## Downloaded Orthoimagery Comparison

The web-service crop is convenient but lower resolution than the downloadable
source imagery. To reproduce the comparison:

1. Download `city_example_sp24.zip` into `data/raw/ortho/`.
2. Run `scripts/extract_downloaded_ortho_crop.py`.
3. If the web-service crop is also present, pass it with `--mapserver-png` and
   review the generated comparison image.

The first extraction selected tile `e_06781394_06_12400_4bd_2024.jp2` and
produced a 4-band crop at `0.5 ft/px`. The large source zip is not retained
after extraction; re-download it only if the crop needs to be regenerated.

Current working imagery:

- `data/interim/ortho/example-2024-loop-tight.tif`
- `build/example-2024-download-loop-tight.png`

## Escalation Path

If the imagery-derived trace cannot meet the needed uncertainty:

- Search more deeply for city/county planimetric, curb, sidewalk, or pavement
  polygons.
- Use the FEMA 2019 LAS crop as an independent supporting layer and test
  whether intensity or local relief discontinuities clarify the curb line.
- Use a physical validation measurement, such as a survey wheel or carefully
  calibrated wheel path, to benchmark the GIS-derived loop length.

## Fused 2D Segmentation Prototype

The first post-transect prototype treats curb detection as a seeded 2D
segmentation problem:

1. Downsample the 0.5 ft ortho crop to a 1 ft working grid.
2. Build image superpixels from RGB, NIR, and NDVI-like features.
3. Seed road-like regions from a narrow buffer around the Loop N/S
   centerlines.
4. Seed green-space regions from the eroded interior loop polygon.
5. Rasterize LiDAR point-cloud evidence onto the same grid: density, intensity,
   ground elevation, local relief, and elevation gradient.
6. Solve the same superpixel graph three ways: imagery-only, LiDAR-only, and
   fused imagery+LiDAR.
7. Extract the 0.5 road/green probability contour as a curb candidate and
   offset the fused closed contour inward by 2 ft.

Current prototype outputs:

- Script: `scripts/build_fused_segmentation.py`
- Overlay: `build/fusion/imagery-lidar-segmentation-overlay.png`
- Diagnostics: `build/fusion/imagery-lidar-segmentation-diagnostics.json`
- Curb candidates:
  `geometry/curb-edge-fused-segmentation-candidates.geojson`
- 2 ft running-line candidate:
  `geometry/running-line-2ft-fused-segmentation-candidate.geojson`

Current candidate measurements:

- imagery-only curb contour: `366.791 m`
- LiDAR-only curb contour: `370.343 m`
- fused curb contour: `368.180 m`
- fused 2 ft inset running line: `359.340 m`

This is not yet a baseline. It is the first promising review artifact because
the two independent evidence families produce similar closed contours.

## Curvature-Constrained Smoothing

The fused segmentation contour still contains short-wavelength wiggles that
are not physically plausible for a granite curb. The smoothing step treats the
closed curb trace as a periodic signal in arclength and applies a Butterworth
Fourier low-pass filter to the `x(s) + i y(s)` curve.

The smoothing parameter is a minimum retained wavelength in meters, not an
abstract spline penalty. This makes the parameter inspectable:

- Script: `scripts/smooth_fused_contour.py`
- Comparison overlay: `build/fusion/fourier-smoothing-comparison.png`
- Diagnostics: `build/fusion/fourier-smoothing-diagnostics.json`
- Smoothed curb candidates:
  `geometry/curb-edge-fused-smoothed-candidates.geojson`
- Smoothed 2 ft running-line candidates:
  `geometry/running-line-2ft-fused-smoothed-candidates.geojson`

The tightest obvious physical curb bend is at the north end of the loop. The
curvature diagnostic fits circles to that bend across multiple smoothing
cutoffs and arc-window sizes:

- Script: `scripts/analyze_curb_curvature.py`
- Diagnostic plot: `build/fusion/curvature-radius-analysis.png`
- Diagnostics JSON: `build/fusion/curvature-radius-diagnostics.json`
- Estimated central north-bend radius: about `8.30 m`
- Interquartile range across central fit windows/cutoffs: `7.80-9.03 m`
- A 90 degree bend at that radius occupies about `13.04 m` of arclength.

Because Fourier wavelength is not the same thing as curve radius, the current
curvature-informed candidate uses an `8 m` wavelength cutoff: it is below the
estimated physical arc scale, so it should preserve the real north bend, while
removing much of the shorter sub-physical wiggle. Current candidate values:

- selected smoothing cutoff: `8 m`
- smoothed curb contour length: `343.975 m`
- smoothed 2 ft running-line length: `340.144 m`

This remains a review candidate. The main visual risk is over-rounding real
small features near the curb/driveway interruptions; the main measurement risk
is under-smoothing segmentation stair-step noise if the cutoff is too low.

## Piecewise Geometry Model

The wavelength filter removes high-frequency noise globally, but some residual
small humps can remain on otherwise straight or broad-curve sections. The next
prototype fits the curb as a sparse sequence of local primitive segments:

- `line`: lateral offset `y(u)=0`
- `circle_arc`: one sagitta parameter,
  `y(u)=4u(1-u)a`
- `clothoid_like`: two-parameter transition,
  `y(u)=4u(1-u)(a+b(2u-1))`
- `flex_arc`: three-parameter low-order arc,
  `y(u)=4u(1-u)(a+bq+c(q^2-1/3))`

This is not a raw x/y spline. Each segment is anchored to the observed curb
interval endpoints and can only express a simple local motion. Dynamic
programming chooses the lowest-cost segmentation with:

- robust distance to the 8 m filtered curb input,
- a penalty for extra segments,
- a penalty for tighter-than-physical curvature based on the north bend
  radius,
- a complexity penalty that prefers line, then one-parameter arc, then
  transition/flexible arc only when needed.

Current piecewise outputs:

- Script: `scripts/fit_piecewise_curvature.py`
- Overlay: `build/fusion/piecewise-curvature-models.png`
- Diagnostics: `build/fusion/piecewise-curvature-diagnostics.json`
- Curb candidates:
  `geometry/curb-edge-piecewise-curvature-candidates.geojson`
- Segment primitives:
  `geometry/curb-edge-piecewise-curvature-segments.geojson`
- 2 ft running-line candidates:
  `geometry/running-line-2ft-piecewise-curvature-candidates.geojson`

Current selected candidate:

- source contour: `8 m` Fourier-smoothed fused curb
- segment penalty: `1.3`
- segment count: `9`
- primitive mix: `8` flexible arcs and `1` one-parameter circular arc
- RMS distance to the 8 m contour: `0.146 m`
- 95th-percentile distance to the 8 m contour: `0.301 m`
- piecewise curb length: `342.264 m`
- piecewise 2 ft running-line length: `338.425 m`

The selected penalty is intentionally aggressive enough to remove residual
humps. The less aggressive sweep remains available in the diagnostics; for
example, penalties `0.62` and `0.90` use `12` segments with RMS distance
around `0.104 m` and running-line length around `338.975 m`.

## Data-Aware Boundary Refinement

The piecewise geometry is now treated as a prior, not as the observed truth.
The data-aware step lets that prior move locally in the normal direction when
the original source data support a better road/green boundary.

Implemented evidence channels:

- imagery features from the 2024 4-band ortho crop: RGB, NIR, luminance,
  chroma, grayness, NDVI, and green excess;
- LiDAR features from the FEMA 2019 LAS crop: point density, intensity,
  ground elevation, local relief, elevation gradient, and ground-observed
  mask.

Current implementation caveat: the optimizer uses one fused road/green score.
LiDAR is included, but its feature weights are lower than the strongest
imagery vegetation features. This means a strong shadow or image artifact can
still pull the curve if the fused score favors it. The next likely improvement
is to make imagery and LiDAR separate terms in the cost function, so a local
image-only edge is not accepted unless the LiDAR evidence is compatible or the
geometric prior strongly supports it.

Current data-aware outputs:

- Script: `scripts/refine_data_aware_boundary.py`
- Overlay: `build/fusion/data-aware-boundary-refinement.png`
- Diagnostics: `build/fusion/data-aware-boundary-diagnostics.json`
- Refined curb: `geometry/curb-edge-data-aware-candidate.geojson`
- Refined 2 ft running line:
  `geometry/running-line-2ft-data-aware-candidate.geojson`
- refined curb length: `341.670 m`
- refined 2 ft running-line length: `337.837 m`

## Interactive Review UI

The hover/click viewer exposes the current candidate geometry and the
underlying source evidence at each station along the curb. It is generated from
project inputs, so it can be rebuilt after every algorithm change:

```sh
scripts/build_loop_viewer.py
python -m http.server 8877 --bind 127.0.0.1 -d build/viewer
```

The viewer includes base layers for ortho imagery, fused road/green score,
imagery-only score, LiDAR-only score, LiDAR local relief, LiDAR elevation
gradient, and LiDAR density. Hover metadata includes station, lat/lon,
piecewise-prior offset, imagery contrast, LiDAR contrast, fused contrast,
centerline-to-curb distance, ground elevation, local relief, elevation
gradient, density, and intensity.

## Centerline-Distance Diagnostic

The current curb can also be checked against the NYS Streets Loop N/S
centerlines. For each station along the data-aware curb, compute the nearest
distance to either centerline. The raw distance is not a final measurement
source because it has expected artifacts near the north/south joins and where
the nearest centerline switches, but its short-scale residual is useful for
finding implausible humps.

Current diagnostic:

- Script: `scripts/analyze_centerline_distance.py`
- Plot: `build/fusion/centerline-distance-analysis.png`
- Diagnostics: `build/fusion/centerline-distance-diagnostics.json`
- Flagged visual humps checked: `14.405 m`, `38.263 m`, `59.421 m`
- `14.405 m` appears strongly in this metric, `59.421 m` moderately, and
  `38.263 m` weakly.

## Heading-Motion Diagnostic

A second diagnostic treats the curb as a trajectory. If the curve contains a
small artificial hump, a person tracing it would briefly steer off the larger
motion and then correct back. That pattern is measured with:

- heading residual: instantaneous direction after removing a local heading
  trend;
- curvature residual: excess left/right steering after removing the broad
  curve;
- course-correction score: local energy in the residual heading/curvature
  motion.

Current diagnostic:

- Script: `scripts/analyze_heading_motion.py`
- Plot: `build/fusion/heading-motion-analysis.png`
- Diagnostics: `build/fusion/heading-motion-diagnostics.json`
- The `14.405 m` region is strong, with about `24.4 deg` local heading
  peak-to-peak motion.
- The `38.263 m` and `59.421 m` regions are weaker at the exact clicked
  stations, but nearby peaks at about `35.2 m` and `62.3 m` show the same
  course-correction signature.
- The viewer labels course-correction local maxima directly on the map and
  lists the ranked peak stations for visual review.

## Visual Curb-Edge Refinement

The remaining humps show that the prior can still dominate if the objective is
too broad. The next refinement therefore uses the current curve only as a
search corridor. Within that corridor, candidate offsets are scored from the
2024 ortho imagery with:

- road/grass contrast across the normal direction;
- oriented normal gradient of the road/green score;
- gray/light curb-line evidence near the candidate center;
- local edge strength;
- penalties on first and second offset differences so the curve cannot make
  small sidesteps unless the visual evidence is strong.

Current visual-edge outputs:

- Script: `scripts/refine_visual_curb_edge.py`
- Overlay: `build/fusion/visual-curb-edge-refinement.png`
- Diagnostics: `build/fusion/visual-curb-edge-diagnostics.json`
- Refined curb: `geometry/curb-edge-visual-candidate.geojson`
- Production 1 m running line:
  `geometry/running-line-1m-visual-candidate.geojson`
- visual curb length: `342.144 m`
- visual 1 m running-line length: `320.000 m`

The first visual-edge pass reduces the suspect motion signatures at the three
review stations:

- `15.399 m`: course score `33.14 -> 9.11`, local heading peak-to-peak
  `24.19 deg -> 11.39 deg`;
- `35.197 m`: course score `25.28 -> 6.20`, local heading peak-to-peak
  `14.78 deg -> 5.11 deg`;
- `62.295 m`: course score `24.01 -> 3.76`, local heading peak-to-peak
  `16.30 deg -> 5.35 deg`.

This candidate is now treated as the production curb in the viewer as the green
`Production visual curb` layer, with the visual 1 m offset path in yellow.
Older 2 ft and prior-curve products remain available as hidden comparison
layers.

## Elevation Gain

The accepted 1 m running line is also sampled against the FEMA 2019 1 m
bare-earth DEM to derive a reproducible per-lap elevation gain. The sampled
closed-loop profile is smoothed before summing positive elevation changes so
single-cell DEM wiggles are not counted as ascent.

Current elevation outputs:

- Script: `scripts/derive_track_elevation.py`
- DEM crop: `data/interim/lidar/fema-2019-dem-loop-tight.tif`
- Profile diagnostics: `geometry/running-line-1m-elevation-profile.json`
- Smoothing: closed-loop Gaussian elevation profile, `sigma = 10 m`
- Smoothed elevation range along the running line: `2.630 m`
- One-lap elevation gain: `1.500 m`, `4.921 ft`
- One-lap elevation loss: `1.500 m`, `4.921 ft`

Because this is a closed loop, gain and loss should be nearly equal. The raw
unsmoothed profile gives `4.753 m` gain/loss, which is a useful upper bound but
likely over-counts raster-scale noise.
