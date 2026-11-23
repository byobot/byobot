// Ids and times in the contract's formats.
//
// Copyright The byobot Authors. Licensed under Apache-2.0.

import Foundation

/// Time-ordered UUIDs (RFC 9562 version 7), lowercase. Ids made in the same millisecond still sort in
/// the order they were made, because events with equal times from one device apply in id order.
public enum UUID7 {
    private final class State: @unchecked Sendable {
        let lock = NSLock()
        var lastMs: UInt64 = 0
        var counter: UInt16 = 0
    }

    private static let state = State()

    public static func make(at date: Date = Date()) -> String {
        var bytes = [UInt8](repeating: 0, count: 16)
        var rng = SystemRandomNumberGenerator()
        for i in 0..<16 { bytes[i] = UInt8.random(in: 0...255, using: &rng) }

        state.lock.lock()
        var ms = Swift.max(UInt64(Swift.max(0, date.timeIntervalSince1970) * 1000), state.lastMs)
        var counter: UInt16
        if ms == state.lastMs {
            counter = state.counter + 1
            if counter > 0xFFF {  // counter spent: borrow the next millisecond
                ms += 1
                counter = 0
            }
        } else {
            counter = (UInt16(bytes[6] & 0x07) << 8) | UInt16(bytes[7])  // start low so the counter has room
        }
        state.lastMs = ms
        state.counter = counter
        state.lock.unlock()

        for i in 0..<6 { bytes[i] = UInt8((ms >> UInt64(8 * (5 - i))) & 0xFF) }
        bytes[6] = 0x70 | UInt8((counter >> 8) & 0x0F)
        bytes[7] = UInt8(counter & 0xFF)
        bytes[8] = 0x80 | (bytes[8] & 0x3F)
        let hex = bytes.map { String(format: "%02x", $0) }.joined()
        let parts = [hex.prefix(8), hex.dropFirst(8).prefix(4), hex.dropFirst(12).prefix(4),
                     hex.dropFirst(16).prefix(4), hex.dropFirst(20)]
        return parts.joined(separator: "-")
    }
}

/// RFC 3339 timestamps and calendar dates as the contract writes them.
public enum Timestamp {
    private static func formatter() -> ISO8601DateFormatter {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        f.timeZone = TimeZone(identifier: "UTC")
        return f
    }

    /// UTC with milliseconds, for example `2026-11-21T03:26:09.123Z`.
    public static func string(_ date: Date) -> String {
        formatter().string(from: date)
    }

    public static func now() -> String {
        string(Date())
    }

    /// Parse any contract timestamp (any offset, with or without a fraction).
    public static func date(_ text: String) -> Date? {
        if let d = formatter().date(from: text) { return d }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: text)
    }
}

/// `YYYY-MM-DD` in the phone's own calendar, as plans schedule sessions.
public enum LocalDate {
    public static func string(_ date: Date, calendar: Calendar = .current) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    public static func today(calendar: Calendar = .current) -> String {
        string(Date(), calendar: calendar)
    }

    public static func adding(days: Int, to date: Date = Date(), calendar: Calendar = .current) -> String {
        string(calendar.date(byAdding: .day, value: days, to: date) ?? date, calendar: calendar)
    }

    /// The date at noon in the given calendar, for display.
    public static func date(_ text: String, calendar: Calendar = .current) -> Date? {
        let parts = text.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2], hour: 12))
    }
}
