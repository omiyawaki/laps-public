import Foundation

public enum ProjectDataLocation {
    /// Default run-log location. Override with the `LAPS_RUNS_PATH`
    /// environment variable (see `RunStore`).
    public static var runsFileURL: URL {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? FileManager.default.homeDirectoryForCurrentUser
        return base
            .appendingPathComponent("Laps", isDirectory: true)
            .appendingPathComponent("runs.json")
    }
}
