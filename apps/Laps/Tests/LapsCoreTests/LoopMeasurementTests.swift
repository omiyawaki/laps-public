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

@Test func standardTrackLanesUseWorldAthleticsStaggers() {
    let expectedLengths = [
        "standard_track_lane_1": 400.000,
        "standard_track_lane_2": 407.038,
        "standard_track_lane_3": 414.704,
        "standard_track_lane_4": 422.370,
        "standard_track_lane_5": 430.034,
        "standard_track_lane_6": 437.700
    ]
    for (trackID, expectedLength) in expectedLengths {
        let track = TrackCatalog.track(for: trackID)
        #expect(track != nil)
        #expect(abs((track?.lapLengthMeters ?? 0) - expectedLength) < 0.000_001)
        #expect(track?.lapElevationGainMeters == 0)
        #expect(track?.elevationSourceDescription == "Assumed flat")
        #expect((track?.iconPoints.count ?? 0) > 20)
        #expect(track?.standardTrackLaneNumber == Int(trackID.replacingOccurrences(of: "standard_track_lane_", with: "")))
    }
}

@Test func trackShortCodesUseCompactTableIDs() {
    #expect(TrackCatalog.defaultTrack.shortCode == "EL")
    #expect(TrackCatalog.track(for: "standard_track_lane_1")?.shortCode == "S1")
    #expect(TrackCatalog.track(for: "standard_track_lane_6")?.shortCode == "S6")
    #expect(TrackCatalog.tracks.allSatisfy { (2...3).contains($0.shortCode.count) })
    #expect(Set(TrackCatalog.tracks.map(\.shortCode)).count == TrackCatalog.tracks.count)
}

@Test func standardTrackLaneIconsUseProportionedLaneGeometry() {
    let lane1 = TrackCatalog.track(for: "standard_track_lane_1")
    let lane6 = TrackCatalog.track(for: "standard_track_lane_6")
    #expect(lane1 != nil)
    #expect(lane6 != nil)

    let lane1Bounds = iconBounds(lane1?.iconPoints ?? [])
    let lane6Bounds = iconBounds(lane6?.iconPoints ?? [])
    let lane6RadiusMeters = 36.800 + 37.700 / (2.0 * Double.pi)
    let expectedLane6Aspect = (84.390 + 2.0 * lane6RadiusMeters) / (2.0 * lane6RadiusMeters)

    #expect(lane1Bounds.width < lane6Bounds.width)
    #expect(lane1Bounds.height < lane6Bounds.height)
    #expect(abs((lane6Bounds.width / lane6Bounds.height) - expectedLane6Aspect) < 0.000_001)
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

private func iconBounds(_ points: [TrackIconPoint]) -> (width: Double, height: Double) {
    guard let minX = points.map(\.x).min(),
          let maxX = points.map(\.x).max(),
          let minY = points.map(\.y).min(),
          let maxY = points.map(\.y).max() else {
        return (0, 0)
    }
    return (maxX - minX, maxY - minY)
}
