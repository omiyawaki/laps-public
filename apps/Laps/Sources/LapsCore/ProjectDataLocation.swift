import Foundation

public enum ProjectDataLocation {
    public static let runsFilePath = "/Users/USERNAME/Documents/Projects/0001/data/app/lap-log/runs.json"

    public static var runsFileURL: URL {
        URL(fileURLWithPath: runsFilePath)
    }
}
