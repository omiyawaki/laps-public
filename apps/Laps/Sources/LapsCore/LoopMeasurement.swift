import Foundation

public enum DistanceUnits {
    public static let metersPerMile = 1609.344
    public static let metersPerFoot = 0.3048
}

public struct TrackIconPoint: Codable, Hashable, Sendable {
    public let x: Double
    public let y: Double

    public init(x: Double, y: Double) {
        self.x = x
        self.y = y
    }
}

public struct Milestone: Codable, Hashable, Identifiable, Sendable {
    public let id: String
    public let label: String
    public let meters: Double
    public let colorHex: Int

    public init(id: String, label: String, meters: Double, colorHex: Int) {
        self.id = id
        self.label = label
        self.meters = meters
        self.colorHex = colorHex
    }

    public func exactLaps(for track: Track) -> Double {
        meters / track.lapLengthMeters
    }

    public func firstWholeLapAtOrOver(for track: Track) -> Int {
        Int(ceil(exactLaps(for: track)))
    }

    public var exactLaps: Double {
        exactLaps(for: TrackCatalog.defaultTrack)
    }

    public var firstWholeLapAtOrOver: Int {
        firstWholeLapAtOrOver(for: TrackCatalog.defaultTrack)
    }
}

public struct Track: Codable, Hashable, Identifiable, Sendable {
    public let id: String
    public let name: String
    public let location: String
    public let sourceDescription: String
    public let lapLengthMeters: Double
    public let milestones: [Milestone]
    public let iconPoints: [TrackIconPoint]

    public init(
        id: String,
        name: String,
        location: String,
        sourceDescription: String,
        lapLengthMeters: Double,
        milestones: [Milestone],
        iconPoints: [TrackIconPoint] = []
    ) {
        self.id = id
        self.name = name
        self.location = location
        self.sourceDescription = sourceDescription
        self.lapLengthMeters = lapLengthMeters
        self.milestones = milestones
        self.iconPoints = iconPoints
    }

    public func distanceMeters(forLaps laps: Double) -> Double {
        max(0, laps) * lapLengthMeters
    }

    public func distanceKilometers(forLaps laps: Double) -> Double {
        distanceMeters(forLaps: laps) / 1_000
    }

    public func distanceMiles(forLaps laps: Double) -> Double {
        distanceMeters(forLaps: laps) / DistanceUnits.metersPerMile
    }

    public func distanceFeet(forLaps laps: Double) -> Double {
        distanceMeters(forLaps: laps) / DistanceUnits.metersPerFoot
    }

    public func paceSecondsPerMile(laps: Double, elapsedSeconds: Double?) -> Double? {
        guard let elapsedSeconds, elapsedSeconds > 0 else {
            return nil
        }
        let miles = distanceMiles(forLaps: laps)
        guard miles > 0 else {
            return nil
        }
        return elapsedSeconds / miles
    }

    public func highestMilestone(forMeters meters: Double) -> Milestone? {
        milestones.last { meters >= $0.meters }
    }

    public var lapLengthMiles: Double {
        lapLengthMeters / DistanceUnits.metersPerMile
    }
}

public enum TrackCatalog {
    public static let defaultTrackID = "example_loop"

    public static let defaultMilestones: [Milestone] = [
        Milestone(id: "five_k", label: "5K", meters: 5_000, colorHex: 0x2E7D32),
        Milestone(id: "ten_k", label: "10K", meters: 10_000, colorHex: 0x1565C0),
        Milestone(id: "half_marathon", label: "Half", meters: 21_097.5, colorHex: 0xEF6C00),
        Milestone(id: "marathon", label: "Marathon", meters: 42_195, colorHex: 0xC62828)
    ]

