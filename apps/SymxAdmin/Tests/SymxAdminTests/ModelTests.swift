import Foundation
import SQLite3
import Testing

@testable import SymxAdmin

@Test func processingStatesStayAlignedWithPythonModel() {
  #expect(
    Set(ProcessingState.allCases.map(\.rawValue)) == [
      "indexed", "indexed_duplicate", "indexed_invalid", "mirrored", "mirroring_failed",
      "mirror_corrupt", "delta_ota", "recovery_ota", "unsupported_ota_payload",
      "symbols_extracted", "symbol_extraction_failed", "ignored",
    ])
}

@Test func defaultFailuresMatchAdminDatabaseContract() {
  #expect(
    ProcessingState.defaultFailures == [
      .mirroringFailed, .mirrorCorrupt, .symbolExtractionFailed, .indexedInvalid,
    ])
}

@Test func manifestDecodesPythonSnapshotIDKey() throws {
  let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
  defer { try? FileManager.default.removeItem(at: directory) }
  try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
  try Data(#"{"active_snapshot_id":"fixture-snapshot"}"#.utf8)
    .write(to: directory.appending(path: "manifest.json"))

  do {
    _ = try SnapshotRepository(cacheURL: directory).load()
    Issue.record("Expected the fixture database to be missing")
  } catch SnapshotLoadError.missingDatabase(let url) {
    #expect(url.path.hasSuffix("snapshots/fixture-snapshot/snapshot.db"))
  } catch {
    Issue.record("Unexpected error: \(error)")
  }
}

@Test func snapshotRepositoryLoadsCurrentTimestampColumns() throws {
  let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
  defer { try? FileManager.default.removeItem(at: directory) }
  let snapshotID = "fixture-snapshot"
  let snapshotDirectory = directory.appending(path: "snapshots/\(snapshotID)")
  try FileManager.default.createDirectory(at: snapshotDirectory, withIntermediateDirectories: true)
  try Data(#"{"active_snapshot_id":"fixture-snapshot"}"#.utf8)
    .write(to: directory.appending(path: "manifest.json"))

  let databaseURL = snapshotDirectory.appending(path: "snapshot.db")
  var databasePointer: OpaquePointer?
  #expect(sqlite3_open(databaseURL.path, &databasePointer) == SQLITE_OK)
  let database = try #require(databasePointer)
  defer { sqlite3_close(database) }

  let sql = """
    CREATE TABLE snapshot_info (
        id INTEGER PRIMARY KEY, snapshot_id TEXT, created_at TEXT, workflow_run_id INTEGER,
        workflow_run_url TEXT, ipsw_generation INTEGER, ota_generation INTEGER
    );
    INSERT INTO snapshot_info VALUES (
        1, 'fixture-snapshot', '2026-09-15T07:00:00+00:00', 123,
        'https://example.com/run/123', 101, 202
    );
    CREATE TABLE ipsw_artifacts (artifact_key TEXT, platform TEXT, version TEXT, build TEXT);
    CREATE TABLE ipsw_sources (
        last_modified TEXT, processing_state TEXT, artifact_key TEXT, file_name TEXT,
        link TEXT, sha1 TEXT, last_run INTEGER, mirror_path TEXT
    );
    INSERT INTO ipsw_artifacts VALUES ('ipsw-key', 'iOS', '26.7', '23H24');
    INSERT INTO ipsw_sources VALUES (
        '2026-09-15T06:30:00.123456', 'mirrored', 'ipsw-key', 'fixture.ipsw',
        'https://example.com/ipsw', 'def', 34938694500, 'gs://mirror/ipsw'
    );
    CREATE TABLE ota_artifacts (
        ota_key TEXT, build TEXT, description_json TEXT, version TEXT, platform TEXT,
        artifact_id TEXT, url TEXT, devices_json TEXT, hash TEXT, hash_algorithm TEXT,
        processing_state TEXT, download_path TEXT, last_run INTEGER, last_modified TEXT
    );
    INSERT INTO ota_artifacts VALUES (
        'ota-key', '23H24', '[]', '26.7', 'ios', 'ota-id',
        'https://example.com/ota', '[]', 'abc', 'sha256', 'mirrored',
        'gs://mirror/ota', 34938694543, '2026-09-15T06:56:19.228617+00:00'
    );
    """
  var sqliteError: UnsafeMutablePointer<CChar>?
  let result = sqlite3_exec(database, sql, nil, nil, &sqliteError)
  if result != SQLITE_OK {
    let message = sqliteError.map { String(cString: $0) } ?? "unknown SQLite fixture error"
    if let sqliteError { sqlite3_free(sqliteError) }
    Issue.record(Comment(rawValue: message))
    return
  }

  let snapshot = try SnapshotRepository(cacheURL: directory).load()

  #expect(snapshot.info.id == snapshotID)
  let ipsw = try #require(snapshot.ipswSources.first)
  let expectedIPSWTimestamp = try Date.ISO8601FormatStyle(includingFractionalSeconds: true)
    .parse("2026-09-15T06:30:00.123456Z")
  #expect(ipsw.lastModified == expectedIPSWTimestamp)

  let ota = try #require(snapshot.otaArtifacts.first)
  #expect(ota.lastRun == 34_938_694_543)
  let expectedOTATimestamp = try Date.ISO8601FormatStyle(includingFractionalSeconds: true)
    .parse("2026-09-15T06:56:19.228617+00:00")
  #expect(ota.lastModified == expectedOTATimestamp)
}

@Test func snapshotFilteringUsesStateAndSearchAcrossCompleteInput() throws {
  let ipsw = IPSWSource(
    lastModified: nil, state: .mirroringFailed, platform: "iOS", version: "26.0", build: "23A1",
    artifactKey: "ios-key", fileName: "iPhone.ipsw",
    link: try #require(URL(string: "https://example.com/a")),
    sha1: nil, lastRun: 1, mirrorPath: nil
  )
  let ota = OTAArtifact(
    lastRun: 2, lastModified: nil, state: .symbolsExtracted, platform: "macOS", version: "26.0",
    build: "25A1",
    otaKey: "mac-key", artifactID: "MacUpdate",
    url: try #require(URL(string: "https://example.com/b")),
    hash: "abc", hashAlgorithm: "sha256", downloadPath: "gs://example/b"
  )
  let unavailableOTA = OTAArtifact(
    lastRun: 3, lastModified: nil, state: .indexed, platform: "iOS", version: "18.0", build: "22A1",
    otaKey: "ios-key", artifactID: "iPhoneUpdate",
    url: try #require(URL(string: "https://example.com/c")),
    hash: "def", hashAlgorithm: "sha256", downloadPath: nil
  )
  let snapshot = Snapshot(
    info: SnapshotInfo(
      id: "fixture", createdAt: nil, workflowRunID: nil, workflowRunURL: nil,
      ipswGeneration: 1, otaGeneration: 2
    ),
    ipswSources: [ipsw], otaArtifacts: [ota, unavailableOTA]
  )

  let result = filterSnapshot(snapshot, states: [.symbolsExtracted], search: "Mac")

  #expect(result.ipsw.isEmpty)
  #expect(result.ipswOptions.platforms.isEmpty)
  #expect(result.ota == [ota])
  #expect(result.otaOptions.platforms == ["macOS"])
  #expect(result.otaOptions.versions == ["26.0"])
  #expect(result.otaOptions.builds == ["25A1"])

  let excluded = filterSnapshot(
    snapshot,
    states: [.symbolsExtracted],
    search: "",
    otaFacets: ArtifactFacetFilter(platforms: ["iOS"])
  )
  #expect(excluded.ota.isEmpty)
  #expect(excluded.otaOptions.platforms == ["macOS"])
}

@Test func tableComparatorsSortNaturallyInBothDirections() throws {
  let older = OTAArtifact(
    lastRun: 20, lastModified: Date(timeIntervalSince1970: 100),
    state: .symbolsExtracted, platform: "iOS", version: "9", build: "A",
    otaKey: "older", artifactID: "Older",
    url: try #require(URL(string: "https://example.com/older")),
    hash: "a", hashAlgorithm: "sha256", downloadPath: nil
  )
  let newer = OTAArtifact(
    lastRun: 10, lastModified: Date(timeIntervalSince1970: 200),
    state: .symbolsExtracted, platform: "iOS", version: "10", build: "B",
    otaKey: "newer", artifactID: "Newer",
    url: try #require(URL(string: "https://example.com/newer")),
    hash: "b", hashAlgorithm: "sha256", downloadPath: nil
  )

  #expect([newer, older].sorted(using: [OTASortComparator(.version)]) == [older, newer])
  #expect(
    [older, newer].sorted(using: [OTASortComparator(.lastModified, order: .reverse)]) == [
      newer, older,
    ])
  #expect(
    [newer, older].sorted(using: [OTASortComparator(.lastRun, order: .reverse)]) == [older, newer])
}

