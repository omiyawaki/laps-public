import AppKit
import Charts
import LapsCore
import SwiftUI

struct RunChartsView: View {
    let entries: [RunEntry]
    let selectedTrack: Track?

    @State private var metric: RunChartMetric = .laps
    @State private var selectedDate: Date?
    @State private var visibleDateRange: ClosedRange<Date>?

    private var points: [RunChartPoint] {
        entries.compactMap { entry in
            guard let value = activeMetric.value(for: entry, track: track(for: entry)) else {
                return nil
            }
            return RunChartPoint(entry: entry, value: value)
        }
        .sorted { $0.date < $1.date }
    }

    private var availableMetrics: [RunChartMetric] {
        RunChartMetric.available(forAllTracks: selectedTrack == nil)
    }

    private var selectedPoint: RunChartPoint? {
        guard !points.isEmpty else {
            return nil
        }
        guard let selectedDate else {
            return points.last
        }
        return points.min {
            abs($0.date.timeIntervalSince(selectedDate)) < abs($1.date.timeIntervalSince(selectedDate))
        }
    }

    private var activeMetric: RunChartMetric {
        availableMetrics.contains(metric) ? metric : availableMetrics[0]
    }

    private var metricSelection: Binding<RunChartMetric> {
        Binding {
            activeMetric
        } set: { newMetric in
            metric = newMetric
        }
    }

    var body: some View {
        GeometryReader { geometry in
            content(chartHeight: chartHeight(for: geometry.size.height))
        }
    }

    private func chartHeight(for availableHeight: CGFloat) -> CGFloat {
        max(300, min(520, availableHeight - 330))
    }

    private func content(chartHeight: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                Text("\(selectedTrack?.name ?? "All Tracks") Graphs")
                    .font(.title3.bold())
                Spacer()
                Text("\(points.count) runs with \(activeMetric.label.lowercased()) data")
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Button {
                    visibleDateRange = nil
                } label: {
                    Label("Reset Zoom", systemImage: "arrow.up.left.and.down.right")
                }
                .buttonStyle(.glass)
                .buttonBorderShape(.roundedRectangle(radius: 8))
                .disabled(visibleDateRange == nil)
            }

