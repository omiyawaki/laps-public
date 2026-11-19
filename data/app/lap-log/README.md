# Sample run log

`sample-runs.json` is **synthetic sample data** (every entry is marked
`"source": "synthetic_sample"`). It exists so the Laps app and the charts have
something to show. Point the app at it with:

```bash
LAPS_RUNS_PATH="$PWD/data/app/lap-log/sample-runs.json" swift run --package-path apps/Laps Laps
```

Real logs are kept outside the repository by default (see `apps/Laps/README.md`).
