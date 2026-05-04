# Data Sources

Last checked: 2026-05-03

## Area Of Interest

- Center: `0.0010000, 0.0010000`
- Roads: Loop N and Loop S
- Cross-streets: First Cross Street and Second Cross Street
- Working offset: `1 m = 3.280839895 ft`
- Measurement context: marathon-training lap counts, where small per-lap errors
  compound across many laps.

## Current Source Ranking

1. NYS 2024 orthoimagery: best first imagery base for tracing the curb or road
   edge.
2. NYS Streets: useful for road names, bounding, and centerline sanity checks.
   It should not be treated as the running line or curb line.
3. NYS LiDAR: useful as supplemental evidence for terrain, intensity, and
   possible road-edge discontinuities. The 2019 LAS crop is dense enough to
   inspect, but ground-classified returns are still borderline for curb-scale
   detection.
4. the county GIS public maps: useful context, but no curb, sidewalk, or
   edge-of-pavement layer has been found yet.
5. City web map sources: worth revisiting only if a scriptable public endpoint
   turns up; the first quick pass did not reveal one.

## NYS Orthoimagery

- Main page: <https://gis.ny.gov/orthoimagery>
- the county downloads:
  <https://gis.ny.gov/example-county-orthoimagery-downloads>
- 2024 service: <https://orthos.its.ny.gov/arcgis/rest/services/wms/2024/MapServer>
- Latest index service:
  <https://services6.arcgis.com/EbVsqZ18sv1kVJ3k/arcgis/rest/services/NYS_Orthoimagery_Latest_Index/FeatureServer>
- All-years index service:
  <https://services6.arcgis.com/EbVsqZ18sv1kVJ3k/arcgis/rest/services/NYS_Orthoimagery_All_Years_Index/FeatureServer>

The point query for the AOI returns both 2024 and 2021 coverages in the latest
index. The all-years index also reports Example City coverage for 2017, 2014, 2011,
2007, 2004, 2001, and 1996.

The 2024 MapServer is a cached natural-color orthoimagery service in Web
Mercator. The service metadata describes the view as roughly 12 inch
resolution, with source imagery at 12 or 6 inches depending on area. The
the county download page lists the 2024 municipality downloads as 4-band
imagery at 0.5 ft resolution over eastern areas and 1.0 ft over western areas,
so the downloadable source should be tested before relying on the web-service
crop for curb extraction.

Initial use:

- Export a small AOI crop from the 2024 MapServer with
  `scripts/export_ortho.rb`.
- Download the the city 2024 zip and extract the intersecting tile crop
  with `scripts/extract_downloaded_ortho_crop.py`.
- Trace the curb/edge-of-road line from that crop using a scriptable workflow.
- Use older imagery only as a comparison if the curb edge is ambiguous in 2024.

Downloaded crop result:

- Source zip: `data/raw/ortho/city_example_sp24.zip`, not retained locally
  after extraction because it is about 1.4 GB and can be re-downloaded.
- Selected tile: `e_06781394_06_12400_4bd_2024.jp2`
- Crop CRS: `EPSG:6537`
- Crop resolution: `0.5 ft/px`
- Crop bands: `4`
- Rendered crop: `build/example-2024-download-loop-tight.png`
- Working raster crop: `data/interim/ortho/example-2024-loop-tight.tif`

## NYS Streets

- Main page: <https://gis.ny.gov/streets-addresses>
- Feature layer:
  <https://services6.arcgis.com/EbVsqZ18sv1kVJ3k/arcgis/rest/services/NYS_Streets/FeatureServer/0>

The NYS Streets layer is a statewide public street centerline dataset. It is
scriptable through ArcGIS REST and is useful because it identifies the local
road segments around the AOI.

Relevant nearby records found in the service include:

- `Loop S`, `CompleteStreetName = Loop South`, `GeometrySource =
  NYSDOP Orthoimagery`, with a main segment length around `205.35 m`.
- `Loop N`, `CompleteStreetName = Loop North`, `GeometrySource =
  NYSDOP Orthoimagery`, with a main segment length around `173.96 m`.
- Additional shorter Loop N/S connector or split segments near First Cross Street
  and Second Cross Street.
- First Cross Street and Second Cross Street centerline records within the
  same neighborhood query.

Caution: these are road centerlines, not curb lines. They should be used for
querying, labeling, rough bounding boxes, and order-of-magnitude checks only.

## NYS Elevation And LiDAR

- Elevation page: <https://gis.ny.gov/elevation>
- LiDAR page: <https://gis.ny.gov/lidar>
- Latest LiDAR collections layer:
  <https://elevation.its.ny.gov/arcgis/rest/services/indexes/Latest_LiDAR_Collections/FeatureServer/0>
- FEMA 2019 FTP root: <ftp://ftp.gis.ny.gov/elevation/LIDAR/FEMA_2019/>
- regional 2008 FTP root:
  <ftp://ftp.gis.ny.gov/elevation/LIDAR/NYSDEC_RegionalCollection2008/>

