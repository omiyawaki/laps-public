# Laps App Map

Generated from `AppleAppBlueprint.yaml`. Edit the blueprint first, then regenerate this map.

## App

- id: `laps`
- platform: `macOS`
- bundle: `~/Applications/Laps.app`
- deploy: `scripts/deploy_laps_app.sh`

## Universal Components

- `icon-composer-build`: Build Apple app icon assets from Icon Composer documents and produce review sheets.
- `mac-app-packager`: Package SwiftPM GUI executables into local .app bundles with metadata, signing, and LaunchServices registration.
- `local-update-ui`: Detect local source drift and expose the standard update toolbar button that redeploys and reopens the app. import `LocalAppUpdateKit`.
- `macos-design-system`: Shared SwiftUI visual language for local macOS tools. import `AppleAppDesignSystem`.

## Package Dependencies

- `apple-app-tools` at `../apple-app-tools`: `AppleAppDesignSystem`, `LocalAppUpdateKit`

## Local App Modules

- Laps app UI: `apps/Laps/Sources/Laps`
- Laps measurement core: `apps/Laps/Sources/LapsCore`
- Laps app assets: `apps/Laps/Assets`

## Custom Code Boundaries

- App-specific code is allowed under `apps/Laps/Sources`.
- App-specific code is allowed under `apps/Laps/Assets`.
- App-specific code is allowed under `scripts`.

If a change looks reusable across local Apple apps, add it to `~/tools/apple-app-tools` and update this blueprint instead of rebuilding it locally.
