import Foundation
import Observation

enum MigrationAction: String, Codable, CaseIterable, Hashable, Sendable {
  case queueMirror = "queue_mirror"
  case queueExtract = "queue_extract"

  var title: String {
    switch self {
    case .queueMirror: "Queue for mirroring"
    case .queueExtract: "Queue for extraction"
    }
  }
}

struct MigrationQueueKey: Identifiable, Hashable, Sendable {
  let store: ArtifactStore
  let action: MigrationAction

  var id: String { "\(store)-\(action.rawValue)" }
  var title: String { "\(store == .ipsw ? "IPSW" : "OTA") · \(action.title)" }
}

struct MigrationPreview: Sendable {
  let allowed: Bool
  let resultingState: ProcessingState?
  let note: String
}

struct MigrationQueueEntry: Identifiable, Hashable, Sendable {
  let id: String
  let label: String
  let currentState: ProcessingState
  let resultingState: ProcessingState
  let artifactKey: String?
  let link: String?
  let otaKey: String?
}

struct MigrationQueueGroup: Identifiable, Sendable {
  let key: MigrationQueueKey
  var entries: [MigrationQueueEntry]
  var reason = ""
  var isRunning = false
  var resultMessage: String?

  var id: String { key.id }
}

@MainActor
@Observable
final class MigrationQueueModel {
  private(set) var groups: [MigrationQueueKey: MigrationQueueGroup] = [:]
  private let diagnostics: DiagnosticsLog

  init(diagnostics: DiagnosticsLog) {
    self.diagnostics = diagnostics
  }

  var orderedGroups: [MigrationQueueGroup] {
    groups.values.sorted { $0.key.id < $1.key.id }
  }

  var count: Int { groups.values.reduce(0) { $0 + $1.entries.count } }

  func add(_ row: IPSWSource, action: MigrationAction) {
    let preview = migrationPreview(row, action: action)
    guard preview.allowed, let resultingState = preview.resultingState else { return }
    let key = MigrationQueueKey(store: .ipsw, action: action)
    let entry = MigrationQueueEntry(
      id: row.id,
      label: row.fileName,
      currentState: row.state,
      resultingState: resultingState,
      artifactKey: row.artifactKey,
      link: row.link.absoluteString,
      otaKey: nil
    )
    insert(entry, into: key)
  }

  func add(_ row: OTAArtifact, action: MigrationAction) {
    let preview = migrationPreview(row, action: action)
    guard preview.allowed, let resultingState = preview.resultingState else { return }
    let key = MigrationQueueKey(store: .ota, action: action)
    let entry = MigrationQueueEntry(
      id: row.id,
      label: row.artifactID,
      currentState: row.state,
      resultingState: resultingState,
      artifactKey: nil,
      link: nil,
      otaKey: row.otaKey
    )
    insert(entry, into: key)
  }

  func remove(entryID: String, from key: MigrationQueueKey) {
    guard var group = groups[key], !group.isRunning else { return }
    group.entries.removeAll { $0.id == entryID }
    if group.entries.isEmpty {
      groups.removeValue(forKey: key)
    } else {
      groups[key] = group
    }
    diagnostics.record("migration", "removed target queue=\(key.id)")
  }

  func setReason(_ reason: String, for key: MigrationQueueKey) {
    guard var group = groups[key] else { return }
    group.reason = reason
    groups[key] = group
  }

  func clear(_ key: MigrationQueueKey) {
    guard groups[key]?.isRunning != true else { return }
    groups.removeValue(forKey: key)
    diagnostics.record("migration", "cleared queue=\(key.id)")
  }