    public static let tracks: [Track] = [
        Track(
            id: defaultTrackID,
            name: "Example Loop",
            location: "Synthetic example",
            sourceDescription: "Visual curb, 1 m offset",
            lapLengthMeters: 320.0,
            milestones: defaultMilestones,
            iconPoints: [
                TrackIconPoint(x: 1.00000, y: 0.24442),
                TrackIconPoint(x: 0.99264, y: 0.20435),
                TrackIconPoint(x: 0.97096, y: 0.16963),
                TrackIconPoint(x: 0.94161, y: 0.14095),
                TrackIconPoint(x: 0.90840, y: 0.11679),
                TrackIconPoint(x: 0.87317, y: 0.09566),
                TrackIconPoint(x: 0.83686, y: 0.07643),
                TrackIconPoint(x: 0.79992, y: 0.05843),
                TrackIconPoint(x: 0.76244, y: 0.04159),
                TrackIconPoint(x: 0.72422, y: 0.02652),
                TrackIconPoint(x: 0.68505, y: 0.01415),
                TrackIconPoint(x: 0.64493, y: 0.00534),
                TrackIconPoint(x: 0.60413, y: 0.00058),
                TrackIconPoint(x: 0.56306, y: 0.00000),
                TrackIconPoint(x: 0.52213, y: 0.00341),
                TrackIconPoint(x: 0.48164, y: 0.01033),
                TrackIconPoint(x: 0.44168, y: 0.01989),
                TrackIconPoint(x: 0.40207, y: 0.03083),
                TrackIconPoint(x: 0.36247, y: 0.04180),
                TrackIconPoint(x: 0.32271, y: 0.05215),
                TrackIconPoint(x: 0.28295, y: 0.06252),
                TrackIconPoint(x: 0.24357, y: 0.07424),
                TrackIconPoint(x: 0.20508, y: 0.08861),
                TrackIconPoint(x: 0.16817, y: 0.10662),
                TrackIconPoint(x: 0.13360, y: 0.12878),
                TrackIconPoint(x: 0.10192, y: 0.15491),
                TrackIconPoint(x: 0.07321, y: 0.18429),
                TrackIconPoint(x: 0.04741, y: 0.21624),
                TrackIconPoint(x: 0.02506, y: 0.25070),
                TrackIconPoint(x: 0.00806, y: 0.28805),
                TrackIconPoint(x: 0.00000, y: 0.32820),
                TrackIconPoint(x: 0.00530, y: 0.36871),
                TrackIconPoint(x: 0.02505, y: 0.40448),
                TrackIconPoint(x: 0.05516, y: 0.43226),
                TrackIconPoint(x: 0.09097, y: 0.45226),
                TrackIconPoint(x: 0.12969, y: 0.46592),
                TrackIconPoint(x: 0.16985, y: 0.47448),
                TrackIconPoint(x: 0.21070, y: 0.47885),
                TrackIconPoint(x: 0.25177, y: 0.47975),
                TrackIconPoint(x: 0.29280, y: 0.47770),
                TrackIconPoint(x: 0.33364, y: 0.47324),
                TrackIconPoint(x: 0.37425, y: 0.46695),
                TrackIconPoint(x: 0.41468, y: 0.45964),
                TrackIconPoint(x: 0.45512, y: 0.45238),
                TrackIconPoint(x: 0.49575, y: 0.44622),
                TrackIconPoint(x: 0.53656, y: 0.44143),
                TrackIconPoint(x: 0.57743, y: 0.43716),
                TrackIconPoint(x: 0.61823, y: 0.43235),
                TrackIconPoint(x: 0.65890, y: 0.42646),
                TrackIconPoint(x: 0.69939, y: 0.41947),
                TrackIconPoint(x: 0.73969, y: 0.41143),
                TrackIconPoint(x: 0.77971, y: 0.40216),
                TrackIconPoint(x: 0.81932, y: 0.39123),
                TrackIconPoint(x: 0.85823, y: 0.37805),
                TrackIconPoint(x: 0.89598, y: 0.36185),
                TrackIconPoint(x: 0.93172, y: 0.34163),
                TrackIconPoint(x: 0.96380, y: 0.31606),
                TrackIconPoint(x: 0.98873, y: 0.28359),
                TrackIconPoint(x: 1.00000, y: 0.24442)            ]
        )
    ]

