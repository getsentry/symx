import Foundation
import SQLite3

struct SnapshotRepository: Sendable {
  static var defaultCacheURL: URL {
    FileManager.default.homeDirectoryForCurrentUser
      .appending(path: ".cache/symx/admin", directoryHint: .isDirectory)
  }

  let cacheURL: URL

  func load() throws -> Snapshot {
    let manifestURL = cacheURL.appending(path: "manifest.json")
    guard FileManager.default.fileExists(atPath: manifestURL.path) else {
      throw SnapshotLoadError.missingManifest(manifestURL)
    }

    struct Manifest: Decodable {
      let activeSnapshotID: String?

      enum CodingKeys: String, CodingKey {
        case activeSnapshotID = "active_snapshot_id"
      }
    }
    guard let data = try? Data(contentsOf: manifestURL),
      let manifest = try? JSONDecoder().decode(Manifest.self, from: data)
    else {
      throw SnapshotLoadError.invalidManifest(manifestURL)
    }
    guard let snapshotID = manifest.activeSnapshotID else {
      throw SnapshotLoadError.noActiveSnapshot
    }

    let databaseURL =
      cacheURL
      .appending(path: "snapshots", directoryHint: .isDirectory)
      .appending(path: snapshotID, directoryHint: .isDirectory)
      .appending(path: "snapshot.db")
    guard FileManager.default.fileExists(atPath: databaseURL.path) else {
      throw SnapshotLoadError.missingDatabase(databaseURL)
    }

    return try Database.readOnly(at: databaseURL) { database in
      Snapshot(
        info: try database.snapshotInfo(),
        ipswSources: try database.ipswSources(),
        otaArtifacts: try database.otaArtifacts()
      )
    }
  }
}

private struct Database {
  let handle: OpaquePointer

  static func readOnly<T>(at url: URL, body: (Database) throws -> T) throws -> T {
    var pointer: OpaquePointer?
    let result = sqlite3_open_v2(
      url.path, &pointer, SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nil)
    guard result == SQLITE_OK, let pointer else {
      let message =
        pointer.map { String(cString: sqlite3_errmsg($0)) } ?? "SQLite could not open the database"
      if let pointer { sqlite3_close(pointer) }
      throw SnapshotLoadError.sqlite(message)
    }
    defer { sqlite3_close(pointer) }
    return try body(Database(handle: pointer))
  }

  func snapshotInfo() throws -> SnapshotInfo {
    let rows = try query(
      """
      SELECT snapshot_id, created_at, workflow_run_id, workflow_run_url,
             ipsw_generation, ota_generation
      FROM snapshot_info WHERE id = 1
      """
    ) { statement in
      SnapshotInfo(
        id: text(statement, 0),
        createdAt: parseDate(optionalText(statement, 1)),
        workflowRunID: optionalInteger(statement, 2),
        workflowRunURL: optionalText(statement, 3).flatMap(URL.init(string:)),
        ipswGeneration: integer(statement, 4),
        otaGeneration: integer(statement, 5)
      )
    }
    guard let info = rows.first else {
      throw SnapshotLoadError.sqlite("snapshot_info contains no active row")
    }
    return info
  }

  func ipswSources() throws -> [IPSWSource] {
    try query(
      """
      SELECT s.last_modified, s.processing_state, a.platform, a.version, a.build,
             s.artifact_key, s.file_name, s.link, s.sha1, s.last_run, s.mirror_path
      FROM ipsw_sources s
      JOIN ipsw_artifacts a ON a.artifact_key = s.artifact_key
      ORDER BY s.last_modified DESC, s.file_name ASC
      """
    ) { statement in
      let stateValue = text(statement, 1)
      guard let state = ProcessingState(rawValue: stateValue) else {
        throw SnapshotLoadError.unsupportedState(stateValue)
      }
      guard let link = URL(string: text(statement, 7)) else {
        throw SnapshotLoadError.sqlite("An IPSW source contains an invalid link")
      }
      return IPSWSource(
        lastModified: parseDate(optionalText(statement, 0)), state: state,
        platform: text(statement, 2), version: text(statement, 3), build: text(statement, 4),
        artifactKey: text(statement, 5), fileName: text(statement, 6), link: link,
        sha1: optionalText(statement, 8), lastRun: integer(statement, 9),
        mirrorPath: optionalText(statement, 10)
      )
    }
  }

  func otaArtifacts() throws -> [OTAArtifact] {
    try query(
      """
      SELECT last_run, last_modified, processing_state, platform, version, build,
             ota_key, artifact_id, url, hash, hash_algorithm, download_path
      FROM ota_artifacts
      ORDER BY last_modified DESC, last_run DESC, ota_key ASC
      """
    ) { statement in
      let stateValue = text(statement, 2)
      guard let state = ProcessingState(rawValue: stateValue) else {
        throw SnapshotLoadError.unsupportedState(stateValue)
      }
      guard let url = URL(string: text(statement, 8)) else {
        throw SnapshotLoadError.sqlite("An OTA artifact contains an invalid URL")
      }
      return OTAArtifact(
        lastRun: integer(statement, 0), lastModified: parseDate(optionalText(statement, 1)),
        state: state, platform: text(statement, 3), version: text(statement, 4),
        build: text(statement, 5), otaKey: text(statement, 6), artifactID: text(statement, 7),
        url: url, hash: text(statement, 9), hashAlgorithm: text(statement, 10),
        downloadPath: optionalText(statement, 11)
      )
    }
  }

  private func query<T>(_ sql: String, transform: (OpaquePointer) throws -> T) throws -> [T] {
    var statement: OpaquePointer?
    guard sqlite3_prepare_v2(handle, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
      throw SnapshotLoadError.sqlite(String(cString: sqlite3_errmsg(handle)))
    }
    defer { sqlite3_finalize(statement) }

    var values: [T] = []
    while true {
      switch sqlite3_step(statement) {
      case SQLITE_ROW:
        values.append(try transform(statement))
      case SQLITE_DONE:
        return values
      default:
        throw SnapshotLoadError.sqlite(String(cString: sqlite3_errmsg(handle)))
      }
    }
  }
}

private func text(_ statement: OpaquePointer, _ column: Int32) -> String {
  guard let value = sqlite3_column_text(statement, column) else { return "" }
  return String(cString: value)
}

private func optionalText(_ statement: OpaquePointer, _ column: Int32) -> String? {
  guard sqlite3_column_type(statement, column) != SQLITE_NULL else { return nil }
  return text(statement, column)
}

private func integer(_ statement: OpaquePointer, _ column: Int32) -> Int64 {
  sqlite3_column_int64(statement, column)
}

private func optionalInteger(_ statement: OpaquePointer, _ column: Int32) -> Int64? {
  guard sqlite3_column_type(statement, column) != SQLITE_NULL else { return nil }
  return integer(statement, column)
}

private func parseDate(_ value: String?) -> Date? {
  guard let value else { return nil }
  return (try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(value))
    ?? (try? Date.ISO8601FormatStyle().parse(value))
}
