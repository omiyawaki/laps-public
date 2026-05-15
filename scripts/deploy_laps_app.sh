#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SOURCE_DIR="$PROJECT_ROOT/apps/Laps"
APP_NAME="Laps"
BINARY_NAME="Laps"
INSTALL_DIR="$HOME/Applications"
APP_BUNDLE="$INSTALL_DIR/$APP_NAME.app"
OLD_APP_BUNDLE="$INSTALL_DIR/Laps.app"
BUILD_DIR="$PROJECT_ROOT/build/laps-app"
PLIST="$BUILD_DIR/Info.plist"
RUNS_FILE="$PROJECT_ROOT/data/app/lap-log/runs.json"
APP_ICON_DIR="$APP_SOURCE_DIR/Assets/AppIcon"
TOOLS_REPO="${APPLE_APP_TOOLS_REPO:-/Users/USERNAME/tools/apple-app-tools}"
SIGN_IDENTITY="${LAPS_SIGN_IDENTITY:-${LAPS_SIGN_IDENTITY_UNUSED:-}}"

if [[ ! -d "$TOOLS_REPO" ]]; then
  echo "apple-app-tools repo not found at: $TOOLS_REPO" >&2
  exit 1
fi

mkdir -p "$(dirname "$RUNS_FILE")"
if [[ ! -f "$RUNS_FILE" ]]; then
  printf '[]\n' > "$RUNS_FILE"
fi

mkdir -p "$BUILD_DIR"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleExecutable</key>
  <string>$BINARY_NAME</string>
  <key>CFBundleIdentifier</key>
  <string>local.osamu.runLog-lap-log</string>
  <key>CFBundleDisplayName</key>
  <string>$APP_NAME</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleIconName</key>
  <string>Laps</string>
  <key>CFBundleName</key>
  <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.healthcare-fitness</string>
  <key>LSMinimumSystemVersion</key>
  <string>26.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSDocumentsFolderUsageDescription</key>
  <string>Laps stores its tracked run log in the project directory under Documents.</string>
</dict>
</plist>
PLIST

"$PROJECT_ROOT/scripts/build_laps_icon.sh" --verify-plist "$PLIST"

cd "$APP_SOURCE_DIR"
swift build -c release

if [[ -d "$APP_BUNDLE" ]]; then
  find "$APP_BUNDLE" -maxdepth 1 -name '*.bundle' -exec rm -rf {} +
fi

RESOURCE_ARGS=(
  --resource "$APP_ICON_DIR/Laps.icns"
  --resource "$APP_ICON_DIR/Assets.car"
)
for RESOURCE_BUNDLE in .build/release/*.bundle .build/release/*.resources; do
  if [[ -e "$RESOURCE_BUNDLE" ]]; then
    RESOURCE_ARGS+=(--resource "$RESOURCE_BUNDLE")
  fi
done

if [[ -z "$SIGN_IDENTITY" ]] && command -v security >/dev/null 2>&1; then
  SIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null | awk -F '"' '/Apple Development:/ { print $2; exit }')"
fi
if [[ -n "$SIGN_IDENTITY" ]]; then
  export APPLE_APP_SIGN_IDENTITY="$SIGN_IDENTITY"
fi

swift run --package-path "$TOOLS_REPO" -c release apple-app-tools mac package \
  --config "$PROJECT_ROOT/AppleAppBlueprint.yaml" \
  --binary ".build/release/$BINARY_NAME" \
  --plist "$PLIST" \
  "${RESOURCE_ARGS[@]}"

if [[ -n "$SIGN_IDENTITY" ]]; then
  echo "Signed with: $SIGN_IDENTITY"
else
  echo "Signed ad-hoc; set LAPS_SIGN_IDENTITY to keep TCC permissions stable across rebuilds."
fi

if [[ "$OLD_APP_BUNDLE" != "$APP_BUNDLE" && -d "$OLD_APP_BUNDLE" ]]; then
  rm -rf "$OLD_APP_BUNDLE"
  echo "Removed old app bundle: $OLD_APP_BUNDLE"
fi

echo "Installed $APP_BUNDLE"
echo "Run log: $RUNS_FILE"
echo "Open with: open '$APP_BUNDLE'"
