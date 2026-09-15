import Foundation

/// Keep values aligned with `ArtifactProcessingState` in `symx/model.py`.
enum ProcessingState: String, CaseIterable, Codable, Hashable, Identifiable, Sendable {
  case indexed
  case indexedDuplicate = "indexed_duplicate"
  case indexedInvalid = "indexed_invalid"
  case mirrored
  case mirroringFailed = "mirroring_failed"
  case mirrorCorrupt = "mirror_corrupt"
  case deltaOTA = "delta_ota"
  case recoveryOTA = "recovery_ota"
  case unsupportedOTAPayload = "unsupported_ota_payload"
  case symbolsExtracted = "symbols_extracted"
  case symbolExtractionFailed = "symbol_extraction_failed"
  case ignored

  var id: Self { self }

  static let defaultFailures: Set<Self> = [
    .mirroringFailed, .mirrorCorrupt, .symbolExtractionFailed, .indexedInvalid,
  ]

  var title: String {
    rawValue.split(separator: "_").map(\.capitalized).joined(separator: " ")
  }

  var systemImage: String {
    switch self {
    case .symbolsExtracted: "checkmark.circle.fill"
    case .mirrored: "externaldrive.fill.badge.checkmark"
    case .indexed: "list.bullet.clipboard.fill"
    case .indexedDuplicate: "doc.on.doc.fill"
    case .deltaOTA, .recoveryOTA, .unsupportedOTAPayload, .ignored: "minus.circle.fill"
    case .indexedInvalid, .mirroringFailed, .mirrorCorrupt, .symbolExtractionFailed:
      "exclamationmark.triangle.fill"
    }
  }

  var isFailure: Bool { Self.defaultFailures.contains(self) }
}

struct SnapshotInfo: Sendable {
  let id: String
  let createdAt: Date?
  let workflowRunID: Int64?
  let workflowRunURL: URL?
  let ipswGeneration: Int64
  let otaGeneration: Int64
}

struct IPSWSource: Identifiable, Hashable, Sendable {
  var id: String { "\(artifactKey)::\(link.absoluteString)" }

  let lastModified: Date?
  let state: ProcessingState
  let platform: String
  let version: String
  let build: String
  let artifactKey: String
  let fileName: String
  let link: URL
  let sha1: String?
  let lastRun: Int64
  let mirrorPath: String?
}

struct OTAArtifact: Identifiable, Hashable, Sendable {
  var id: String { otaKey }

  let lastRun: Int64
  let lastModified: Date?
  let state: ProcessingState
  let platform: String
  let version: String
  let build: String
  let otaKey: String
  let artifactID: String
  let url: URL
  let hash: String
  let hashAlgorithm: String
  let downloadPath: String?
}

struct Snapshot: Sendable {
  let info: SnapshotInfo
  let ipswSources: [IPSWSource]
  let otaArtifacts: [OTAArtifact]
}

struct ArtifactFacetFilter: Equatable, Sendable {
  var platforms: Set<String> = []
  var versions: Set<String> = []
  var builds: Set<String> = []

  func matches(platform: String, version: String, build: String) -> Bool {
    (platforms.isEmpty || platforms.contains(platform))
      && (versions.isEmpty || versions.contains(version))
      && (builds.isEmpty || builds.contains(build))
  }
}

struct ArtifactFacetOptions: Equatable, Sendable {
  var platforms: [String] = []
  var versions: [String] = []
  var builds: [String] = []
}

enum ArtifactStore: Hashable, Sendable {
  case ipsw
  case ota
}

enum ArtifactFacet: Hashable, Sendable {
  case platform
  case version
  case build
}

struct IPSWSortComparator: SortComparator, Hashable, Sendable {
  enum Field: Hashable, Sendable {
    case state, platform, version, build, fileName, lastModified
  }

  let field: Field
  var order: SortOrder

  init(_ field: Field, order: SortOrder = .forward) {
    self.field = field
    self.order = order
  }

