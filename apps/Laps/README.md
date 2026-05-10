# Laps

Native macOS SwiftUI app for logging laps across measured tracks and translating
them into distance and elevation gain. Built-in tracks include the accepted
Example Loop 1 m offset measurement plus lanes 1-6 of a World Athletics
standard 400 m track.

## Run Locally

```bash
swift run Laps
```

Run records are saved as project-local JSON at:

```text
/Users/USERNAME/Documents/Projects/0001/data/app/lap-log/runs.json
```

That keeps the log next to the measurement project so it can be tracked,
diffed, and backed up with the project itself. The measurement constants live in
`Sources/LapsCore/LoopMeasurement.swift` and should be regenerated
whenever the production running line changes.

After a run is added, edited, or deleted, the app stages and commits only this
run-log JSON file. It also pushes automatically when the project git repo has an
upstream or `origin` remote configured. Set `LAPS_DISABLE_GIT_SYNC=1` when
running special local checks that should not create data commits.

Entries store the selected `trackID`, optional elapsed time in seconds,
lap-start/lap-end timestamps, and a source label. Distance and elevation gain
are derived from the selected track's measurement constants. Manual entry
exposes elapsed time as hour, minute, and second fields; Apple Health backfill
scripts can populate the richer timestamp fields later.

The main app layout has a searchable track sidebar, a track-aware entry form,
and log/graph views. `All Tracks` is the default sidebar scope and aggregates
runs across every measured track; choosing a specific track filters the log and
graphs to that track only. Track rows use a small outline generated from each
track's measured geometry. The new-run header can show a bundled satellite
thumbnail for the selected track when one is available. Adding another measured
loop later should mainly mean adding another `Track` to `TrackCatalog`.

The standard track lane lengths are lane 1 at `400.000 m`, then lanes 2-6 as
`407.038 m`, `414.704 m`, `422.370 m`, `430.034 m`, and `437.700 m` per lap.
These use the standard 400 m lane stagger distances and assume flat elevation.

Saved runs can be edited in place: select one row in the log, click the pencil
button, then update track, date, laps, elapsed time, or note.

When the entry form is empty, the summary shows cumulative training totals and
rolling scale references. References are drawn from a prebuilt ladder, and only
appear while the cumulative distance is between `0.1x` and `1.1x` of that
reference. Cumulative distance is shown in miles only so the value stays legible
as the log grows.

The app has a Graphs tab with interactive Swift Charts for laps, distance,
elapsed time, pace, and elevation gain over date. Click or drag across a chart
to inspect the nearest saved run. Pinch over the chart to zoom the date axis;
two-finger scroll over the chart to pan the visible date range. The Laps metric
is shown only for a selected track, because lap counts are not directly
comparable across different tracks.

## Deploy To Applications

From the project root:

```bash
scripts/deploy_laps_app.sh
```

This builds the release binary, wraps it as a minimal `.app`, installs it at
`~/Applications/Laps.app`, and signs the bundle for local launching.
By default the deploy script uses the first available Apple Development signing
identity so macOS can keep Documents-folder permission stable across rebuilds.
Set `LAPS_SIGN_IDENTITY` to override that identity.

The first launch after changing from ad-hoc signing to a stable identity can
still ask for Documents access once because macOS treats the app as having a new
privacy identity.

## Testing Practice

Prefer non-intrusive testing so the app does not steal focus while the machine
is in use. Run `swift test`, `swift build`, data validation, deploy/signing
checks, and generated visual snapshots first. If visual QA needs screenshots,
write them under `build/visual-qa/` without foregrounding the installed app
whenever possible. Only use foreground macOS UI automation after confirming with
the user in that turn.

## App Icon

The app icon uses Apple's Icon Composer workflow. The source of truth is the
project-specific document at:

```text
Assets/AppIcon/Laps.icon
```

The layered artwork is generated from the accepted 1 m running-line geometry,
then compiled with the shared `apple-app-tools icon build` command:

```bash
scripts/build_laps_icon.sh
```

The script writes compatibility outputs under `Assets/AppIcon/`:

- `Laps.png`
- `Laps.icns`
- `Assets.car`

It also writes default, dark, clear, and tinted rendition previews plus the
small-size review sheet under `build/laps-app-icon/`. The deploy script calls
this icon build and copies both `Laps.icns` and `Assets.car` into
`~/Applications/Laps.app`; the app plist uses `CFBundleIconName` so macOS can
use the adaptive Icon Composer assets.

## Track Thumbnails

The Example Loop thumbnail is generated from the project ortho crop and the
accepted 1 m running-line GeoJSON:

```bash
scripts/generate_track_thumbnail.py
```

The output lives under `Sources/Laps/Resources/TrackThumbnails/` so SwiftPM
bundles it with the app. The deploy script copies the generated SwiftPM
resource bundle into the installed `.app`.
