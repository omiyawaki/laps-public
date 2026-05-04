#!/usr/bin/env python3
"""Import high-confidence Apple Health Loop lap estimates into the run log."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ESTIMATES = PROJECT_ROOT / "build" / "apple-health" / "apple-health-loop-lap-estimates.csv"
DEFAULT_RUN_LOG = PROJECT_ROOT / "data" / "app" / "lap-log" / "runs.json"
IMPORT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "lap-log/apple-health-import")
TRACK_ID = "example_loop"


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.astimezone(timezone.utc).replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def find_git_root(path: Path) -> Path | None:
    directory = path.resolve().parent
    while True:
        if (directory / ".git").exists():
            return directory
        if directory.parent == directory:
            return None
        directory = directory.parent


def run_git(repo_root: Path, args: list[str], allowed_returncodes: set[int] | None = None) -> subprocess.CompletedProcess[str]:
    allowed_returncodes = allowed_returncodes or {0}
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode not in allowed_returncodes:
        command = " ".join(["git", *args])
        stderr = result.stderr.strip()
        raise RuntimeError(f"{command} failed with exit code {result.returncode}: {stderr}")
    return result


def sync_run_log_to_git(path: Path, message: str) -> dict[str, Any]:
    repo_root = find_git_root(path)
    if repo_root is None:
        return {"status": "skipped", "reason": "no_git_repo"}

    relative_path = str(path.resolve().relative_to(repo_root))
    status = run_git(repo_root, ["status", "--porcelain", "--", relative_path])
    if not status.stdout.strip():
        return {"status": "unchanged", "path": relative_path}

    run_git(repo_root, ["add", "--", relative_path])
    staged = run_git(repo_root, ["diff", "--cached", "--quiet", "--", relative_path], {0, 1})
    if staged.returncode == 0:
        return {"status": "unchanged", "path": relative_path}

    run_git(repo_root, ["commit", "-m", message, "--", relative_path])
    upstream = run_git(
        repo_root,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        {0, 128},
    )
    if upstream.returncode == 0:
        run_git(repo_root, ["push"])
        return {"status": "committed_and_pushed", "path": relative_path}

    remotes = run_git(repo_root, ["remote"])
    if "origin" not in remotes.stdout.splitlines():
        return {"status": "committed", "path": relative_path, "push": "skipped_no_remote"}

    run_git(repo_root, ["push", "-u", "origin", "HEAD"])
    return {"status": "committed_and_pushed", "path": relative_path}


def imported_id(route_path: str) -> str:
    return str(uuid.uuid5(IMPORT_NAMESPACE, route_path)).upper()


def float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def build_entry(row: dict[str, str], imported_at: str) -> dict[str, Any]:
    lap_start = parse_datetime(row["complete_lap_start_time_utc"])
    lap_end = parse_datetime(row["complete_lap_end_time_utc"])
    observed = float(row["observed_laps_on_near_segment"])
    median_distance = float(row["median_loop_distance_m"])
    hacc = float_or_none(row.get("mean_hacc_m"))
    note_parts = [
        f"Imported from Apple Health route {row['file_name']}",
        f"observed near-loop progress {observed:.2f} laps",
        f"median loop distance {median_distance:.1f} m",
    ]
    if hacc is not None:
        note_parts.append(f"mean hAcc {hacc:.1f} m")
    return {
        "createdAt": imported_at,
        "date": iso_z(lap_start),
        "elapsedSeconds": float(row["complete_lap_elapsed_s"]),
        "id": imported_id(row["route_path"]),
        "lapEnd": iso_z(lap_end),
        "lapStart": iso_z(lap_start),
        "laps": int(row["complete_laps"]),
        "note": "; ".join(note_parts),
        "source": "apple_health_export",
        "trackID": TRACK_ID,
        "updatedAt": imported_at,
    }


def without_timestamps(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key not in {"createdAt", "updatedAt"}}


def sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (entry.get("date") or "", entry.get("id") or "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--estimates", type=Path, default=DEFAULT_ESTIMATES)
    parser.add_argument("--run-log", type=Path, default=DEFAULT_RUN_LOG)
    parser.add_argument(
        "--min-auto-laps",
        type=int,
        default=2,
        help="Minimum complete laps required for an Apple Health estimate to be auto-imported.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-git-sync", action="store_true", help="Write the run log without committing/pushing it.")
    args = parser.parse_args()

    existing = load_json(args.run_log)
    previous_imports = {entry["id"]: entry for entry in existing if entry.get("source") == "apple_health_export"}
    merged_by_id = {entry["id"]: entry for entry in existing if entry.get("source") != "apple_health_export"}
    imported_at = iso_z(datetime.now(timezone.utc))
    rows = list(csv.DictReader(args.estimates.open("r", encoding="utf-8")))
    ok_rows = [row for row in rows if row.get("quality_flag") == "ok" and int(row.get("complete_laps") or "0") > 0]
    selected_rows = [row for row in ok_rows if int(row["complete_laps"]) >= args.min_auto_laps]
    low_lap_review_rows = [row for row in ok_rows if int(row["complete_laps"]) < args.min_auto_laps]
    review_rows = [row for row in rows if row.get("quality_flag") != "ok"]
    new_entries = []
    updated_entries = []
    unchanged_entries = []
    for row in selected_rows:
        entry = build_entry(row, imported_at)
        if entry["id"] in previous_imports:
            previous = previous_imports[entry["id"]]
            entry["createdAt"] = previous.get("createdAt", entry["createdAt"])
            if without_timestamps(previous) == without_timestamps(entry):
                entry["updatedAt"] = previous.get("updatedAt", entry["updatedAt"])
                unchanged_entries.append(entry)
            else:
                updated_entries.append(entry)
        else:
            new_entries.append(entry)
        merged_by_id[entry["id"]] = entry

    selected_ids = {imported_id(row["route_path"]) for row in selected_rows}
    removed_imports = [entry for entry_id, entry in previous_imports.items() if entry_id not in selected_ids]
    merged = sorted(merged_by_id.values(), key=sort_key)
    summary = {
        "estimates": str(args.estimates),
        "run_log": str(args.run_log),
        "ok_rows": len(ok_rows),
        "min_auto_laps": args.min_auto_laps,
        "auto_import_rows": len(selected_rows),
        "low_lap_review_rows": len(low_lap_review_rows),
        "review_rows": len(review_rows),
        "new_entries": len(new_entries),
        "updated_entries": len(updated_entries),
        "unchanged_entries": len(unchanged_entries),
        "removed_previous_imports": len(removed_imports),
        "final_entry_count": len(merged),
        "low_lap_review_files": [row["file_name"] for row in low_lap_review_rows],
        "quality_review_files": [row["file_name"] for row in review_rows],
        "review_files": [row["file_name"] for row in low_lap_review_rows + review_rows],
    }
    if not args.dry_run:
        write_json(args.run_log, merged)
        if not args.no_git_sync:
            summary["git_sync"] = sync_run_log_to_git(
                args.run_log,
                "Import Apple Health run log entries\n\nAutomatically committed after updating the project run log.",
            )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
