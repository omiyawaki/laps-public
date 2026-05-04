import LapsCore
import Foundation
import SwiftUI

private let allTracksSelectionID = "all_tracks"

struct ContentView: View {
    @EnvironmentObject private var store: RunStore
    @AppStorage("selectedDetailPage") private var selectedDetailPage = DetailPage.log.rawValue
    @State private var selectedScopeID = allTracksSelectionID
    @State private var entryTrackID = TrackCatalog.defaultTrackID
    @State private var trackSearch = ""
    @State private var runDate = Date()
    @State private var laps = 0.0
    @State private var elapsedHours = 0
    @State private var elapsedMinutes = 0
    @State private var elapsedSecondsPart = 0
    @State private var note = ""
    @State private var selectedRunIDs = Set<RunEntry.ID>()
    @State private var editingRunDraft: RunEditDraft?

    private var selectedTrack: Track? {
        guard selectedScopeID != allTracksSelectionID else {
            return nil
        }
        return TrackCatalog.track(for: selectedScopeID)
    }

    private var entryTrack: Track {
        TrackCatalog.track(for: entryTrackID) ?? TrackCatalog.defaultTrack
    }

    private var scopeTitle: String {
        selectedTrack?.name ?? "All Tracks"
    }

    private var filteredTracks: [Track] {
        let query = trackSearch.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            return TrackCatalog.tracks
        }
        return TrackCatalog.tracks.filter { track in
            track.name.localizedCaseInsensitiveContains(query)
                || track.location.localizedCaseInsensitiveContains(query)
                || track.sourceDescription.localizedCaseInsensitiveContains(query)
        }
    }

    private var selectedEntries: [RunEntry] {
        if let selectedTrack {
            return store.entries(for: selectedTrack.id)
        }
        return store.sortedEntries
    }

    private var selectedRuns: [RunEntry] {
        selectedEntries.filter { selectedRunIDs.contains($0.id) }
    }

    private var editableSelectedRun: RunEntry? {
        selectedRuns.count == 1 ? selectedRuns[0] : nil
    }

    private func track(for entry: RunEntry) -> Track {
        TrackCatalog.track(for: entry.trackID) ?? TrackCatalog.defaultTrack
    }

    private var distanceMeters: Double {
        entryTrack.distanceMeters(forLaps: laps)
    }

    private var elapsedSeconds: Double? {
        let totalSeconds = elapsedHours * 3600 + elapsedMinutes * 60 + elapsedSecondsPart
        return totalSeconds > 0 ? Double(totalSeconds) : nil
    }

    private var hasDraftEntry: Bool {
        laps > 0 || elapsedSeconds != nil || !note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var cumulativeTotals: RunTotals {
        RunTotals(entries: selectedEntries)
    }

    private var elevationSourceSummary: String? {
        if let selectedTrack {
            return selectedTrack.elevationSourceDescription
        }
        let sources = Set(selectedEntries.compactMap { track(for: $0).elevationSourceDescription })
        if sources.count == 1 {
            return sources.first
        }
        if sources.count > 1 {
            return "Mixed track sources"
        }
        return nil
    }

    var body: some View {
        NavigationSplitView {
            trackSidebar
                .navigationSplitViewColumnWidth(min: 240, ideal: 280)
        } content: {
            entryPanel(track: entryTrack)
                .navigationSplitViewColumnWidth(min: 300, ideal: 340)
        } detail: {
            VStack(spacing: 0) {
                Picker("View", selection: $selectedDetailPage) {
                    ForEach(DetailPage.allCases) { page in
                        Label(page.title, systemImage: page.systemImage).tag(page.rawValue)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 260)
                .padding(.top, 14)
                .padding(.bottom, 12)

                Divider()

                Group {
                    if selectedDetailPage == DetailPage.graphs.rawValue {
                        RunChartsView(entries: selectedEntries, selectedTrack: selectedTrack)
                    } else {
                        VStack(spacing: 18) {
                            resultsPanel
                            logTable
                        }
                    }
                }
                .padding(18)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            }
        }
        .onChange(of: selectedScopeID) {
            selectedRunIDs.removeAll()
            if let selectedTrack {
                entryTrackID = selectedTrack.id
            }
        }
        .sheet(item: $editingRunDraft) { draft in
            RunEditSheet(
                draft: draft,
                onCancel: {
                    editingRunDraft = nil
                },
                onSave: { edited in
                    store.updateRun(
                        id: edited.entryID,
                        trackID: edited.trackID,
                        date: edited.date,
                        laps: edited.laps,
                        elapsedSeconds: edited.elapsedSeconds,
                        note: edited.note
                    )
                    entryTrackID = edited.trackID
                    if selectedTrack != nil {
                        selectedScopeID = edited.trackID
                    }
                    selectedRunIDs = [edited.entryID]
                    editingRunDraft = nil
                }
            )
        }
    }

    private var trackSidebar: some View {
        List(selection: $selectedScopeID) {
            Section("Tracks") {
                if trackSearch.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || "All Tracks".localizedCaseInsensitiveContains(trackSearch) {
                    AllTracksSidebarRow(trackCount: TrackCatalog.tracks.count, runCount: store.entries.count)
                        .tag(allTracksSelectionID)
                }
                ForEach(filteredTracks) { track in
                    TrackSidebarRow(track: track, runCount: store.runCount(for: track.id))
                        .tag(track.id)
                }
            }
        }
        .navigationTitle("Tracks")
        .searchable(text: $trackSearch, placement: .sidebar, prompt: "Search tracks")
    }

    private func entryPanel(track: Track) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 6) {
                Text("New Run")
                    .font(.title2.bold())
                Text(track.name)
                    .font(.headline)
                Text(track.location)
                    .foregroundStyle(.secondary)
                Text(track.sourceDescription)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Text("\(track.lapLengthMeters, specifier: "%.3f") m per lap")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.secondary)
            }

            Divider()

            Picker("Track", selection: $entryTrackID) {
                ForEach(TrackCatalog.tracks) { track in
                    Text(track.name).tag(track.id)
                }
            }
            .onChange(of: entryTrackID) {
                if selectedTrack != nil {
                    selectedScopeID = entryTrackID
                }
            }

            DatePicker("Date", selection: $runDate, displayedComponents: .date)

            VStack(alignment: .leading, spacing: 8) {
                Text("Laps")
                    .font(.headline)
                HStack {
                    TextField("0", value: $laps, format: .number.precision(.fractionLength(0...2)))
                        .textFieldStyle(.roundedBorder)
                    Stepper("", value: $laps, in: 0...300, step: 1)
                        .labelsHidden()
                }
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("Elapsed")
                    .font(.headline)
                DurationInput(hours: $elapsedHours, minutes: $elapsedMinutes, seconds: $elapsedSecondsPart)
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("Note")
                    .font(.headline)
                TextField("Optional", text: $note, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(3, reservesSpace: true)
            }

            Button {
                store.saveRun(trackID: track.id, date: runDate, laps: laps, elapsedSeconds: elapsedSeconds, note: note)
                laps = 0
                elapsedHours = 0
                elapsedMinutes = 0
                elapsedSecondsPart = 0
                note = ""
            } label: {
                Text("Save Run")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(laps <= 0)

            Spacer()
        }
        .padding(18)
    }

    private var resultsPanel: some View {
        Group {
            if hasDraftEntry {
                currentEntryPanel
            } else {
                cumulativePanel
            }
        }
    }

    private var currentEntryPanel: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text("Current Entry")
                    .font(.title3.bold())
                Spacer()
                Text(LoopMeasurement.lapsString(laps) + " laps")
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 12) {
                DistanceTile(title: "Meters", value: distanceMeters, suffix: "m", digits: 0)
                DistanceTile(title: "Kilometers", value: distanceMeters / 1_000, suffix: "km", digits: 3)
                DistanceTile(title: "Miles", value: distanceMeters / DistanceUnits.metersPerMile, suffix: "mi", digits: 3)
                DistanceTile(title: "Feet", value: distanceMeters / DistanceUnits.metersPerFoot, suffix: "ft", digits: 0)
            }

            HStack(spacing: 12) {
                ValueTile(title: "Elapsed", value: durationString(elapsedSeconds))
                ValueTile(title: "Pace", value: paceString(laps: laps, elapsedSeconds: elapsedSeconds, track: entryTrack))
                ValueTile(title: "Gain", value: elevationGainString(feet: entryTrack.elevationGainFeet(forLaps: laps)))
            }

            HStack(spacing: 8) {
                ForEach(entryTrack.milestones) { milestone in
                    MilestoneChip(milestone: milestone, track: entryTrack, distanceMeters: distanceMeters, laps: laps)
                }
            }
        }
    }

    private var cumulativePanel: some View {
        let totals = cumulativeTotals
        return VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .firstTextBaseline) {
                Text(selectedTrack == nil ? "All Tracks Training" : "\(scopeTitle) Training")
                    .font(.title3.bold())
                Spacer()
                Text("\(selectedEntries.count) saved runs")
                    .foregroundStyle(.secondary)
            }

            HStack(spacing: 12) {
                ValueTile(title: "Total Laps", value: LoopMeasurement.lapsString(totals.totalLaps))
                ValueTile(title: "Total Miles", value: totals.distanceSummary)
                ValueTile(title: "Total Time", value: durationString(totals.totalElapsedSeconds))
                ValueTile(title: "Average Pace", value: paceString(secondsPerMile: totals.averagePaceSecondsPerMile))
            }

            HStack(spacing: 12) {
                ValueTile(title: "Total Gain", value: elevationGainString(feet: totals.totalElevationGainFeet))
                if let elevationSource = elevationSourceSummary {
                    ValueTile(title: "Elevation Source", value: elevationSource)
                }
            }

            HStack(spacing: 12) {
                ForEach(DistanceReference.references(forMeters: totals.totalMeters)) { reference in
                    ReferenceTile(reference: reference, totalMeters: totals.totalMeters)
                }
            }
        }
    }

    private var logTable: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Saved Runs")
                    .font(.title3.bold())
                Spacer()
                Button {
                    if let editableSelectedRun {
                        editingRunDraft = RunEditDraft(entry: editableSelectedRun)
                    }
                } label: {
                    Label("Edit Selected", systemImage: "pencil")
                }
                .labelStyle(.iconOnly)
                .buttonStyle(.borderless)
                .disabled(editableSelectedRun == nil)
                .help("Edit selected run")
                Button {
                    selectedRuns.forEach { store.delete($0) }
                    selectedRunIDs.removeAll()
                } label: {
                    Label("Delete Selected", systemImage: "trash")
                }
                .labelStyle(.iconOnly)
                .buttonStyle(.borderless)
                .disabled(selectedRuns.isEmpty)
                .help("Delete selected run")
            }

            Table(selectedEntries, selection: $selectedRunIDs) {
                TableColumn("Date") { entry in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(entry.date.formatted(date: .abbreviated, time: .omitted))
                        if selectedTrack == nil {
                            Text(track(for: entry).name)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .width(min: 110, ideal: 130, max: 150)
                TableColumn("Laps") { entry in
                    Text(LoopMeasurement.lapsString(entry.laps))
                }
                .width(min: 54, ideal: 64, max: 76)
                TableColumn("Distance") { entry in
                    let miles = track(for: entry).distanceMiles(forLaps: entry.laps)
                    Text("\(miles, specifier: "%.3f") mi")
                }
                .width(min: 90, ideal: 108, max: 122)
                TableColumn("Gain") { entry in
                    Text(elevationGainString(feet: track(for: entry).elevationGainFeet(forLaps: entry.laps)))
                }
                .width(min: 72, ideal: 82, max: 96)
                TableColumn("Elapsed") { entry in
                    Text(durationString(entry.elapsedSeconds))
                }
                .width(min: 78, ideal: 92, max: 106)
                TableColumn("Pace") { entry in
                    Text(paceString(laps: entry.laps, elapsedSeconds: entry.elapsedSeconds, track: track(for: entry)))
                }
                .width(min: 86, ideal: 104, max: 116)
            }
        }
    }
}

