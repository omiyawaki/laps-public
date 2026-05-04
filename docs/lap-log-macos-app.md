# Laps macOS App

## Purpose

Laps is a small native macOS app for recording how many laps were run on a given
day and immediately translating that count into distance using the selected
track's measured loop length. The first built-in track uses the accepted Example City
Loop loop measurement.

Current measurement constants:

- Curb source: `geometry/curb-edge-visual-candidate.geojson`
- Running line: `geometry/running-line-1m-visual-candidate.geojson`
- Offset: `1.0 m`
- Lap length: `320.0 m`
- Lap length: `0.19883878151594686 mi`

## Milestones

| Milestone | Distance | Exact laps | First whole lap at/over | Color |
| --- | ---: | ---: | ---: | --- |
| 5K | `5000 m` | `15.625` | `16` | green `#2E7D32` |
| 10K | `10000 m` | `31.250` | `32` | blue `#1565C0` |
| Half marathon | `21097.5 m` | `65.930` | `66` | orange `#EF6C00` |
| Marathon | `42195 m` | `131.859` | `132` | red `#C62828` |

The app should color a milestone when the entered lap count reaches or exceeds
the exact milestone distance. It can also show the first whole-lap threshold as
the practical target for runs that only count complete laps.

## MVP UI

Use SwiftUI for a native macOS app.

- Main window: searchable track sidebar, compact running log form for the
  selected track, explicit Log/Graphs view switcher, and results panel.
- Date picker: the day of the run.
- Track selection: a searchable sidebar of measured tracks. The default
  `All Tracks` scope aggregates data across every track. Choosing a specific
  track filters the log and graphs to that track only. Track rows use the
  measured track geometry as their icon.
- Lap input: integer stepper plus direct numeric field. Keep the model flexible
  enough to allow decimal laps later, but default the UI to whole laps.
- Summary panel: when the entry form is empty, show cumulative laps, distance,
  elapsed time, average pace, and scale-reference distance units. Distance
  references are chosen from a prebuilt scale ladder and shown only when the
  cumulative distance is between `0.1x` and `1.1x` of that reference. Once the
  user starts entering a run, switch to current-entry distance output in meters,
  kilometers, miles, and feet. Cumulative distance is shown in miles only to
  avoid truncation as the total grows.
- Milestone strip: colored milestone chips. Completed milestones use their
  assigned color; incomplete milestones are muted but show remaining laps and
  remaining distance.
- Log table: saved runs for the selected track sorted by date, with columns for
  date, laps, miles, elapsed time, and pace. Selecting one run enables an edit
  sheet for changing track, date/time, laps, elapsed minutes, and note.
- Graphs page: interactive Swift Charts for laps, distance, elapsed time, and
  pace over date. Selecting or dragging over the chart shows the nearest run and
  its exact metric values. Trackpad pinch zooms the date axis; two-finger scroll
  pans the visible date range.

Avoid a dashboard-heavy or gamified interface. This is a training utility: fast
entry, clear feedback, and durable local records matter more than decoration.

## Data Model

Store data in the project tree so the log is versionable with the measurement
tooling:

```text
data/app/lap-log/runs.json
```

The current MVP uses plain JSON rather than SwiftData because project-local,
human-diffable storage is more important than opaque app-container storage for
this tool.

Core model:

```swift
@Model
final class RunEntry {
    var trackID: String
    var date: Date
    var laps: Double
    var note: String
    var createdAt: Date
    var updatedAt: Date
}
```

Derived values should not be stored unless export performance later requires
it. Distance is calculated from the selected track's measurement constants:

```text
distance_m = laps * 320.0
distance_km = distance_m / 1000
distance_mi = distance_m / 1609.344
distance_ft = distance_m / 0.3048
```

## Local macOS Permissions

The run log intentionally lives under `~/Documents/Projects`, which is a
macOS-protected location. The app cannot silently pregrant itself Documents
access. The deploy script signs the app with the first available Apple
Development code-signing identity, or with `LAPS_SIGN_IDENTITY` when
set, so macOS can keep that permission stable across rebuilds. The first launch
after switching from ad-hoc signing to the stable identity can still ask once
because macOS sees a new privacy identity. If no stable identity is available,
it falls back to ad-hoc signing and macOS may ask again after rebuilt binaries.

## Implementation Plan

1. Scaffold `apps/Laps/` as a SwiftUI macOS app. Done.
2. Add a tiny track catalog/constants type generated from the current
   measurement diagnostics. Done.
3. Build the form, result summary, milestone chips, and log table. Done.
4. Add project-local JSON persistence. Done.
5. Add deploy tooling for `~/Applications`. Done.
6. Add interactive graph views for training metrics over time. Done.
7. Add cumulative training summary when the entry form is empty. Done.
8. Add stable code signing for local app deployment. Done.
9. Add searchable multi-track selection, all-track aggregation, per-track
   geometry icons, and store `trackID` on each run. Done.
10. Add in-place editing for saved runs. Done.
11. Add CSV export so the running log can be backed up or analyzed elsewhere.
12. Add a small verification test for distance and milestone calculations. Done.

The app should read as a separate product, but the measurement constants should
remain traceable to this project so future curb/offset revisions can update the
app without hand-copying numbers.
