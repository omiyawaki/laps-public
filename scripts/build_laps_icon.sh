#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ICON_DIR="$PROJECT_ROOT/apps/Laps/Assets/AppIcon"
ICON_DOC="$APP_ICON_DIR/Laps.icon"
TOOL_REPO="${ICON_COMPOSER_BUILD_REPO:-/Users/USERNAME/tools/icon-composer-build}"
VERIFY_PLIST=""

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
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "$TOOL_REPO" ]]; then
  echo "icon-composer-build repo not found at: $TOOL_REPO" >&2
  exit 1
fi

if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  echo "Missing project Python environment at $PROJECT_ROOT/.venv/bin/python" >&2
  exit 1
fi

"$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/generate_laps_icon.py"

COMMAND=(
  swift run --package-path "$TOOL_REPO" -c release icon-composer-build build
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
