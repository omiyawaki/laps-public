# Example geometry

Everything in this directory is **synthetic**. The loop is an arbitrary smooth
closed curve placed at a placeholder location (0.001 N, 0.001 E), scaled so the
1 m running line is exactly 320 m per lap. It does not describe a real place.

- `curb-edge-visual-candidate.geojson`: the curb edge (running line + 1 m).
- `running-line-1m-visual-candidate.geojson`: the running line used by the
  Laps app, the icon generator and the Apple Health lap-extraction scripts.
- `running-line-1m-elevation-profile.json`: a synthetic elevation profile with
  1.5 m of gain and loss per lap.

Regenerate or replace them with measurements of your own loop using the
scripts in `scripts/` (see `docs/method.md`). Reproject with
`--measurement-crs EPSG:32631` for the placeholder location.