private struct DurationInput: View {
    @Binding var hours: Int
    @Binding var minutes: Int
    @Binding var seconds: Int

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            DurationUnitField(title: "hr", value: $hours, range: 0...24)
            DurationUnitField(title: "min", value: $minutes, range: 0...59)
            DurationUnitField(title: "sec", value: $seconds, range: 0...59)
        }
        .accessibilityElement(children: .contain)
    }
}

private struct DurationUnitField: View {
    let title: String
    @Binding var value: Int
    let range: ClosedRange<Int>

    private var clampedValue: Binding<Int> {
        Binding {
            value.clamped(to: range)
        } set: { newValue in
            value = newValue.clamped(to: range)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            TextField("0", value: clampedValue, format: .number)
                .textFieldStyle(.roundedBorder)
                .monospacedDigit()
                .frame(width: 56)
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

private struct RunEditDraft: Identifiable {
    let entryID: RunEntry.ID
    var trackID: String
    var date: Date
    var laps: Double
    var elapsedHours: Int
    var elapsedMinutes: Int
    var elapsedSecondsPart: Int
    var note: String
    var source: String?

    var id: RunEntry.ID {
        entryID
    }

    var elapsedSeconds: Double? {
        let totalSeconds = elapsedHours * 3600 + elapsedMinutes * 60 + elapsedSecondsPart
        return totalSeconds > 0 ? Double(totalSeconds) : nil
    }

    init(entry: RunEntry) {
        self.entryID = entry.id
        self.trackID = entry.trackID
        self.date = entry.date
        self.laps = entry.laps
        let wholeSeconds = max(0, Int((entry.elapsedSeconds ?? 0).rounded()))
        self.elapsedHours = wholeSeconds / 3600
        self.elapsedMinutes = (wholeSeconds % 3600) / 60
        self.elapsedSecondsPart = wholeSeconds % 60
        self.note = entry.note
        self.source = entry.source
    }
}

private struct RunEditSheet: View {
    @State private var draft: RunEditDraft

    let onCancel: () -> Void
    let onSave: (RunEditDraft) -> Void

    init(draft: RunEditDraft, onCancel: @escaping () -> Void, onSave: @escaping (RunEditDraft) -> Void) {
        _draft = State(initialValue: draft)
        self.onCancel = onCancel
        self.onSave = onSave
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Edit Run")
                    .font(.title2.bold())
                Text(sourceLabel)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 14) {
                GridRow {
                    Text("Track")
                        .foregroundStyle(.secondary)
                    Picker("Track", selection: $draft.trackID) {
                        ForEach(TrackCatalog.tracks) { track in
                            Text(track.name).tag(track.id)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                GridRow {
                    Text("Date")
                        .foregroundStyle(.secondary)
                    DatePicker("Date", selection: $draft.date, displayedComponents: [.date, .hourAndMinute])
                        .labelsHidden()
                }

                GridRow {
                    Text("Laps")
                        .foregroundStyle(.secondary)
                    HStack {
                        TextField("0", value: $draft.laps, format: .number.precision(.fractionLength(0...2)))
                            .textFieldStyle(.roundedBorder)
                        Stepper("", value: $draft.laps, in: 0...300, step: 1)
                            .labelsHidden()
                    }
                }

                GridRow {
                    Text("Elapsed")
                        .foregroundStyle(.secondary)
                    DurationInput(hours: $draft.elapsedHours, minutes: $draft.elapsedMinutes, seconds: $draft.elapsedSecondsPart)
                }

                GridRow {
                    Text("Note")
                        .foregroundStyle(.secondary)
                    TextField("Optional", text: $draft.note, axis: .vertical)
                        .textFieldStyle(.roundedBorder)
                        .lineLimit(3, reservesSpace: true)
                }
            }

            HStack {
                Spacer()
                Button("Cancel") {
                    onCancel()
                }
                .keyboardShortcut(.cancelAction)

                Button("Save") {
                    onSave(draft)
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(draft.laps <= 0)
            }
        }
        .padding(20)
        .frame(width: 460)
    }

    private var sourceLabel: String {
        draft.source == "apple_health_export" ? "Apple Health import" : "Manual entry"
    }
}

private struct TrackSidebarRow: View {
    let track: Track
    let runCount: Int

    var body: some View {
        HStack(spacing: 10) {
            TrackGeometryIcon(points: track.iconPoints)

            VStack(alignment: .leading, spacing: 3) {
                Text(track.name)
                    .font(.headline)
                    .lineLimit(1)
                Text(track.location)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text("\(track.lapLengthMeters, specifier: "%.1f") m lap, \(runCount) \(runCount == 1 ? "run" : "runs")")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 4)
    }
}

private struct AllTracksSidebarRow: View {
    let trackCount: Int
    let runCount: Int

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "square.grid.2x2")
                .font(.title3.weight(.medium))
                .foregroundStyle(Color.accentColor)
                .frame(width: 32, height: 32)

            VStack(alignment: .leading, spacing: 3) {
                Text("All Tracks")
                    .font(.headline)
                    .lineLimit(1)
                Text("\(trackCount) \(trackCount == 1 ? "track" : "tracks")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text("\(runCount) \(runCount == 1 ? "run" : "runs")")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 4)
    }
}

private struct TrackGeometryIcon: View {
    let points: [TrackIconPoint]

    var body: some View {
        GeometryReader { proxy in
            if points.count > 1 {
                let side = min(proxy.size.width, proxy.size.height)
                let rect = CGRect(
                    x: (proxy.size.width - side) / 2 + 3,
                    y: (proxy.size.height - side) / 2 + 3,
                    width: side - 6,
                    height: side - 6
                )

                Path { path in
                    let first = points[0]
                    path.move(to: point(first, in: rect))
                    for iconPoint in points.dropFirst() {
                        path.addLine(to: point(iconPoint, in: rect))
                    }
                }
                .stroke(
                    Color.accentColor,
                    style: StrokeStyle(lineWidth: 2.2, lineCap: .round, lineJoin: .round)
                )
            } else {
                Image(systemName: "oval")
                    .font(.title3.weight(.medium))
                    .foregroundStyle(Color.accentColor)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(width: 32, height: 32)
    }

    private func point(_ iconPoint: TrackIconPoint, in rect: CGRect) -> CGPoint {
        CGPoint(
            x: rect.minX + CGFloat(iconPoint.x) * rect.width,
            y: rect.minY + CGFloat(iconPoint.y) * rect.height
        )
    }
}

private enum DetailPage: String, CaseIterable, Identifiable {
    case log
    case graphs

    var id: Self { self }

    var title: String {
        switch self {
        case .log: "Log"
        case .graphs: "Graphs"
        }
    }

    var systemImage: String {
        switch self {
        case .log: "list.bullet.rectangle"
        case .graphs: "chart.xyaxis.line"
        }
    }
}

private extension Comparable {
    func clamped(to range: ClosedRange<Self>) -> Self {
        min(max(self, range.lowerBound), range.upperBound)
    }
}

private func durationString(_ seconds: Double?) -> String {
    guard let seconds, seconds > 0 else {
        return "-"
    }
    let total = Int(round(seconds))
    let hours = total / 3600
    let minutes = (total % 3600) / 60
    let remainingSeconds = total % 60
    if hours > 0 {
        return String(format: "%d:%02d:%02d", hours, minutes, remainingSeconds)
    }
    return String(format: "%d:%02d", minutes, remainingSeconds)
}

private func paceString(laps: Double, elapsedSeconds: Double?, track: Track) -> String {
    guard let pace = track.paceSecondsPerMile(laps: laps, elapsedSeconds: elapsedSeconds) else {
        return "-"
    }
    return paceString(secondsPerMile: pace)
}

private func paceString(secondsPerMile: Double?) -> String {
    guard let secondsPerMile else {
        return "-"
    }
    return durationString(secondsPerMile) + " / mi"
}

private func elevationGainString(feet: Double) -> String {
    guard feet > 0 else {
        return "-"
    }
    if feet >= 1_000 {
        return String(format: "%.1fk ft", feet / 1_000)
    }
    return String(format: "%.0f ft", feet)
}

private struct RunTotals {
    let entries: [RunEntry]

    var totalLaps: Double {
        entries.reduce(0) { $0 + $1.laps }
    }

    var totalMeters: Double {
        entries.reduce(0) { total, entry in
            total + track(for: entry).distanceMeters(forLaps: entry.laps)
        }
    }

    var totalKilometers: Double {
        totalMeters / 1_000
    }

    var totalMiles: Double {
        totalMeters / DistanceUnits.metersPerMile
    }

    var totalElevationGainFeet: Double {
        entries.reduce(0) { total, entry in
            total + track(for: entry).elevationGainFeet(forLaps: entry.laps)
        }
    }

    var distanceSummary: String {
        String(format: "%.1f mi", totalMiles)
    }

    var totalElapsedSeconds: Double? {
        let total = entries.reduce(0.0) { $0 + ($1.elapsedSeconds ?? 0) }
        return total > 0 ? total : nil
    }

    var averagePaceSecondsPerMile: Double? {
        let timedEntries = entries.filter { ($0.elapsedSeconds ?? 0) > 0 && $0.laps > 0 }
        let totalSeconds = timedEntries.reduce(0.0) { $0 + ($1.elapsedSeconds ?? 0) }
        let totalMiles = timedEntries.reduce(0.0) { total, entry in
            total + track(for: entry).distanceMiles(forLaps: entry.laps)
        }
        guard totalSeconds > 0, totalMiles > 0 else {
            return nil
        }
        return totalSeconds / totalMiles
    }

    private func track(for entry: RunEntry) -> Track {
        TrackCatalog.track(for: entry.trackID) ?? TrackCatalog.defaultTrack
    }
}

private struct DistanceReference: Identifiable {
    let id: String
    let title: String
    let meters: Double
    let detail: String

    static func references(forMeters totalMeters: Double) -> [DistanceReference] {
        guard totalMeters > 0 else {
            return Array(referenceLadder.prefix(4))
        }
        let visible = referenceLadder
            .map { reference in
                (reference: reference, multiple: totalMeters / reference.meters)
            }
            .filter { item in
                item.multiple >= 0.1 && item.multiple <= 1.1
            }
            .sorted { lhs, rhs in
                abs(log(lhs.multiple)) < abs(log(rhs.multiple))
            }
            .prefix(4)
            .map(\.reference)
        return Array(visible)
    }

    func multipleString(for totalMeters: Double) -> String {
        let multiple = totalMeters / meters
        if multiple >= 100 {
            return String(format: "%.0fx", multiple)
        }
        if multiple >= 10 {
            return String(format: "%.1fx", multiple)
        }
        if multiple >= 1 {
            return String(format: "%.2fx", multiple)
        }
        return String(format: "%.3fx", multiple)
    }

    private static let referenceLadder = [
        DistanceReference(id: "five_k", title: "5K", meters: 5_000, detail: "3.1 mi"),
        DistanceReference(id: "ten_k", title: "10K", meters: 10_000, detail: "6.2 mi"),
        DistanceReference(id: "half_marathon", title: "Half Marathon", meters: 21_097.5, detail: "13.1 mi"),
        DistanceReference(id: "marathon", title: "Marathon", meters: 42_195, detail: "26.2 mi"),
        DistanceReference(id: "hundred_k", title: "100K Ultra", meters: 100_000, detail: "62 mi"),
        DistanceReference(id: "lake_tahoe_loop", title: "Lake Tahoe Loop", meters: 116_000, detail: "72 mi"),
        DistanceReference(id: "philly_nyc", title: "Philadelphia to NYC", meters: 153_000, detail: "95 mi"),
        DistanceReference(id: "nyc_boston", title: "NYC to Boston", meters: 306_000, detail: "190 mi"),
        DistanceReference(id: "erie_canal", title: "Erie Canal", meters: 584_000, detail: "363 mi"),
        DistanceReference(id: "empire_state_trail", title: "Empire State Trail", meters: 1_210_000, detail: "750 mi"),
        DistanceReference(id: "appalachian_trail", title: "Appalachian Trail", meters: 3_524_000, detail: "2,190 mi"),
        DistanceReference(id: "us_coast_to_coast", title: "US Coast to Coast", meters: 4_500_000, detail: "2,800 mi"),
        DistanceReference(id: "earth_radius", title: "Earth Radius", meters: 6_371_000, detail: "3,959 mi"),
        DistanceReference(id: "earth_diameter", title: "Earth Diameter", meters: 12_742_000, detail: "7,918 mi"),
        DistanceReference(id: "earth_loop", title: "Earth Loop", meters: 40_075_017, detail: "24,901 mi"),
        DistanceReference(id: "moon_distance", title: "Moon Distance", meters: 384_400_000, detail: "238,855 mi")
    ]
}

private struct ReferenceTile: View {
    let reference: DistanceReference
    let totalMeters: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(reference.title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(reference.multipleString(for: totalMeters))
                .font(.system(.title3, design: .rounded).weight(.semibold))
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.75)
            Text(reference.detail)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct DistanceTile: View {
    let title: String
    let value: Double
    let suffix: String
    let digits: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(formatted)
                .font(.system(.title3, design: .rounded).weight(.semibold))
                .monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
    }

    private var formatted: String {
        String(format: "%.\(digits)f %@", value, suffix)
    }
}

private struct ValueTile: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.system(.title3, design: .rounded).weight(.semibold))
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.75)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
    }
}

private struct MilestoneChip: View {
    let milestone: Milestone
    let track: Track
    let distanceMeters: Double
    let laps: Double

    private var isComplete: Bool {
        distanceMeters >= milestone.meters
    }

    private var remainingMeters: Double {
        max(0, milestone.meters - distanceMeters)
    }

    private var remainingLaps: Double {
        remainingMeters / track.lapLengthMeters
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(milestone.label)
                .font(.headline)
            Text(isComplete ? "reached" : "\(remainingLaps, specifier: "%.2f") laps left")
                .font(.caption)
                .foregroundStyle(isComplete ? .primary : .secondary)
            Text("at \(milestone.firstWholeLapAtOrOver(for: track)) laps")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(chipColor.opacity(isComplete ? 0.20 : 0.08), in: RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(chipColor.opacity(isComplete ? 0.90 : 0.22), lineWidth: isComplete ? 1.5 : 1)
        )
    }

    private var chipColor: Color {
        Color(hex: milestone.colorHex)
    }
}

private extension Color {
    init(hex: Int) {
        let red = Double((hex >> 16) & 0xff) / 255
        let green = Double((hex >> 8) & 0xff) / 255
        let blue = Double(hex & 0xff) / 255
        self.init(red: red, green: green, blue: blue)
    }
}