  func apply(_ key: MigrationQueueKey, snapshot: SnapshotInfo) async -> Bool {
    guard var group = groups[key], !group.entries.isEmpty,
      !group.reason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    else {
      return false
    }
    group.isRunning = true
    group.resultMessage = nil
    groups[key] = group
    diagnostics.record("migration", "dispatch queue=\(key.id) targets=\(group.entries.count)")

    let submittedEntryIDs = Set(group.entries.map(\.id))
    let request = MigrationApplyRequest(
      store: key.store == .ipsw ? "ipsw" : "ota",
      action: key.action,
      snapshotID: snapshot.id,
      baseGeneration: key.store == .ipsw ? snapshot.ipswGeneration : snapshot.otaGeneration,
      targets: group.entries.map(MigrationRequestTarget.init),
      reason: group.reason.trimmingCharacters(in: .whitespacesAndNewlines)
    )
    do {
      let result = try await Task.detached { try MigrationApplyService().apply(request) }.value
      diagnostics.record(
        "migration", "result queue=\(key.id) status=\(result.status) message=\(result.message)")
      if migrationStatusChangedRemoteMetadata(result.status) {
        if let currentGroup = groups[key],
          let remainingGroup = migrationGroupAfterSuccessfulApply(
            currentGroup, submittedEntryIDs: submittedEntryIDs)
        {
          groups[key] = remainingGroup
        } else {
          groups.removeValue(forKey: key)
        }
        return true
      } else if let currentGroup = groups[key] {
        groups[key] = migrationGroupAfterFailedApply(currentGroup, message: result.message)
      }
      return false
    } catch {
      if let currentGroup = groups[key] {
        groups[key] = migrationGroupAfterFailedApply(
          currentGroup, message: error.localizedDescription)
      }
      diagnostics.record("migration", "failed queue=\(key.id): \(error.localizedDescription)")
      return false
    }
  }

  private func insert(_ entry: MigrationQueueEntry, into key: MigrationQueueKey) {
    var group = groups[key] ?? MigrationQueueGroup(key: key, entries: [])
    guard !group.entries.contains(where: { $0.id == entry.id }) else { return }
    group.entries.append(entry)
    groups[key] = group
    diagnostics.record(
      "migration",
      "added target queue=\(key.id) state=\(entry.currentState.rawValue)->\(entry.resultingState.rawValue)"
    )
  }
}

func migrationStatusChangedRemoteMetadata(_ status: String) -> Bool {
  status == "applied" || status == "applied_with_worker_warning"
}

func migrationGroupAfterSuccessfulApply(
  _ currentGroup: MigrationQueueGroup, submittedEntryIDs: Set<MigrationQueueEntry.ID>
) -> MigrationQueueGroup? {
  var group = currentGroup
  group.entries.removeAll { submittedEntryIDs.contains($0.id) }
  guard !group.entries.isEmpty else { return nil }
  group.isRunning = false
  group.resultMessage = nil
  return group
}

func migrationGroupAfterFailedApply(
  _ currentGroup: MigrationQueueGroup, message: String
) -> MigrationQueueGroup {
  var group = currentGroup
  group.isRunning = false
  group.resultMessage = message
  return group
}

func commonMigrationActions(for rows: [IPSWSource]) -> [MigrationAction] {
  guard !rows.isEmpty else { return [] }
  return MigrationAction.allCases.filter { action in
    rows.allSatisfy { migrationPreview($0, action: action).allowed }
  }
}

func commonMigrationActions(for rows: [OTAArtifact]) -> [MigrationAction] {
  guard !rows.isEmpty else { return [] }
  return MigrationAction.allCases.filter { action in
    rows.allSatisfy { migrationPreview($0, action: action).allowed }
  }
}

func migrationPreview(_ row: IPSWSource, action: MigrationAction) -> MigrationPreview {
  previewMigration(
    store: .ipsw, action: action, state: row.state, hasRequiredPath: row.mirrorPath != nil)
}

func migrationPreview(_ row: OTAArtifact, action: MigrationAction) -> MigrationPreview {
  previewMigration(
    store: .ota, action: action, state: row.state, hasRequiredPath: row.downloadPath != nil)
}

