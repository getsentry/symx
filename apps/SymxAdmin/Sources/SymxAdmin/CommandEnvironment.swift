import Foundation

struct CapturedCommandResult: Sendable {
  let terminationStatus: Int32
  let stdout: Data
  let stderr: Data

  var stdoutText: String { String(decoding: stdout, as: UTF8.self) }
  var stderrText: String { String(decoding: stderr, as: UTF8.self) }

  var failureText: String {
    [stderrText, stdoutText]
      .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
      .filter { !$0.isEmpty }
      .joined(separator: "\n")
  }
}

enum CommandEnvironment {
  static func run(
    executable: URL,
    currentDirectory: URL,
    arguments: [String]
  ) throws -> CapturedCommandResult {
    let directory = FileManager.default.temporaryDirectory.appending(
      path: UUID().uuidString, directoryHint: .isDirectory)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: directory) }

    // Separate files keep stderr out of machine-readable stdout without bounded pipe buffers.
    let stdoutURL = directory.appending(path: "stdout")
    let stderrURL = directory.appending(path: "stderr")
    try Data().write(to: stdoutURL)
    try Data().write(to: stderrURL)
    let stdoutHandle = try FileHandle(forWritingTo: stdoutURL)
    defer { try? stdoutHandle.close() }
    let stderrHandle = try FileHandle(forWritingTo: stderrURL)
    defer { try? stderrHandle.close() }

    let process = Process()
    process.executableURL = executable
    process.currentDirectoryURL = currentDirectory
    process.arguments = arguments
    process.standardOutput = stdoutHandle
    process.standardError = stderrHandle
    try process.run()
    process.waitUntilExit()

    try stdoutHandle.close()
    try stderrHandle.close()
    return CapturedCommandResult(
      terminationStatus: process.terminationStatus,
      stdout: try Data(contentsOf: stdoutURL),
      stderr: try Data(contentsOf: stderrURL)
    )
  }

  static func executable(named name: String) -> URL? {
    let environmentPath = ProcessInfo.processInfo.environment["PATH", default: ""]
      .split(separator: ":")
      .map(String.init)
    let candidates = environmentPath + ["/opt/homebrew/bin", "/usr/local/bin"]
    return
      candidates
      .map { URL(filePath: $0).appending(path: name) }
      .first { FileManager.default.isExecutableFile(atPath: $0.path) }
  }

  static func projectRoot() -> URL? {
    let starts = [
      URL(filePath: FileManager.default.currentDirectoryPath, directoryHint: .isDirectory),
      Bundle.main.bundleURL,
      URL(filePath: #filePath).deletingLastPathComponent(),
    ]
    for start in starts {
      var candidate = start
      for _ in 0..<12 {
        let pyproject = candidate.appending(path: "pyproject.toml")
        let admin = candidate.appending(path: "symx/admin", directoryHint: .isDirectory)
        if FileManager.default.fileExists(atPath: pyproject.path),
          FileManager.default.fileExists(atPath: admin.path)
        {
          return candidate
        }
        let parent = candidate.deletingLastPathComponent()
        guard parent != candidate else { break }
        candidate = parent
      }
    }
    return nil
  }
}
