import AppKit
import SwiftUI

struct WorkflowTasksView: View {
  @Bindable var model: WorkflowTasksModel

  var body: some View {
    VStack(spacing: 0) {
      HStack {
        Text("Workflow Tasks").font(.title2.bold())
        Spacer()
        if model.isRefreshing { ProgressView().controlSize(.small) }
        Button("Refresh", systemImage: "arrow.clockwise") { Task { await model.refresh() } }
          .disabled(model.isRefreshing)
      }
      .padding()
      Divider()
      if let error = model.errorMessage, model.tasks.isEmpty {
        ContentUnavailableView(
          "Could Not Load Workflows", systemImage: "exclamationmark.triangle",
          description: Text(error))
      } else {
        Table(model.tasks) {
          TableColumn("Status") { task in WorkflowStatusView(task: task) }.width(
            min: 100, ideal: 120)
          TableColumn("Workflow") { Text($0.workflowName) }.width(min: 160, ideal: 210)
          TableColumn("Run") { Text("#\($0.databaseId)").monospacedDigit() }.width(
            min: 80, ideal: 90)
          TableColumn("Title") { Text($0.displayTitle) }.width(min: 220, ideal: 360)
          TableColumn("Started") {
            Text($0.startedAt?.formatted(date: .numeric, time: .shortened) ?? "—")
          }
          .width(min: 140, ideal: 170)
        }
        .contextMenu(forSelectionType: WorkflowTask.ID.self) { ids in
          if let id = ids.first, let task = model.tasks.first(where: { $0.id == id }) {
            Button("Open on GitHub", systemImage: "safari") { NSWorkspace.shared.open(task.url) }
          }
        } primaryAction: { ids in
          if let id = ids.first, let task = model.tasks.first(where: { $0.id == id }) {
            NSWorkspace.shared.open(task.url)
          }
        }
      }
    }
    .frame(minWidth: 900, minHeight: 480)
    .task {
      while !Task.isCancelled {
        await model.refresh()
        let hasActive = model.tasks.contains(where: \.isActive)
        try? await Task.sleep(for: .seconds(hasActive ? 10 : 60))
      }
    }
  }
}

private struct WorkflowStatusView: View {
  let task: WorkflowTask

  var body: some View {
    Label(label, systemImage: icon)
      .foregroundStyle(task.isActive ? .blue : task.conclusion == "success" ? .green : .secondary)
  }

  private var label: String { task.isActive ? task.status : task.conclusion ?? "completed" }
  private var icon: String {
    if task.isActive { return "arrow.trianglehead.2.clockwise.rotate.90" }
    return task.conclusion == "success" ? "checkmark.circle.fill" : "xmark.circle.fill"
  }
}

struct MigrationQueueView: View {
  @Bindable var model: AdminModel
  @State private var confirmation: MigrationQueueKey?

  private var queue: MigrationQueueModel { model.migrationQueue }

  var body: some View {
    ScrollView {
      LazyVStack(alignment: .leading, spacing: 16) {
        HStack {
          VStack(alignment: .leading) {
            Text("Migration Queues").font(.title2.bold())
            Text("Applying a queue mutates shared metadata through GitHub Actions.")
              .foregroundStyle(.secondary)
          }
          Spacer()
          Text("\(queue.count) targets").monospacedDigit()
        }

        if queue.orderedGroups.isEmpty {
          ContentUnavailableView(
            "No Queued Migrations",
            systemImage: "tray",
            description: Text(
              "Right-click an artifact in an IPSW or OTA table to add a curated migration.")
          )
        }

        ForEach(queue.orderedGroups) { group in
          MigrationGroupView(model: model, group: group) {
            confirmation = group.key
          }
        }
      }
      .padding()
    }
    .frame(minWidth: 760, minHeight: 500)
    .confirmationDialog(
      "Apply this migration queue to shared storage?",
      isPresented: Binding(get: { confirmation != nil }, set: { if !$0 { confirmation = nil } })
    ) {
      if let key = confirmation {
        Button("Apply \(key.title)", role: .destructive) {
          confirmation = nil
          guard let snapshot = model.snapshot?.info else { return }
          Task {
            await queue.apply(key, snapshot: snapshot)
            if queue.groups[key] == nil { await model.sync() }
          }
        }
      }
      Button("Cancel", role: .cancel) { confirmation = nil }
    } message: {
      Text("The request is generation-matched and will fail safely if the snapshot is stale.")
    }
  }
}

private struct MigrationGroupView: View {
  @Bindable var model: AdminModel
  let group: MigrationQueueGroup
  let apply: () -> Void

  private var queue: MigrationQueueModel { model.migrationQueue }

  var body: some View {
    GroupBox {
      VStack(alignment: .leading, spacing: 12) {
        ForEach(group.entries) { entry in
          HStack {
            Text(entry.label).lineLimit(1)
            Spacer()
            Text(entry.currentState.rawValue).foregroundStyle(.secondary)
            Image(systemName: "arrow.right")
            Text(entry.resultingState.rawValue)
            Button("Remove", systemImage: "minus.circle") {
              queue.remove(entryID: entry.id, from: group.key)
            }
            .labelStyle(.iconOnly)
            .disabled(group.isRunning)
          }
        }
        Divider()
        TextField(
          "Reason for this curated migration",
          text: Binding(
            get: { queue.groups[group.key]?.reason ?? "" },
            set: { queue.setReason($0, for: group.key) }
          )
        )
        .disabled(group.isRunning)
        if let result = group.resultMessage {
          Text(result).foregroundStyle(.red).textSelection(.enabled)
        }
        HStack {
          Button("Clear", role: .destructive) { queue.clear(group.key) }
            .disabled(group.isRunning)
          Spacer()
          if group.isRunning { ProgressView().controlSize(.small) }
          Button("Apply Queue", systemImage: "paperplane.fill", action: apply)
            .buttonStyle(.borderedProminent)
            .disabled(
              group.isRunning
                || group.reason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
      }
      .padding(6)
    } label: {
      Text("\(group.key.title) (\(group.entries.count))")
    }
  }
}
