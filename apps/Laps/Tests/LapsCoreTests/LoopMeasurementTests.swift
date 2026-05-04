import Testing
import Foundation
@testable import LapsCore

@Test func sixteenLapsExceedsFiveK() {
    let meters = LoopMeasurement.distanceMeters(forLaps: 16)
    #expect(abs(meters - 5_120.0) < 0.000_001)
    #expect(LoopMeasurement.highestMilestone(forMeters: meters)?.id == "five_k")
}

@Test func milestoneLapThresholdsUseOneMeterOffsetMeasurement() {
    let thresholds = LoopMeasurement.milestones.map(\.firstWholeLapAtOrOver)
    #expect(thresholds == [16, 32, 66, 132])
}

@Test func defaultTrackCarriesAcceptedLoopMeasurement() {
    let track = TrackCatalog.defaultTrack
    #expect(track.id == "example_loop")
    #expect(track.name == "Example Loop")
    #expect(abs(track.lapLengthMeters - LoopMeasurement.lapLengthMeters) < 0.000_001)
    #expect(abs(track.lapElevationGainMeters - 1.5) < 0.000_001)
    #expect(abs(track.elevationGainFeet(forLaps: 38) - 187.007874015) < 0.000_001)
    #expect(track.thumbnailResourceName == nil)
    #expect(track.iconPoints.count > 20)
}

@Test func marathonExactLapsMatchesDiagnostics() {
    let marathon = LoopMeasurement.milestones.first { $0.id == "marathon" }
    #expect(marathon != nil)
    #expect(abs((marathon?.exactLaps ?? 0) - 131.859375) < 0.000_001)
}

@Test func existingRunLogEntriesDecodeWithoutElapsedTime() throws {
    let data = Data(
        """
        [{
          "createdAt": "2025-01-02T08:00:00Z",
          "date": "2025-01-01T08:00:00Z",
          "id": "00000000-0000-4000-8000-000000000001",
          "laps": 10,
          "note": "",
          "updatedAt": "2025-01-02T08:00:00Z"
        }]
        """.utf8
    )
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    let entries = try decoder.decode([RunEntry].self, from: data)
    #expect(entries.first?.laps == 10)
    #expect(entries.first?.trackID == TrackCatalog.defaultTrackID)
    #expect(entries.first?.elapsedSeconds == nil)
}