The AOI intersects two downloadable latest-collection footprints:

- `FEMA 2019`: position spacing `0.7`, DEM resolution `1`, FTP path under
  `FEMA_2019`.
- `regional 2008`: position spacing `1.3`, DEM resolution `2`, FTP
  path under `NYSDEC_RegionalCollection2008`.

The 2019 collection is the more useful LiDAR source because it is denser and
newer. The AOI falls inside tile `18TXX000000` in the Example City subdirectory:

- LAS index layer:
  <https://elevation.its.ny.gov/arcgis/rest/services/LAS_Indexes/FeatureServer/7>
- DEM index layer:
  <https://elevation.its.ny.gov/arcgis/rest/services/Dem_Indexes/FeatureServer/2>
- LAS direct download:
  <https://gisdata.ny.gov/elevation/LIDAR/FEMA_2019/ExampleCounty/18TXX000000.las>
- Bare-earth DEM direct download:
  <https://gisdata.ny.gov/elevation/DEM/FEMA_2019_DEM/ExampleCounty/18TXX000000.tif>

Downloaded/cropped outputs:

- Raw LAS tile: `data/raw/lidar/fema_2019/18TXX000000.las`, about `400 MB`
  and ignored by git.
- Raw 1 m bare-earth DEM tile:
  `data/raw/lidar/fema_2019/18TXX000000.tif`, about `9 MB` and ignored by git.
- DEM crop: `data/interim/lidar/fema-2019-dem-loop-tight.tif`
- DEM preview: `build/lidar/fema-2019-dem-loop-tight.png`
- LAS crop: `data/interim/lidar/fema-2019-las-loop-tight.npz`
- LAS preview: `build/lidar/fema-2019-las-loop-tight.png`
- LAS metadata: `build/lidar/fema-2019-las-loop-tight.json`

The DEM is 1 m resolution and smooths over curb-scale height breaks, so it
should be treated as terrain context. The LAS crop is more promising:

- cropped points: `132,189`
- all-return density: `8.87 points/m^2`
- implied all-return mean spacing: `0.34 m`
- ground-classified points: `43,269`
- ground-return density: `2.90 points/m^2`
- implied ground-return mean spacing: `0.59 m`
- classes present in the crop: unclassified and ground

This is independent evidence worth keeping. However, because the classified
ground surface is still sampled at roughly half-meter spacing, it should not
be expected to trace a curb by itself with sub-meter confidence. The best use
is likely as a supporting layer for 2D image segmentation, especially where
local relief or LAS intensity agrees with an ortho-derived road edge.

The 2008 collection has a small published index zip and accessible metadata,
but it is old enough that it should be treated as a fallback or validation
source, not the primary geometry basis.

## the county GIS

- Public maps experience:
  <https://experience.arcgis.com/experience/8b462d7c4ebb439b96fa3a55cb2a9976/>
- ArcGIS item JSON:
  <https://www.arcgis.com/sharing/rest/content/items/8b462d7c4ebb439b96fa3a55cb2a9976?f=json>

The public county experience exposes several web maps, including outdoor
activities, natural resources, county assets, planning, climate risks, and
special districts. The checked layers include parks/preserves, trails,
municipalities, soils, wetlands, flood hazards, contours, county roads, county
trails, facilities, zoning, and parcels.

No public curb, sidewalk, planimetric, or edge-of-pavement layer has been found
in those county maps so far.

## Method Implications

- The strongest reproducible first pass is imagery-derived, not centerline
  derived.
- The core geometry should be maintained as vector linework with explicit
  versioned assumptions: imagery year, tracing method, offset distance, corner
  treatment, smoothing tolerance, and CRS.
- Length and offset operations should happen in a projected CRS. The current
  preferred measurement CRS is `EPSG:32115` (`NAD83 / New York East`, meters).
  `EPSG:26918` (`NAD83 / UTM zone 18N`, meters) remains useful because several
  public NYS services use it.
- The first user-in-the-loop moment should be visual: review the extracted
  curb/edge and configured-offset path over the 2024 imagery.

## Near-Term Actions

1. Run `scripts/query_sources.rb` when source metadata needs to be refreshed.
2. Re-download the the city 2024 zip only when the extracted crop needs
   to be regenerated.
3. Run `scripts/extract_downloaded_ortho_crop.py` to refresh the working
   0.5-ft orthoimagery crop.
4. Run `scripts/extract_lidar_dem_crop.py` and
   `scripts/extract_lidar_las_crop.py` to refresh elevation/LiDAR review
   layers after re-downloading FEMA 2019 tile `18TXX000000`.
5. Prototype the curb/road-edge extraction and generate a review overlay.
6. Ask for visual confirmation before treating any traced geometry as the
   measurement baseline.
