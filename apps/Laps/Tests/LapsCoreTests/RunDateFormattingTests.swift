import Foundation
import Testing
@testable import LapsCore

struct RunDateFormattingTests {
    @Test func formatsSingleDigitMonthAndDayWithLeadingZeros() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!

        let date = try #require(calendar.date(from: DateComponents(year: 2026, month: 5, day: 8)))

        #expect(RunDateFormatting.displayString(for: date, timeZone: calendar.timeZone) == "05/08/2026")
    }

    @Test func parsesDisplayDateWhilePreservingOriginalTime() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!

        let original = try #require(
            calendar.date(from: DateComponents(year: 2026, month: 1, day: 2, hour: 14, minute: 35, second: 17))
        )

        let parsed = try #require(
            RunDateFormatting.date(fromDisplayString: "05/08/2026", preservingTimeFrom: original, calendar: calendar)
        )
        let components = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: parsed)

        #expect(components.year == 2026)
        #expect(components.month == 5)
        #expect(components.day == 8)
        #expect(components.hour == 14)
        #expect(components.minute == 35)
        #expect(components.second == 17)
    }
}