private func previewMigration(
  store: ArtifactStore,
  action: MigrationAction,
  state: ProcessingState,
  hasRequiredPath: Bool
) -> MigrationPreview {
  if state == .ignored {
    return MigrationPreview(
      allowed: false, resultingState: nil, note: "Ignored artifacts are excluded")
  }
  if store == .ota,
    state == .indexedDuplicate || state == .recoveryOTA
      || (state == .deltaOTA && action == .queueMirror)
  {
    return MigrationPreview(
      allowed: false, resultingState: nil, note: "This state is excluded from the curated action")
  }
  if action == .queueExtract, !hasRequiredPath {
    let field = store == .ipsw ? "mirror path" : "download path"
    return MigrationPreview(allowed: false, resultingState: nil, note: "A \(field) is required")
  }
  let resultingState: ProcessingState = action == .queueExtract ? .mirrored : .indexed
  return MigrationPreview(
    allowed: true,
    resultingState: resultingState,
    note: state == resultingState
      ? "Already eligible; the last run will be refreshed"
      : "Will set state to \(resultingState.rawValue)"
  )
}

private struct MigrationApplyRequest: Encodable, Sendable {
  let store: String
  let action: MigrationAction
  let snapshotID: String
  let baseGeneration: Int64
  let targets: [MigrationRequestTarget]
  let reason: String

  enum CodingKeys: String, CodingKey {
    case store, action, targets, reason
    case snapshotID = "snapshot_id"
    case baseGeneration = "base_generation"
  }
}

private enum MigrationRequestTarget: Encodable, Sendable {
  case ipsw(artifactKey: String, link: String)
  case ota(otaKey: String)

  init(_ entry: MigrationQueueEntry) {
    if let artifactKey = entry.artifactKey, let link = entry.link {
      self = .ipsw(artifactKey: artifactKey, link: link)
    } else {
      self = .ota(otaKey: entry.otaKey ?? "")
    }
  }

  func encode(to encoder: Encoder) throws {
    var container = encoder.container(keyedBy: CodingKeys.self)
    switch self {
    case .ipsw(let artifactKey, let link):
      try container.encode(artifactKey, forKey: .artifactKey)
      try container.encode(link, forKey: .link)
    case .ota(let otaKey):
      try container.encode(otaKey, forKey: .otaKey)
    }
  }

  private enum CodingKeys: String, CodingKey {
    case link
    case artifactKey = "artifact_key"
    case otaKey = "ota_key"
  }
}

private struct MigrationApplyResult: Decodable, Sendable {
  let status: String
  let message: String
}

private struct MigrationApplyService: Sendable {
  func apply(_ request: MigrationApplyRequest) throws -> MigrationApplyResult {
    guard let root = CommandEnvironment.projectRoot() else {
      throw MigrationServiceError.projectRootNotFound
    }
    guard let uv = CommandEnvironment.executable(named: "uv") else {
      throw MigrationServiceError.uvNotFound
    }
    let directory = FileManager.default.temporaryDirectory.appending(
      path: UUID().uuidString, directoryHint: .isDirectory)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let requestURL = directory.appending(path: "request.json")
    let resultURL = directory.appending(path: "result.json")
    try JSONEncoder().encode(request).write(to: requestURL)

    let process = Process()
    let output = Pipe()
    process.executableURL = uv
    process.currentDirectoryURL = root
    process.arguments = [
      "run", "--project", root.path, "symx", "admin", "dispatch-batch",
      "--request-path", requestURL.path, "--result-path", resultURL.path,
    ]
    process.standardOutput = output
    process.standardError = output
    try process.run()
    // Drain the pipe while the child runs; waiting first can deadlock if the pipe fills.
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else {
      throw MigrationServiceError.commandFailed(String(decoding: data, as: UTF8.self))
    }
    return try JSONDecoder().decode(MigrationApplyResult.self, from: Data(contentsOf: resultURL))
  }
}

private enum MigrationServiceError: LocalizedError {
  case projectRootNotFound, uvNotFound
  case commandFailed(String)

  var errorDescription: String? {
    switch self {
    case .projectRootNotFound: "Could not locate the symx checkout."
    case .uvNotFound: "Could not locate uv."
    case .commandFailed(let message):
      "Migration dispatch failed: \(message.trimmingCharacters(in: .whitespacesAndNewlines))"
    }
  }
}
