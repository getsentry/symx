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

    let result: CapturedCommandResult
    do {
      result = try CommandEnvironment.run(
        executable: uv,
        currentDirectory: projectRoot,
        arguments: [
          "run", "--project", projectRoot.path,
          "symx", "admin", "sync",
          "--cache-dir", cacheURL.path,
        ]
      )
    } catch {
      throw SyncError.couldNotLaunch(error.localizedDescription)
    }

    guard result.terminationStatus == 0 else {
      let message = result.failureText
      throw SyncError.commandFailed(
        message.isEmpty ? "uv exited with status \(result.terminationStatus)" : message)
    }
    return result.stdoutText.trimmingCharacters(in: .whitespacesAndNewlines)
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
