import Foundation
import Observation

struct WorkflowTask: Decodable, Identifiable, Sendable {
  let databaseId: Int64
  let status: String
  let conclusion: String?
  let url: URL
  let startedAt: Date?
  let updatedAt: Date?
  let displayTitle: String
  let workflowName: String

  var id: Int64 { databaseId }
  var isActive: Bool { status != "completed" }

  enum CodingKeys: String, CodingKey {
    case databaseId, status, conclusion, url, startedAt, updatedAt, displayTitle, workflowName
  }
}

@MainActor
@Observable
final class WorkflowTasksModel {
  private(set) var tasks: [WorkflowTask] = []
  private(set) var isRefreshing = false
  private(set) var errorMessage: String?
  private let diagnostics: DiagnosticsLog

  init(diagnostics: DiagnosticsLog) {
    self.diagnostics = diagnostics
  }

  func refresh() async {
    guard !isRefreshing else { return }
    isRefreshing = true
    errorMessage = nil
    do {
      let loaded = try await Task.detached { try WorkflowTaskService().load() }.value
      tasks = loaded
      diagnostics.record(
        "workflows",
        "refreshed active=\(loaded.count(where: \.isActive)) finished=\(loaded.count(where: { !$0.isActive }))"
      )
    } catch {
      errorMessage = error.localizedDescription
      diagnostics.record("workflows", "refresh failed: \(error.localizedDescription)")
    }
    isRefreshing = false
  }
}

private struct WorkflowTaskService: Sendable {
  private let names: Set<String> = [
    "Mirror IPSW artifacts",
    "Extract IPSW symbols",
    "Mirror OTA images",
    "Extract OTA symbols",
    "Sync admin meta cache",
    "Apply admin rerun batch",
  ]

  func load() throws -> [WorkflowTask] {
    guard let gh = CommandEnvironment.executable(named: "gh") else {
      throw WorkflowTaskError.ghNotFound
    }
    guard let root = CommandEnvironment.projectRoot() else {
      throw WorkflowTaskError.projectRootNotFound
    }
    let process = Process()
    let output = Pipe()
    process.executableURL = gh
    process.currentDirectoryURL = root
    process.arguments = [
      "run", "list", "--limit", "200", "--json",
      "databaseId,status,conclusion,url,startedAt,updatedAt,displayTitle,workflowName",
    ]
    process.standardOutput = output
    process.standardError = output
    try process.run()
    // Drain the pipe while the child runs; waiting first can deadlock if the pipe fills.
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else {
      throw WorkflowTaskError.commandFailed(String(decoding: data, as: UTF8.self))
    }
    let decoder = JSONDecoder()
    decoder.dateDecodingStrategy = .iso8601
    let relevant = try decoder.decode([WorkflowTask].self, from: data)
      .filter { names.contains($0.workflowName) }
      .sorted { ($0.startedAt ?? .distantPast) > ($1.startedAt ?? .distantPast) }
    let active = relevant.filter(\.isActive)
    return active + Array(relevant.filter { !$0.isActive }.prefix(50))
  }
}

private enum WorkflowTaskError: LocalizedError {
  case ghNotFound, projectRootNotFound
  case commandFailed(String)

  var errorDescription: String? {
    switch self {
    case .ghNotFound: "Could not locate gh."
    case .projectRootNotFound: "Could not locate the symx checkout."
    case .commandFailed(let message):
      "Could not list workflow runs: \(message.trimmingCharacters(in: .whitespacesAndNewlines))"
    }
  }
}
