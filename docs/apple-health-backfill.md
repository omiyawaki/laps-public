# Apple Health Backfill

Laps can be back-filled from an Apple Health export. Your own export and the
data derived from it stay out of git (`data/raw/` and `build/` are ignored); no
Apple Health data is included in this repository.

## Source

Export from the Health app (Profile, Export All Health Data) and unzip it under
the ignored raw-data tree, for example:

```text
data/raw/apple_health/export/apple_health_export/
```

The relevant files are:

- `export.xml`: the full HealthKit XML export.
- `workout-routes/*.gpx`: timestamped workout GPS tracks (trackpoint position,
  time, speed, course and accuracy).

`export.xml` links each GPX file to workout metadata through
`Workout/WorkoutRoute/FileReference`, so a route can be matched back to the
workout start and end, duration, activity type and time zone.

## Summary

```bash
scripts/summarize_apple_health_export.py
```

writes route summaries and the list of loop candidates under
`build/apple-health/`.

## Loop Candidate Heuristic

A route is flagged as a loop run when the matched workout is a running workout
and at least `25%` of its GPX points are within `30 m` of
`geometry/running-line-1m-visual-candidate.geojson`. This is deliberately
permissive: it keeps runs that include a warm-up or cool-down outside the loop.

## Lap Extraction

```bash
scripts/extract_loop_laps_from_apple_health.py
```

1. Parse each candidate GPX route.
2. Project every timestamped GPS point onto the running line.
3. Keep the sustained near-loop segment, allowing small GPS gaps, so travel to
   and from the loop is not counted.
4. Unwrap station along the closed loop to estimate repeated loop progress.
5. Count complete laps and interpolate the timestamp at which the final complete
   lap was reached.

Estimates use loop progress rather than total GPX length, so a route whose
total length is, say, 37.47 loop lengths yields 37 complete laps.

## Official Import

```bash
scripts/import_loop_lap_estimates.py
```

The import keeps rows with `quality_flag == ok` and at least two complete laps
by default. It uses deterministic UUIDs derived from the route path, so it can be
rerun without duplicating entries, and it preserves manual entries. Routes with a
single complete lap or a quality flag are held for review rather than imported.