            HStack(spacing: 12) {
                Text("Metric")
                    .font(.headline)
                    .foregroundStyle(.secondary)
                Picker("Metric", selection: metricSelection) {
                    ForEach(availableMetrics) { metric in
                        Text(metric.label).tag(metric)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 500)
                .onChange(of: metric) {
                    selectedDate = nil
                    visibleDateRange = nil
                }
            }
            .onChange(of: selectedTrack?.id) {
                if !availableMetrics.contains(metric) {
                    metric = availableMetrics[0]
                }
                selectedDate = nil
                visibleDateRange = nil
            }

            GlassEffectContainer(spacing: 12) {
                HStack(spacing: 12) {
                    RunChartStatTile(title: "Selected", value: selectedPoint.map { activeMetric.formatted($0.value) } ?? "-")
                    RunChartStatTile(title: "Latest", value: points.last.map { activeMetric.formatted($0.value) } ?? "-")
                    RunChartStatTile(title: activeMetric.bestTitle, value: bestValue)
                    RunChartStatTile(title: "Total", value: totalValue)
                }
            }

            if points.isEmpty {
                ContentUnavailableView("No Data", systemImage: "chart.xyaxis.line", description: Text(activeMetric.emptyMessage))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                chart
                    .frame(height: chartHeight)

                if let selectedPoint {
                    selectedRunSummary(selectedPoint)
                }
            }
        }
    }

    private var chart: some View {
        Chart {
            ForEach(points) { point in
                LineMark(
                    x: .value("Date", point.date),
                    y: .value(activeMetric.label, point.value)
                )
                .foregroundStyle(activeMetric.tint)
                .interpolationMethod(.catmullRom)

                PointMark(
                    x: .value("Date", point.date),
                    y: .value(activeMetric.label, point.value)
                )
                .foregroundStyle(activeMetric.tint)
                .symbolSize(point.id == selectedPoint?.id ? 72 : 26)
            }

            ForEach(referenceMarks) { reference in
                RuleMark(y: .value(reference.label, reference.value))
                    .foregroundStyle(reference.color.opacity(0.35))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 4]))
                    .annotation(position: .overlay, alignment: .leading) {
                        Text(reference.label)
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(reference.color)
                            .padding(.leading, 4)
                    }
            }

            if let selectedPoint {
                RuleMark(x: .value("Selected date", selectedPoint.date))
                    .foregroundStyle(.secondary.opacity(0.45))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 3]))

                PointMark(
                    x: .value("Selected date", selectedPoint.date),
                    y: .value(activeMetric.label, selectedPoint.value)
                )
                .foregroundStyle(activeMetric.tint)
                .symbolSize(120)
            }
        }
        .chartXScale(domain: activeDateRange ?? Date()...Date().addingTimeInterval(86_400))
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 7))
        }
        .chartYAxis {
            AxisMarks(values: .automatic(desiredCount: 6))
        }
        .chartYAxisLabel(activeMetric.yAxisLabel)
        .chartOverlay { proxy in
            GeometryReader { geometry in
                if let plotFrame = proxy.plotFrame {
                    let frame = geometry[plotFrame]
                    ChartInteractionOverlay(
                        onSelect: { normalizedX in
                            selectDate(at: normalizedX)
                        },
                        onScroll: { scrollDelta, normalizedX, width in
                            panDateRange(scrollDelta: scrollDelta, normalizedX: normalizedX, plotWidth: width)
                        },
                        onMagnify: { magnification, normalizedX in
                            zoomDateRange(magnification: magnification, normalizedX: normalizedX)
                        }
                    )
                    .frame(width: frame.width, height: frame.height)
                    .position(x: frame.midX, y: frame.midY)
                }
            }
        }
    }

    private var fullDateRange: ClosedRange<Date>? {
        guard let first = points.first?.date, let last = points.last?.date else {
            return nil
        }
        let span = max(last.timeIntervalSince(first), 86_400)
        let padding = max(span * 0.04, 43_200)
        return first.addingTimeInterval(-padding)...last.addingTimeInterval(padding)
    }

    private var activeDateRange: ClosedRange<Date>? {
        visibleDateRange ?? fullDateRange
    }

    private func selectDate(at normalizedX: CGFloat) {
        guard let date = date(at: normalizedX) else {
            return
        }
        selectedDate = date
    }

    private func date(at normalizedX: CGFloat) -> Date? {
        guard let domain = activeDateRange else {
            return nil
        }
        let clamped = min(max(Double(normalizedX), 0), 1)
        let span = domain.upperBound.timeIntervalSince(domain.lowerBound)
        return domain.lowerBound.addingTimeInterval(span * clamped)
    }

    private func panDateRange(scrollDelta: CGFloat, normalizedX: Double, plotWidth: CGFloat) {
        guard let domain = activeDateRange, plotWidth > 1 else {
            return
        }
        let span = domain.upperBound.timeIntervalSince(domain.lowerBound)
        let shift = -TimeInterval(scrollDelta / plotWidth) * span
        setVisibleDateRange(
            start: domain.lowerBound.addingTimeInterval(shift),
            span: span
        )
        selectDate(at: normalizedX)
    }

    private func zoomDateRange(magnification: CGFloat, normalizedX: Double) {
        guard let domain = activeDateRange else {
            return
        }
        let span = domain.upperBound.timeIntervalSince(domain.lowerBound)
        let zoomFactor = max(0.25, min(4, 1 + Double(magnification) * 2.5))
        let newSpan = span / zoomFactor
        let anchorFraction = min(max(Double(normalizedX), 0), 1)
        let anchor = domain.lowerBound.addingTimeInterval(span * anchorFraction)
        let newStart = anchor.addingTimeInterval(-newSpan * anchorFraction)
        setVisibleDateRange(start: newStart, span: newSpan)
        selectDate(at: normalizedX)
    }

    private func setVisibleDateRange(start: Date, span requestedSpan: TimeInterval) {
        guard let full = fullDateRange else {
            return
        }
        let fullStart = full.lowerBound
        let fullEnd = full.upperBound
        let fullSpan = fullEnd.timeIntervalSince(fullStart)
        let span = min(max(requestedSpan, 86_400), fullSpan)
        var startTime = start.timeIntervalSinceReferenceDate
        let minStart = fullStart.timeIntervalSinceReferenceDate
        let maxStart = fullEnd.timeIntervalSinceReferenceDate - span
        startTime = min(max(startTime, minStart), maxStart)
        let lower = Date(timeIntervalSinceReferenceDate: startTime)
        visibleDateRange = lower...lower.addingTimeInterval(span)
    }

    private var referenceMarks: [RunChartReferenceMark] {
        guard let selectedTrack else {
            return []
        }
        switch activeMetric {
        case .laps:
            return selectedTrack.milestones.map {
                RunChartReferenceMark(label: $0.label, value: Double($0.firstWholeLapAtOrOver(for: selectedTrack)), color: Color(hex: $0.colorHex))
            }
        case .distance:
            return selectedTrack.milestones.map {
                RunChartReferenceMark(label: $0.label, value: $0.meters / DistanceUnits.metersPerMile, color: Color(hex: $0.colorHex))
            }
        case .elapsed, .pace, .gain:
            return []
        }
    }

    private var bestValue: String {
        guard let value = activeMetric.bestValue(in: points.map(\.value)) else {
            return "-"
        }
        return activeMetric.formatted(value)
    }

    private var totalValue: String {
        switch activeMetric {
        case .laps, .distance, .elapsed, .gain:
            return activeMetric.formatted(points.reduce(0) { $0 + $1.value })
        case .pace:
            guard !points.isEmpty else {
                return "-"
            }
            let totalSeconds = points.reduce(0.0) { total, point in
                total + (point.entry.elapsedSeconds ?? 0)
            }
            let totalMiles = points.reduce(0.0) { total, point in
                total + track(for: point.entry).distanceMiles(forLaps: point.entry.laps)
            }
            guard totalSeconds > 0, totalMiles > 0 else {
                return "-"
            }
            return activeMetric.formatted(totalSeconds / totalMiles / 60)
        }
    }

    private func selectedRunSummary(_ point: RunChartPoint) -> some View {
        let pointTrack = track(for: point.entry)
        return HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(point.date.formatted(date: .abbreviated, time: .shortened))
                    .font(.headline)
                Text(sourceSummary(for: point.entry, track: pointTrack))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text("\(LoopMeasurement.lapsString(point.entry.laps)) laps")
                .font(.system(.body, design: .rounded).weight(.semibold))
                .monospacedDigit()
            Text("\(pointTrack.distanceMiles(forLaps: point.entry.laps), specifier: "%.2f") mi")
                .font(.system(.body, design: .rounded).weight(.semibold))
                .monospacedDigit()
            Text("\(pointTrack.elevationGainFeet(forLaps: point.entry.laps), specifier: "%.0f") ft gain")
                .font(.system(.body, design: .rounded).weight(.semibold))
                .monospacedDigit()
            Text(durationString(point.entry.elapsedSeconds))
                .font(.system(.body, design: .rounded).weight(.semibold))
                .monospacedDigit()
            Text(paceString(laps: point.entry.laps, elapsedSeconds: point.entry.elapsedSeconds, track: pointTrack))
                .font(.system(.body, design: .rounded).weight(.semibold))
                .monospacedDigit()
        }
        .padding(12)
        .lapsGlassCard()
    }

    private func track(for entry: RunEntry) -> Track {
        TrackCatalog.track(for: entry.trackID) ?? TrackCatalog.defaultTrack
    }

    private func sourceSummary(for entry: RunEntry, track: Track) -> String {
        let source = entry.source == "apple_health_export" ? "Apple Health import" : "Manual entry"
        guard selectedTrack == nil else {
            return source
        }
        return "\(track.name), \(source)"
    }
}