@MainActor
@Test func facetFiltersAreIndependentAndClearDownstreamSelections() {
  let model = AdminModel()

  model.toggleFacetValue("iOS", facet: .platform, store: .ipsw)
  model.toggleFacetValue("26.0", facet: .version, store: .ipsw)
  model.toggleFacetValue("23A1", facet: .build, store: .ipsw)
  model.toggleFacetValue("macOS", facet: .platform, store: .ipsw)

  #expect(model.ipswFacetFilter.platforms == ["iOS", "macOS"])
  #expect(model.ipswFacetFilter.versions.isEmpty)
  #expect(model.ipswFacetFilter.builds.isEmpty)
  #expect(model.otaFacetFilter == ArtifactFacetFilter())
}

@MainActor
@Test func curatedMigrationsCreateIndependentExecutableGroups() throws {
  let diagnostics = DiagnosticsLog()
  let queue = MigrationQueueModel(diagnostics: diagnostics)
  let ipsw = IPSWSource(
    lastModified: nil, state: .symbolExtractionFailed, platform: "iOS", version: "26", build: "A",
    artifactKey: "ipsw-key", fileName: "test.ipsw",
    link: try #require(URL(string: "https://example.com/a")),
    sha1: nil, lastRun: 1, mirrorPath: "gs://mirror/a"
  )
  let ota = OTAArtifact(
    lastRun: 2, lastModified: nil, state: .mirroringFailed, platform: "iOS", version: "26",
    build: "A",
    otaKey: "ota-key", artifactID: "test-ota",
    url: try #require(URL(string: "https://example.com/b")),
    hash: "abc", hashAlgorithm: "sha256", downloadPath: nil
  )

  let delta = OTAArtifact(
    lastRun: 3, lastModified: nil, state: .deltaOTA, platform: "iOS", version: "26", build: "B",
    otaKey: "delta-key", artifactID: "delta",
    url: try #require(URL(string: "https://example.com/c")),
    hash: "def", hashAlgorithm: "sha256", downloadPath: "gs://mirror/c"
  )

  #expect(migrationPreview(ipsw, action: .queueExtract).allowed)
  #expect(!migrationPreview(ota, action: .queueExtract).allowed)
  #expect(Set(commonMigrationActions(for: [ipsw])) == Set(MigrationAction.allCases))
  #expect(commonMigrationActions(for: [ota, delta]).isEmpty)
  queue.add(ipsw, action: .queueExtract)
  queue.add(ota, action: .queueMirror)

  #expect(queue.groups.count == 2)
  #expect(queue.count == 2)
  #expect(
    queue.groups[MigrationQueueKey(store: .ipsw, action: .queueExtract)]?.entries.first?
      .resultingState == .mirrored)
  #expect(
    queue.groups[MigrationQueueKey(store: .ota, action: .queueMirror)]?.entries.first?
      .resultingState == .indexed)
}

