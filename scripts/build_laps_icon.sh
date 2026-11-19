#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ICON_DIR="$PROJECT_ROOT/apps/Laps/Assets/AppIcon"
ICON_DOC="$APP_ICON_DIR/Laps.icon"
ICNS_OUT="$APP_ICON_DIR/Laps.icns"
CAR_OUT="$APP_ICON_DIR/Assets.car"
GENERATOR="$PROJECT_ROOT/scripts/generate_laps_icon.py"
GEOMETRY="$PROJECT_ROOT/geometry/running-line-1m-visual-candidate.geojson"
TOOL_REPO="${APPLE_APP_TOOLS_REPO:-$HOME/tools/apple-app-tools}"
VERIFY_PLIST=""
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify-plist)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --verify-plist" >&2
        exit 1
      fi
      VERIFY_PLIST="$2"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "$TOOL_REPO" ]]; then
  echo "apple-app-tools repo not found at: $TOOL_REPO" >&2
  exit 1
fi

verify_plist() {
  local plist="$1"
  local icon_name
  if ! icon_name="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconName' "$plist" 2>/dev/null)"; then
    echo "$plist must set CFBundleIconName to Laps." >&2
    exit 1
  fi
  if [[ "$icon_name" != "Laps" ]]; then
    echo "$plist must set CFBundleIconName to Laps." >&2
    exit 1
  fi
  if /usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "$plist" >/dev/null 2>&1; then
    echo "$plist must not set CFBundleIconFile because it can bypass adaptive Icon Composer assets." >&2
    exit 1
  fi
}

# Returns 0 (rebuild) when outputs are missing or older than any tracked file
# input; returns 1 (cache hit) when both exported assets are newer than every
# file under Laps.icon, the geometry source, and the generator script.
needs_rebuild() {
  [[ -f "$ICNS_OUT" && -f "$CAR_OUT" ]] || return 0

  local baseline="$ICNS_OUT"
  if [[ "$CAR_OUT" -ot "$ICNS_OUT" ]]; then
    baseline="$CAR_OUT"
  fi

  local newer
  newer="$(
    {
      find "$ICON_DOC" -type f -newer "$baseline" -print 2>/dev/null
      for input in "$GENERATOR" "$GEOMETRY"; do
        if [[ -f "$input" && "$input" -nt "$baseline" ]]; then
          printf '%s\n' "$input"
        fi
      done
    } | head -n 1 || true
  )"
  [[ -n "$newer" ]]
}

if [[ $FORCE -eq 0 ]] && ! needs_rebuild; then
  echo "Laps icon assets are up to date; skipping regeneration."
  if [[ -n "$VERIFY_PLIST" ]]; then
    verify_plist "$VERIFY_PLIST"
  fi
  exit 0
fi

if ! command -v pixi >/dev/null 2>&1; then
  echo "Laps icon source has changed and needs regeneration, but" >&2
  echo "Pixi is not available on PATH." >&2
  echo "Install Pixi with Homebrew, or pass --force only when the existing" >&2
  echo "Laps.icns and Assets.car are known to be current." >&2
  exit 1
fi

pixi run --manifest-path "$PROJECT_ROOT" python "$GENERATOR" --measurement-crs "${LAPS_MEASUREMENT_CRS:-EPSG:32631}"

COMMAND=(
  swift run --package-path "$TOOL_REPO" -c release apple-app-tools icon build
  --icon "$ICON_DOC"
  --name Laps
  --out "$APP_ICON_DIR"
  --work "$PROJECT_ROOT/build/laps-app-icon/icon-composer-build"
  --review "$PROJECT_ROOT/build/laps-app-icon/review"
  --review-sheet "$PROJECT_ROOT/build/laps-app-icon/laps-icon-review.png"
  --platform macOS
  --minimum-deployment-target 26.0
)

if [[ -n "$VERIFY_PLIST" ]]; then
  COMMAND+=(--verify-plist "$VERIFY_PLIST")
fi

"${COMMAND[@]}"