private enum RunChartMetric: String, CaseIterable, Identifiable {
    case laps
    case distance
    case elapsed
    case pace
    case gain

    var id: Self { self }

    static func available(forAllTracks: Bool) -> [RunChartMetric] {
        if forAllTracks {
            return [.distance, .elapsed, .pace, .gain]
        }
        return allCases
    }

    var label: String {
        switch self {
        case .laps: "Laps"
        case .distance: "Distance"
        case .elapsed: "Elapsed"
        case .pace: "Pace"
        case .gain: "Gain"
        }
    }

    var yAxisLabel: String {
        switch self {
        case .laps: "laps"
        case .distance: "miles"
        case .elapsed: "minutes"
        case .pace: "min / mi"
        case .gain: "feet"
        }
    }

    var bestTitle: String {
        switch self {
        case .laps, .distance, .elapsed, .gain: "Max"
        case .pace: "Best"
        }
    }

    var emptyMessage: String {
        switch self {
        case .elapsed, .pace:
            "Entries need elapsed time before this chart can be drawn."
        case .laps, .distance, .gain:
            "Add a run to start seeing trends."
        }
    }

    var tint: Color {
        switch self {
        case .laps: Color(red: 0.12, green: 0.46, blue: 0.74)
        case .distance: Color(red: 0.13, green: 0.56, blue: 0.36)
        case .elapsed: Color(red: 0.74, green: 0.36, blue: 0.10)
        case .pace: Color(red: 0.58, green: 0.22, blue: 0.64)
        case .gain: Color(red: 0.49, green: 0.43, blue: 0.21)
        }
    }

