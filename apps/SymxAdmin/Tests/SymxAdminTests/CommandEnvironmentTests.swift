import Foundation
import Testing

@testable import SymxAdmin

struct CommandEnvironmentTests {
  @Test func successfulStderrNoticeDoesNotContaminateJSON() throws {
    let result = try runShell(
      """
      printf '{"ok":true}\\n'
      printf 'upgrade notice\\n' >&2
      """)

    #expect(result.terminationStatus == 0)
    #expect(result.stdout == Data("{\"ok\":true}\n".utf8))
    #expect(result.stderr == Data("upgrade notice\n".utf8))
    let decoded = try JSONDecoder().decode(JSONResponse.self, from: result.stdout)
    #expect(decoded.ok)
  }

  @Test func nonzeroExitRetainsBothStreamsForDiagnostics() throws {
    let result = try runShell(
      """
      printf 'partial output\\n'
      printf 'failure detail\\n' >&2
      exit 7
      """)

    #expect(result.terminationStatus == 7)
    #expect(result.stdoutText == "partial output\n")
    #expect(result.stderrText == "failure detail\n")
    #expect(result.failureText == "failure detail\npartial output")
  }

  @Test(arguments: [
    (" \n", "\t\n", ""),
    (" output \n", "\t\n", "output"),
    (" \n", " error \n", "error"),
    (" output \n", " error \n", "error\noutput"),
  ])
  func failureTextTrimsAndOmitsBlankStreams(stdout: String, stderr: String, expected: String) {
    let result = CapturedCommandResult(
      terminationStatus: 1, stdout: Data(stdout.utf8), stderr: Data(stderr.utf8))

    #expect(result.failureText == expected)
  }

  @Test func capturesLargeOutputOnBothStreams() throws {
    let result = try runShell(
      """
      i=0
      while [ "$i" -lt 8192 ]; do
        printf '0123456789abcdef'
        printf 'fedcba9876543210' >&2
        i=$((i + 1))
      done
      """)

    #expect(result.terminationStatus == 0)
    #expect(result.stdout == Data(String(repeating: "0123456789abcdef", count: 8192).utf8))
    #expect(result.stderr == Data(String(repeating: "fedcba9876543210", count: 8192).utf8))
  }

  @Test func launchFailureThrows() {
    let missingExecutable = FileManager.default.temporaryDirectory.appending(
      path: UUID().uuidString)

    #expect(throws: (any Error).self) {
      try CommandEnvironment.run(
        executable: missingExecutable,
        currentDirectory: FileManager.default.temporaryDirectory,
        arguments: []
      )
    }
  }

  private func runShell(_ script: String) throws -> CapturedCommandResult {
    try CommandEnvironment.run(
      executable: URL(filePath: "/bin/sh"),
      currentDirectory: FileManager.default.temporaryDirectory,
      arguments: ["-c", script]
    )
  }

  private struct JSONResponse: Decodable {
    let ok: Bool
  }
}
