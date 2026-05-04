import Combine
import Foundation
import LapsCore

@MainActor
final class RunStore: ObservableObject {
    @Published private(set) var entries: [RunEntry] = []

    private let fileURL: URL
    private let calendar = Calendar.current
    private let gitSync: GitRunLogSync?

    init(fileManager: FileManager = .default, gitSync: GitRunLogSync? = .live) {
        self.gitSync = gitSync
        let environment = ProcessInfo.processInfo.environment
        if let override = environment["LAPS_RUNS_PATH"], !override.isEmpty {
            self.fileURL = URL(fileURLWithPath: override)
        } else {
            self.fileURL = ProjectDataLocation.runsFileURL
        }
        do {
            let directory = fileURL.deletingLastPathComponent()
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
            if !fileManager.fileExists(atPath: fileURL.path) {
                try Data("[]\n".utf8).write(to: fileURL, options: [.atomic])
            }
            let data = try Data(contentsOf: fileURL)
            entries = try JSONDecoder.runLog.decode([RunEntry].self, from: data)
            if !entries.isEmpty && data.range(of: Data("\"trackID\"".utf8)) == nil {
                save(syncReason: "Migrate run log schema")
            }
        } catch {
            entries = []
        }
    }

    var sortedEntries: [RunEntry] {
        entries.sorted { lhs, rhs in
            if calendar.isDate(lhs.date, inSameDayAs: rhs.date) {
                return lhs.createdAt > rhs.createdAt
            }
            return lhs.date > rhs.date
        }
    }

    func entries(for trackID: String) -> [RunEntry] {
        sortedEntries.filter { $0.trackID == trackID }
    }

    func runCount(for trackID: String) -> Int {
        entries.filter { $0.trackID == trackID }.count
    }

    func saveRun(trackID: String, date: Date, laps: Double, elapsedSeconds: Double?, note: String) {
        let cleanLaps = max(0, laps)
        let cleanElapsedSeconds = elapsedSeconds.flatMap { $0 > 0 ? $0 : nil }
        let cleanNote = note.trimmingCharacters(in: .whitespacesAndNewlines)
        entries.append(RunEntry(trackID: trackID, date: date, laps: cleanLaps, elapsedSeconds: cleanElapsedSeconds, source: "manual", note: cleanNote))
        save(syncReason: "Add run log entry")
    }

    func updateRun(id: RunEntry.ID, trackID: String, date: Date, laps: Double, elapsedSeconds: Double?, note: String) {
        guard let index = entries.firstIndex(where: { $0.id == id }) else {
            return
        }
        let cleanLaps = max(0, laps)
        let cleanElapsedSeconds = elapsedSeconds.flatMap { $0 > 0 ? $0 : nil }
        let cleanNote = note.trimmingCharacters(in: .whitespacesAndNewlines)
        entries[index].trackID = trackID
        entries[index].date = date
        entries[index].laps = cleanLaps
        entries[index].elapsedSeconds = cleanElapsedSeconds
        entries[index].note = cleanNote
        entries[index].updatedAt = Date()
        save(syncReason: "Update run log entry")
    }

    func delete(_ entry: RunEntry) {
        entries.removeAll { $0.id == entry.id }
        save(syncReason: "Delete run log entry")
    }

    private func save(syncReason: String) {
        do {
            let data = try JSONEncoder.runLog.encode(entries)
            try data.write(to: fileURL, options: [.atomic])
            gitSync?.sync(fileURL: fileURL, reason: syncReason)
        } catch {
            assertionFailure("Could not save run log: \(error)")
        }
    }
}

private extension JSONEncoder {
    static var runLog: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }
}

private extension JSONDecoder {
    static var runLog: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}
