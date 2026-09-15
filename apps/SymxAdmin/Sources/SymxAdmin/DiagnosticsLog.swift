import Foundation
import Observation

struct DiagnosticEntry: Identifiable, Sendable {
  let id: Int
  let timestamp: Date
  let category: String
  let message: String

  var line: String {
    "[\(timestamp.formatted(date: .omitted, time: .standard))] [\(category)] \(message)"
  }
}

@MainActor
@Observable
final class DiagnosticsLog {
  private(set) var entries: [DiagnosticEntry] = []
  private var nextID = 0
  private let capacity = 1_000

  var text: String {
    entries.map(\.line).joined(separator: "\n")
  }

  func record(_ category: String, _ message: String) {
    nextID += 1
    entries.append(
      DiagnosticEntry(id: nextID, timestamp: .now, category: category, message: message))
    if entries.count > capacity {
      entries.removeFirst(entries.count - capacity)
    }
  }

  func clear() {
    entries.removeAll(keepingCapacity: true)
  }
}