  func compare(_ lhs: IPSWSource, _ rhs: IPSWSource) -> ComparisonResult {
    let result: ComparisonResult =
      switch field {
      case .state: naturalCompare(lhs.state.rawValue, rhs.state.rawValue)
      case .platform: naturalCompare(lhs.platform, rhs.platform)
      case .version: naturalCompare(lhs.version, rhs.version)
      case .build: naturalCompare(lhs.build, rhs.build)
      case .fileName: naturalCompare(lhs.fileName, rhs.fileName)
      case .lastModified: compareOptionalDates(lhs.lastModified, rhs.lastModified)
      }
    return ordered(result)
  }

  private func ordered(_ result: ComparisonResult) -> ComparisonResult {
    order == .forward ? result : result.reversed
  }
}

struct OTASortComparator: SortComparator, Hashable, Sendable {
  enum Field: Hashable, Sendable {
    case state, platform, version, build, artifactID, lastModified, lastRun
  }

  let field: Field
  var order: SortOrder

  init(_ field: Field, order: SortOrder = .forward) {
    self.field = field
    self.order = order
  }

  func compare(_ lhs: OTAArtifact, _ rhs: OTAArtifact) -> ComparisonResult {
    let result: ComparisonResult =
      switch field {
      case .state: naturalCompare(lhs.state.rawValue, rhs.state.rawValue)
      case .platform: naturalCompare(lhs.platform, rhs.platform)
      case .version: naturalCompare(lhs.version, rhs.version)
      case .build: naturalCompare(lhs.build, rhs.build)
      case .artifactID: naturalCompare(lhs.artifactID, rhs.artifactID)
      case .lastModified: compareOptionalDates(lhs.lastModified, rhs.lastModified)
      case .lastRun:
        lhs.lastRun < rhs.lastRun
          ? .orderedAscending : lhs.lastRun > rhs.lastRun ? .orderedDescending : .orderedSame
      }
    return ordered(result)
  }

  private func ordered(_ result: ComparisonResult) -> ComparisonResult {
    order == .forward ? result : result.reversed
  }
}

private func naturalCompare(_ lhs: String, _ rhs: String) -> ComparisonResult {
  lhs.compare(rhs, options: [.caseInsensitive, .numeric])
}

private func compareOptionalDates(_ lhs: Date?, _ rhs: Date?) -> ComparisonResult {
  switch (lhs, rhs) {
  case (.none, .none): .orderedSame
  case (.none, .some): .orderedAscending
  case (.some, .none): .orderedDescending
  case (.some(let lhs), .some(let rhs)):
    lhs < rhs ? .orderedAscending : lhs > rhs ? .orderedDescending : .orderedSame
  }
}

extension ComparisonResult {
  fileprivate var reversed: ComparisonResult {
    switch self {
    case .orderedAscending: .orderedDescending
    case .orderedSame: .orderedSame
    case .orderedDescending: .orderedAscending
    }
  }
}

enum AdminSection: String, CaseIterable, Identifiable {
  case overview
  case ipsw
  case ota

  var id: Self { self }
  var title: String { rawValue.uppercased() }

  var systemImage: String {
    switch self {
    case .overview: "chart.bar.xaxis"
    case .ipsw: "shippingbox.fill"
    case .ota: "antenna.radiowaves.left.and.right"
    }
  }
}

enum SnapshotLoadError: LocalizedError {
  case missingManifest(URL)
  case invalidManifest(URL)
  case noActiveSnapshot
  case missingDatabase(URL)
  case sqlite(String)
  case unsupportedState(String)

  var errorDescription: String? {
    switch self {
    case .missingManifest(let url): "No admin snapshot manifest exists at \(url.path)."
    case .invalidManifest(let url): "The admin snapshot manifest at \(url.path) is invalid."
    case .noActiveSnapshot: "The admin cache has no active snapshot."
    case .missingDatabase(let url): "The active snapshot database is missing at \(url.path)."
    case .sqlite(let message): "Could not read the admin snapshot: \(message)"
    case .unsupportedState(let state):
      "The snapshot contains unknown processing state “\(state)”. Update the app model."
    }
  }
}
