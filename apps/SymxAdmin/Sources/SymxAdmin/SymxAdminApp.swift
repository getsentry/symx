import AppKit
import SwiftUI

@MainActor
final class SymxAdminApplicationDelegate: NSObject, NSApplicationDelegate {
  func applicationDidFinishLaunching(_ notification: Notification) {
    NSApplication.shared.setActivationPolicy(.regular)
    NSApplication.shared.activate()
  }
}

@main
struct SymxAdminApp: App {
  @NSApplicationDelegateAdaptor(SymxAdminApplicationDelegate.self) private var applicationDelegate
  @State private var model = AdminModel()

  var body: some Scene {
    WindowGroup("symx admin") {
      ContentView(model: model)
        .frame(minWidth: 1_080, minHeight: 680)
        .task { await model.start() }
    }
    .defaultSize(width: 1_360, height: 860)
    .commands {
      CommandGroup(after: .newItem) {
        Button("Sync Snapshot") {
          Task { await model.sync() }
        }
        .keyboardShortcut("r")

        Button("Show Admin Cache in Finder") {
          NSWorkspace.shared.activateFileViewerSelecting([model.cacheURL])
        }
      }
    }

    Window("Workflow Tasks", id: "workflow-tasks") {
      WorkflowTasksView(model: model.workflowTasks)
    }
    .defaultSize(width: 1_100, height: 620)

    Window("Migration Queues", id: "migration-queues") {
      MigrationQueueView(model: model)
    }
    .defaultSize(width: 900, height: 700)
  }
}
