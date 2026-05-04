#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SOURCE_DIR="$PROJECT_ROOT/apps/Laps"
APP_NAME="Laps"
BINARY_NAME="Laps"
INSTALL_DIR="$HOME/Applications"
APP_BUNDLE="$INSTALL_DIR/$APP_NAME.app"
OLD_APP_BUNDLE="$INSTALL_DIR/Laps.app"
CONTENTS_DIR="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
PLIST="$CONTENTS_DIR/Info.plist"
RUNS_FILE="$PROJECT_ROOT/data/app/lap-log/runs.json"
ICON_FILE="$APP_SOURCE_DIR/Assets/AppIcon/Laps.icns"
ICON_SOURCE="$PROJECT_ROOT/geometry/running-line-1m-visual-candidate.geojson"
SIGN_IDENTITY="${LAPS_SIGN_IDENTITY:-${LAPS_SIGN_IDENTITY_UNUSED:-}}"

mkdir -p "$(dirname "$RUNS_FILE")"
if [[ ! -f "$RUNS_FILE" ]]; then
  printf '[]\n' > "$RUNS_FILE"
fi

if [[ ! -f "$ICON_FILE" || "$PROJECT_ROOT/scripts/generate_laps_icon.py" -nt "$ICON_FILE" || "$ICON_SOURCE" -nt "$ICON_FILE" ]]; then
  if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    echo "Missing project Python environment at $PROJECT_ROOT/.venv/bin/python" >&2
    exit 1
  fi
  "$PROJECT_ROOT/.venv/bin/python" "$PROJECT_ROOT/scripts/generate_laps_icon.py"
fi

cd "$APP_SOURCE_DIR"
swift build -c release

rm -rf "$CONTENTS_DIR"
if [[ -d "$APP_BUNDLE" ]]; then
  find "$APP_BUNDLE" -maxdepth 1 -name '*.bundle' -exec rm -rf {} +
fi
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"
cp ".build/release/$BINARY_NAME" "$MACOS_DIR/$BINARY_NAME"
chmod 755 "$MACOS_DIR/$BINARY_NAME"
cp "$ICON_FILE" "$RESOURCES_DIR/Laps.icns"
for RESOURCE_BUNDLE in .build/release/*.bundle .build/release/*.resources; do
  if [[ -e "$RESOURCE_BUNDLE" ]]; then
    cp -R "$RESOURCE_BUNDLE" "$RESOURCES_DIR/"
  fi
done

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
  <key>CFBundleIconFile</key>
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
  <string>14.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSDocumentsFolderUsageDescription</key>
  <string>Laps stores its tracked run log in the project directory under Documents.</string>
</dict>
</plist>
PLIST

if command -v codesign >/dev/null 2>&1; then
  if [[ -z "$SIGN_IDENTITY" ]] && command -v security >/dev/null 2>&1; then
    SIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null | awk -F '"' '/Apple Development:/ { print $2; exit }')"
  fi
  if [[ -n "$SIGN_IDENTITY" ]]; then
    codesign --force --deep --options runtime --sign "$SIGN_IDENTITY" "$APP_BUNDLE" >/dev/null
    echo "Signed with: $SIGN_IDENTITY"
  else
    codesign --force --deep --sign - "$APP_BUNDLE" >/dev/null
    echo "Signed ad-hoc; set LAPS_SIGN_IDENTITY to keep TCC permissions stable across rebuilds."
  fi
fi

if [[ "$OLD_APP_BUNDLE" != "$APP_BUNDLE" && -d "$OLD_APP_BUNDLE" ]]; then
  rm -rf "$OLD_APP_BUNDLE"
  echo "Removed old app bundle: $OLD_APP_BUNDLE"
fi

echo "Installed $APP_BUNDLE"
echo "Run log: $RUNS_FILE"
echo "Open with: open '$APP_BUNDLE'"
