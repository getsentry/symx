import Foundation

enum CommandEnvironment {
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
