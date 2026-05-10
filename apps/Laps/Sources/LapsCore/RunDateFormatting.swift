import Foundation

public enum RunDateFormatting {
    public static let displayFormat = "MM/dd/yyyy"

    public static func displayString(for date: Date, timeZone: TimeZone = .current) -> String {
        formatter(timeZone: timeZone).string(from: date)
    }

    public static func date(
        fromDisplayString string: String,
        preservingTimeFrom originalDate: Date,
        calendar: Calendar = .current
    ) -> Date? {
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let parsedDate = formatter(timeZone: calendar.timeZone).date(from: trimmed) else {
            return nil
        }

        var components = calendar.dateComponents([.year, .month, .day], from: parsedDate)
        let timeComponents = calendar.dateComponents([.hour, .minute, .second, .nanosecond], from: originalDate)
        components.hour = timeComponents.hour
        components.minute = timeComponents.minute
        components.second = timeComponents.second
        components.nanosecond = timeComponents.nanosecond
        return calendar.date(from: components)
    }

    private static func formatter(timeZone: TimeZone) -> DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        formatter.calendar = calendar
        formatter.timeZone = timeZone
        formatter.dateFormat = displayFormat
        formatter.isLenient = false
        return formatter
    }
}