@Test func migrationApplyCompletionPreservesItemsQueuedWhileRunning() throws {
  let key = MigrationQueueKey(store: .ota, action: .queueMirror)
  let submitted = MigrationQueueEntry(
    id: "submitted", label: "Submitted", currentState: .mirroringFailed,
    resultingState: .indexed, artifactKey: nil, link: nil, otaKey: "submitted"
  )
  let addedWhileRunning = MigrationQueueEntry(
    id: "new", label: "New", currentState: .mirroringFailed,
    resultingState: .indexed, artifactKey: nil, link: nil, otaKey: "new"
  )
  let current = MigrationQueueGroup(
    key: key, entries: [submitted, addedWhileRunning], reason: "retry", isRunning: true)

  let afterSuccess = try #require(
    migrationGroupAfterSuccessfulApply(current, submittedEntryIDs: [submitted.id]))

  #expect(afterSuccess.entries == [addedWhileRunning])
  #expect(!afterSuccess.isRunning)
  #expect(afterSuccess.resultMessage == nil)

  let afterFailure = migrationGroupAfterFailedApply(current, message: "failed")

  #expect(afterFailure.entries == current.entries)
  #expect(!afterFailure.isRunning)
  #expect(afterFailure.resultMessage == "failed")
}

@Test func missingManifestProducesUsefulError() throws {
  let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
  defer { try? FileManager.default.removeItem(at: directory) }
  try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)

  #expect(throws: SnapshotLoadError.self) {
    try SnapshotRepository(cacheURL: directory).load()
  }
}