    func value(for entry: RunEntry, track: Track) -> Double? {
        switch self {
        case .laps:
            entry.laps
        case .distance:
            track.distanceMiles(forLaps: entry.laps)
        case .elapsed:
            entry.elapsedSeconds.map { $0 / 60 }
        case .pace:
            track.paceSecondsPerMile(laps: entry.laps, elapsedSeconds: entry.elapsedSeconds).map { $0 / 60 }
        case .gain:
            track.elevationGainFeet(forLaps: entry.laps)
        }
    }

    func bestValue(in values: [Double]) -> Double? {
        switch self {
        case .laps, .distance, .elapsed, .gain:
            values.max()
        case .pace:
            values.min()
        }
    }

    func formatted(_ value: Double) -> String {
        switch self {
        case .laps:
            LoopMeasurement.lapsString(value)
        case .distance:
            String(format: "%.2f mi", value)
        case .elapsed:
            durationString(value * 60)
        case .pace:
            durationString(value * 60) + " / mi"
        case .gain:
            String(format: "%.0f ft", value)
        }
    }
}

private struct RunChartPoint: Identifiable {
    let entry: RunEntry
    let value: Double

    var id: UUID { entry.id }
    var date: Date { entry.date }
}

private struct RunChartReferenceMark: Identifiable {
    let label: String
    let value: Double
    let color: Color

    var id: String { label }
}

private struct RunChartStatTile: View {
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
        .lapsGlassCard()
    }
}

private struct ChartInteractionOverlay: NSViewRepresentable {
    let onSelect: (CGFloat) -> Void
    let onScroll: (CGFloat, CGFloat, CGFloat) -> Void
    let onMagnify: (CGFloat, CGFloat) -> Void

    func makeNSView(context: Context) -> EventView {
        let view = EventView()
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor.clear.cgColor
        return view
    }

    func updateNSView(_ nsView: EventView, context: Context) {
        nsView.onSelect = onSelect
        nsView.onScroll = onScroll
        nsView.onMagnify = onMagnify
    }

    final class EventView: NSView {
        var onSelect: ((CGFloat) -> Void)?
        var onScroll: ((CGFloat, CGFloat, CGFloat) -> Void)?
        var onMagnify: ((CGFloat, CGFloat) -> Void)?

        override var acceptsFirstResponder: Bool {
            true
        }

        override func acceptsFirstMouse(for event: NSEvent?) -> Bool {
            true
        }

        override func mouseDown(with event: NSEvent) {
            window?.makeFirstResponder(self)
            onSelect?(normalizedX(for: event))
        }

        override func mouseDragged(with event: NSEvent) {
            onSelect?(normalizedX(for: event))
        }

        override func scrollWheel(with event: NSEvent) {
            let horizontal = event.scrollingDeltaX
            let vertical = event.scrollingDeltaY
            let delta = abs(horizontal) > abs(vertical) ? horizontal : vertical
            guard abs(delta) > 0.1, bounds.width > 1 else {
                return
            }
            onScroll?(delta, normalizedX(for: event), bounds.width)
        }

        override func magnify(with event: NSEvent) {
            guard abs(event.magnification) > 0.001 else {
                return
            }
            onMagnify?(event.magnification, normalizedX(for: event))
        }

        private func normalizedX(for event: NSEvent) -> CGFloat {
            guard bounds.width > 0 else {
                return 0
            }
            let point = convert(event.locationInWindow, from: nil)
            return min(max(point.x / bounds.width, 0), 1)
        }
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
    return durationString(pace) + " / mi"
}

private extension Color {
    init(hex: Int) {
        let red = Double((hex >> 16) & 0xff) / 255
        let green = Double((hex >> 8) & 0xff) / 255
        let blue = Double(hex & 0xff) / 255
        self.init(red: red, green: green, blue: blue)
    }
}
