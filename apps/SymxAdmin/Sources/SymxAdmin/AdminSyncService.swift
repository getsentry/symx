import Foundation

struct AdminSyncService: Sendable {
  let cacheURL: URL

  func sync() throws -> String {
    guard let projectRoot = CommandEnvironment.projectRoot() else {
      throw SyncError.projectRootNotFound
    }
    guard let uv = CommandEnvironment.executable(named: "uv") else {
      throw SyncError.uvNotFound
    }

    let process = Process()
    let output = Pipe()
    process.executableURL = uv
    process.currentDirectoryURL = projectRoot
    process.arguments = [
      "run", "--project", projectRoot.path,
      "symx", "admin", "sync",
      "--cache-dir", cacheURL.path,
    ]
    process.standardOutput = output
    process.standardError = output

    do {
      try process.run()
    } catch {
      throw SyncError.couldNotLaunch(error.localizedDescription)
    }

    // Drain the pipe while the child runs; waiting first can deadlock if the pipe fills.
    let data = output.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    let message = String(decoding: data, as: UTF8.self).trimmingCharacters(
      in: .whitespacesAndNewlines)
    guard process.terminationStatus == 0 else {
      throw SyncError.commandFailed(
        message.isEmpty ? "uv exited with status \(process.terminationStatus)" : message)
    }
    return message
  }
}

enum SyncError: LocalizedError {
  case projectRootNotFound
  case uvNotFound
  case couldNotLaunch(String)
  case commandFailed(String)

  var errorDescription: String? {
    switch self {
    case .projectRootNotFound:
      "Could not find the symx checkout containing pyproject.toml."
    case .uvNotFound:
      "Could not find uv in PATH, /opt/homebrew/bin, or /usr/local/bin."
    case .couldNotLaunch(let detail):
      "Could not start the admin sync: \(detail)"
    case .commandFailed(let detail):
      "Admin sync failed: \(detail)"
    }
  }
}