    public static var defaultTrack: Track {
        tracks[0]
    }

    public static func track(for id: String) -> Track? {
        tracks.first { $0.id == id }
    }
}

public struct RunEntry: Codable, Equatable, Identifiable, Sendable {
    public var id: UUID
    public var trackID: String
    public var date: Date
    public var laps: Double
    public var elapsedSeconds: Double?
    public var lapStart: Date?
    public var lapEnd: Date?
    public var source: String?
    public var note: String
    public var createdAt: Date
    public var updatedAt: Date

    public init(
        id: UUID = UUID(),
        trackID: String = TrackCatalog.defaultTrackID,
        date: Date,
        laps: Double,
        elapsedSeconds: Double? = nil,
        lapStart: Date? = nil,
        lapEnd: Date? = nil,
        source: String? = nil,
        note: String = "",
        createdAt: Date = Date(),
        updatedAt: Date = Date()
    ) {
        self.id = id
        self.trackID = trackID
        self.date = date
        self.laps = laps
        self.elapsedSeconds = elapsedSeconds
        self.lapStart = lapStart
        self.lapEnd = lapEnd
        self.source = source
        self.note = note
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case trackID
        case date
        case laps
        case elapsedSeconds
        case lapStart
        case lapEnd
        case source
        case note
        case createdAt
        case updatedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        trackID = try container.decodeIfPresent(String.self, forKey: .trackID) ?? TrackCatalog.defaultTrackID
        date = try container.decode(Date.self, forKey: .date)
        laps = try container.decode(Double.self, forKey: .laps)
        elapsedSeconds = try container.decodeIfPresent(Double.self, forKey: .elapsedSeconds)
        lapStart = try container.decodeIfPresent(Date.self, forKey: .lapStart)
        lapEnd = try container.decodeIfPresent(Date.self, forKey: .lapEnd)
        source = try container.decodeIfPresent(String.self, forKey: .source)
        note = try container.decodeIfPresent(String.self, forKey: .note) ?? ""
        createdAt = try container.decodeIfPresent(Date.self, forKey: .createdAt) ?? date
        updatedAt = try container.decodeIfPresent(Date.self, forKey: .updatedAt) ?? createdAt
    }
}

public enum LoopMeasurement {
    public static let defaultTrack = TrackCatalog.defaultTrack
    public static let sourceDescription = defaultTrack.sourceDescription
    public static let lapLengthMeters = defaultTrack.lapLengthMeters
    public static let metersPerMile = DistanceUnits.metersPerMile
    public static let metersPerFoot = DistanceUnits.metersPerFoot
    public static let milestones = defaultTrack.milestones

    public static func distanceMeters(forLaps laps: Double) -> Double {
        defaultTrack.distanceMeters(forLaps: laps)
    }

    public static func distanceKilometers(forLaps laps: Double) -> Double {
        defaultTrack.distanceKilometers(forLaps: laps)
    }

    public static func distanceMiles(forLaps laps: Double) -> Double {
        defaultTrack.distanceMiles(forLaps: laps)
    }

    public static func distanceFeet(forLaps laps: Double) -> Double {
        defaultTrack.distanceFeet(forLaps: laps)
    }

    public static func paceSecondsPerMile(laps: Double, elapsedSeconds: Double?) -> Double? {
        defaultTrack.paceSecondsPerMile(laps: laps, elapsedSeconds: elapsedSeconds)
    }

    public static func highestMilestone(forMeters meters: Double) -> Milestone? {
        defaultTrack.highestMilestone(forMeters: meters)
    }

    public static func lapsString(_ laps: Double) -> String {
        if abs(laps - round(laps)) < 0.000_001 {
            return String(format: "%.0f", laps)
        }
        return String(format: "%.2f", laps)
    }
}
