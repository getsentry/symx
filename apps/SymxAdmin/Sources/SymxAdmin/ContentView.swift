import AppKit
import SwiftUI

struct ContentView: View {
  @Environment(\.openWindow) private var openWindow
  @Bindable var model: AdminModel
  @State private var section: AdminSection? = .overview
  @State private var selectedIPSW: Set<IPSWSource.ID> = []
  @State private var selectedOTA: Set<OTAArtifact.ID> = []
  @State private var suspendedIPSWSelection: Set<IPSWSource.ID>?
  @State private var suspendedOTASelection: Set<OTAArtifact.ID>?
  @State private var showDiagnostics = true

  var body: some View {
    VStack(spacing: 0) {
      NavigationSplitView {
        List(AdminSection.allCases, selection: $section) { item in
          Label(item.title, systemImage: item.systemImage)
            .badge(badge(for: item))
        }
        .navigationTitle("symx")
        .navigationSplitViewColumnWidth(min: 180, ideal: 210)
      } detail: {
        Group {
          if model.snapshot == nil, model.isLoading || model.isSyncing {
            ProgressView(model.statusMessage ?? "Reading admin snapshot…")
              .controlSize(.large)
          } else if model.snapshot == nil, let error = model.errorMessage {
            ContentUnavailableView {
              Label(
                "Snapshot Could Not Be Loaded", systemImage: "externaldrive.badge.exclamationmark")
            } description: {
              Text(error)
            } actions: {
              Button("Try Sync Again") { Task { await model.sync() } }
                .buttonStyle(.borderedProminent)
              Button("Show Cache in Finder") {
                NSWorkspace.shared.activateFileViewerSelecting([model.cacheURL])
              }
            }
          } else if model.snapshot == nil {
            ContentUnavailableView(
              "No Admin Snapshot", systemImage: "externaldrive.badge.questionmark")
          } else {
            switch section ?? .overview {
            case .overview:
              OverviewView(model: model)
            case .ipsw:
              IPSWListView(
                rows: model.ipswSources,
                selection: $selectedIPSW,
                diagnostics: model.diagnostics,
                migrationQueue: model.migrationQueue,
                sortOrder: Binding(
                  get: { model.ipswSortOrder },
                  set: {
                    suspendTableSelection()
                    model.ipswSortOrder = $0
                  }
                )
              )
              .id(model.tableRevision)
              .transaction { transaction in
                transaction.animation = nil
                transaction.disablesAnimations = true
              }
            case .ota:
              OTAListView(
                rows: model.otaArtifacts,
                selection: $selectedOTA,
                diagnostics: model.diagnostics,
                migrationQueue: model.migrationQueue,
                sortOrder: Binding(
                  get: { model.otaSortOrder },
                  set: {
                    suspendTableSelection()
                    model.otaSortOrder = $0
                  }
                )
              )
              .id(model.tableRevision)
              .transaction { transaction in
                transaction.animation = nil
                transaction.disablesAnimations = true
              }
            }
          }
        }
        .navigationTitle((section ?? .overview).title)
        .searchable(text: $model.searchText, placement: .toolbar, prompt: "Search artifacts")
        .toolbar {
          ToolbarItemGroup {
            StateFilterMenu(model: model) {
              suspendTableSelection()
            }
            if section == .ipsw {
              ArtifactFacetControls(model: model, store: .ipsw) { suspendTableSelection() }
            } else if section == .ota {
              ArtifactFacetControls(model: model, store: .ota) { suspendTableSelection() }
            }
            if model.isSyncing {
              ProgressView().controlSize(.small)
            }
            Button {
              openWindow(id: "workflow-tasks")
            } label: {
              Label("Workflow Tasks", systemImage: "point.3.connected.trianglepath.dotted")
            }
            Button {
              openWindow(id: "migration-queues")
            } label: {
              Label("Migration Queues", systemImage: "tray.full")
            }
            Button {
              Task { await model.sync() }
            } label: {
              Label("Sync Snapshot", systemImage: "arrow.trianglehead.2.clockwise.rotate.90")
            }
            .disabled(model.isLoading || model.isSyncing)
            Button {
              showDiagnostics.toggle()
            } label: {
              Label("Diagnostics", systemImage: "text.rectangle.page")
            }
            .help(showDiagnostics ? "Hide diagnostics" : "Show diagnostics")
          }
        }
      }
      if showDiagnostics {
        Divider()
        DiagnosticsPane(log: model.diagnostics) {
          showDiagnostics = false
        }
        .frame(minHeight: 120, idealHeight: 190, maxHeight: 260)
      }
    }
    .onChange(of: model.searchText) {
      suspendTableSelection()
    }
    .onChange(of: model.tableRevision) { _, revision in
      restoreTableSelection(for: revision)
    }
    .alert(
      "Snapshot Could Not Be Loaded",
      isPresented: Binding(
        get: { model.snapshot != nil && model.errorMessage != nil },
        set: { if !$0 { model.dismissError() } }
      )
    ) {
      Button("OK") { model.dismissError() }
    } message: {
      Text(model.errorMessage ?? "Unknown error")
    }
  }

  private func suspendTableSelection() {
    model.diagnostics.record(
      "selection",
      "suspend ipsw=\(selectedIPSW.count) ota=\(selectedOTA.count)"
    )
    if suspendedIPSWSelection == nil {
      suspendedIPSWSelection = selectedIPSW
    }
    if suspendedOTASelection == nil {
      suspendedOTASelection = selectedOTA
    }
    selectedIPSW = []
    selectedOTA = []
  }

  private func restoreTableSelection(for revision: Int) {
    let ipswSelection = suspendedIPSWSelection
    let otaSelection = suspendedOTASelection
    suspendedIPSWSelection = nil
    suspendedOTASelection = nil

    Task { @MainActor in
      // Let the replacement Table install its AppKit coordinator before attaching
      // selection. A newer filter result supersedes this restoration attempt.
      await Task.yield()
      guard model.tableRevision == revision else { return }
      if let ipswSelection {
        let available = Set(model.ipswSources.lazy.map(\.id))
        selectedIPSW = ipswSelection.intersection(available)
        model.diagnostics.record(
          "selection",
          "restored IPSW=\(selectedIPSW.count) excluded=\(ipswSelection.count - selectedIPSW.count) revision=\(revision)"
        )
      }
      if let otaSelection {
        let available = Set(model.otaArtifacts.lazy.map(\.id))
        selectedOTA = otaSelection.intersection(available)
        model.diagnostics.record(
          "selection",
          "restored OTA=\(selectedOTA.count) excluded=\(otaSelection.count - selectedOTA.count) revision=\(revision)"
        )
      }
    }
  }

  private func badge(for section: AdminSection) -> Int {
    switch section {
    case .overview: model.failureCount
    case .ipsw: model.ipswSources.count
    case .ota: model.otaArtifacts.count
    }
  }
}

private struct StateFilterMenu: View {
  @Bindable var model: AdminModel
  let willChange: () -> Void

  var body: some View {
    Menu {
      ForEach(ProcessingState.allCases) { state in
        Toggle(isOn: binding(for: state)) {
          Label(state.title, systemImage: state.systemImage)
        }
      }
      Divider()
      Button("Default Failures") {
        willChange()
        model.resetFilters()
      }
      Button("Show Everything") {
        willChange()
        model.showAllStates()
      }
    } label: {
      Label("Processing States", systemImage: "line.3.horizontal.decrease.circle")
    }
    .help("Filter by processing state")
  }

  private func binding(for state: ProcessingState) -> Binding<Bool> {
    Binding(
      get: { model.selectedStates.contains(state) },
      set: { selected in
        willChange()
        if selected {
          model.selectedStates.insert(state)
        } else {
          model.selectedStates.remove(state)
        }
      }
    )
  }
}

private struct ArtifactFacetControls: View {
  @Bindable var model: AdminModel
  let store: ArtifactStore
  let willChange: () -> Void

  var body: some View {
    let filter = model.facetFilter(for: store)
    let options = model.facetOptions(for: store)
    FacetFilterButton(
      title: "Platform",
      facet: .platform,
      options: options.platforms,
      selection: filter.platforms,
      model: model,
      store: store,
      willChange: willChange
    )
    FacetFilterButton(
      title: "Version",
      facet: .version,
      options: options.versions,
      selection: filter.versions,
      model: model,
      store: store,
      willChange: willChange
    )
    FacetFilterButton(
      title: "Build",
      facet: .build,
      options: options.builds,
      selection: filter.builds,
      model: model,
      store: store,
      willChange: willChange
    )
  }
}

private struct FacetFilterButton: View {
  let title: String
  let facet: ArtifactFacet
  let options: [String]
  let selection: Set<String>
  @Bindable var model: AdminModel
  let store: ArtifactStore
  let willChange: () -> Void
  @State private var isPresented = false
  @State private var optionSearch = ""

  var body: some View {
    Button {
      isPresented.toggle()
    } label: {
      Label(
        filterLabel,
        systemImage: selection.isEmpty
          ? "line.3.horizontal.decrease" : "line.3.horizontal.decrease.circle.fill")
    }
    .help("Filter by \(title.lowercased())")
    .popover(isPresented: $isPresented, arrowEdge: .bottom) {
      VStack(spacing: 0) {
        HStack {
          Text(title).font(.headline)
          Spacer()
          Button("All") {
            willChange()
            model.selectAll(facet, for: store)
          }
          .disabled(selection.isEmpty)
        }
        .padding(12)

        TextField("Search \(title.lowercased())s", text: $optionSearch)
          .textFieldStyle(.roundedBorder)
          .padding(.horizontal, 12)
          .padding(.bottom, 8)

        Divider()
        List(filteredOptions, id: \.self) { option in
          Button {
            willChange()
            model.toggleFacetValue(option, facet: facet, store: store)
          } label: {
            HStack {
              Text(option)
              Spacer()
              if selection.contains(option) {
                Image(systemName: "checkmark")
              }
            }
            .contentShape(.rect)
          }
          .buttonStyle(.plain)
        }
        .alternatingRowBackgrounds(.enabled)

        Divider()
        Text("\(options.count.formatted()) available")
          .font(.caption)
          .foregroundStyle(.secondary)
          .frame(maxWidth: .infinity, alignment: .leading)
          .padding(10)
      }
      .frame(width: 320, height: 420)
    }
  }

  private var filterLabel: String {
    selection.isEmpty ? title : "\(title) (\(selection.count))"
  }

  private var filteredOptions: [String] {
    optionSearch.isEmpty ? options : options.filter { $0.localizedStandardContains(optionSearch) }
  }
}

private struct OverviewView: View {
  @Bindable var model: AdminModel

  private var snapshot: Snapshot { model.snapshot! }

  var body: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 24) {
        HStack(spacing: 16) {
          MetricCard(
            title: "Failures", value: model.failureCount.formatted(),
            systemImage: "exclamationmark.triangle.fill", tint: .orange)
          MetricCard(
            title: "IPSW sources", value: snapshot.ipswSources.count.formatted(),
            systemImage: "shippingbox.fill", tint: .blue)
          MetricCard(
            title: "OTA artifacts", value: snapshot.otaArtifacts.count.formatted(),
            systemImage: "antenna.radiowaves.left.and.right", tint: .purple)
        }

        GroupBox("Active snapshot") {
          Grid(alignment: .leading, horizontalSpacing: 28, verticalSpacing: 12) {
            DetailRow(label: "Snapshot", value: snapshot.info.id)
            DetailRow(
              label: "Created",
              value: snapshot.info.createdAt?.formatted(date: .abbreviated, time: .standard) ?? "—")
            DetailRow(label: "IPSW generation", value: snapshot.info.ipswGeneration.formatted())
            DetailRow(label: "OTA generation", value: snapshot.info.otaGeneration.formatted())
            if let runID = snapshot.info.workflowRunID {
              DetailRow(label: "Workflow run", value: "#\(runID)")
            }
          }
          .frame(maxWidth: .infinity, alignment: .leading)
          .padding(8)
        }

        StateSummary(snapshot: snapshot)
      }
      .padding(24)
    }
  }
}

private struct MetricCard: View {
  let title: String
  let value: String
  let systemImage: String
  let tint: Color

  var body: some View {
    HStack(spacing: 16) {
      Image(systemName: systemImage)
        .font(.title)
        .foregroundStyle(tint)
        .frame(width: 42, height: 42)
      VStack(alignment: .leading) {
        Text(value).font(.system(.title, design: .rounded, weight: .semibold))
        Text(title).foregroundStyle(.secondary)
      }
      Spacer()
    }
    .padding(18)
    .frame(maxWidth: .infinity)
    .glassEffect(.regular, in: .rect(cornerRadius: 18))
  }
}

private struct StateSummary: View {
  let snapshot: Snapshot

  var body: some View {
    GroupBox("Processing states") {
      Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 10) {
        GridRow {
          Text("State").foregroundStyle(.secondary)
          Text("IPSW").foregroundStyle(.secondary)
          Text("OTA").foregroundStyle(.secondary)
        }
        Divider()
        ForEach(ProcessingState.allCases) { state in
          let ipsw = snapshot.ipswSources.count { $0.state == state }
          let ota = snapshot.otaArtifacts.count { $0.state == state }
          if ipsw > 0 || ota > 0 {
            GridRow {
              StateBadge(state: state)
              Text(ipsw.formatted()).monospacedDigit()
              Text(ota.formatted()).monospacedDigit()
            }
          }
        }
      }
      .padding(8)
      .frame(maxWidth: .infinity, alignment: .leading)
    }
  }
}

private struct IPSWListView: View {
  @Environment(\.openWindow) private var openWindow
  let rows: [IPSWSource]
  @Binding var selection: Set<IPSWSource.ID>
  let diagnostics: DiagnosticsLog
  let migrationQueue: MigrationQueueModel
  @Binding var sortOrder: [IPSWSortComparator]
  @State private var scrollPosition = ScrollPosition(idType: String.self)
  @State private var visibleRowIDs: Set<String> = []
  private var selected: IPSWSource? {
    guard selection.count == 1, let id = selection.first else { return nil }
    return rows.first { $0.id == id }
  }

  var body: some View {
    HSplitView {
      Table(of: IPSWSource.self, selection: $selection, sortOrder: $sortOrder) {
        TableColumn("State", sortUsing: IPSWSortComparator(.state)) { StateBadge(state: $0.state) }
          .width(min: 150, ideal: 180)
        TableColumn("Platform", sortUsing: IPSWSortComparator(.platform)) { Text($0.platform) }
          .width(min: 80, ideal: 100)
        TableColumn("Version", sortUsing: IPSWSortComparator(.version)) { Text($0.version) }
          .width(min: 80, ideal: 100)
        TableColumn("Build", sortUsing: IPSWSortComparator(.build)) { Text($0.build) }
          .width(min: 90, ideal: 110)
        TableColumn("File", sortUsing: IPSWSortComparator(.fileName)) { Text($0.fileName) }
          .width(min: 220, ideal: 360)
        TableColumn("Modified", sortUsing: IPSWSortComparator(.lastModified)) {
          Text($0.lastModified?.formatted(date: .numeric, time: .shortened) ?? "—")
        }
        .width(min: 140, ideal: 170)
      } rows: {
        ForEach(rows) { row in
          TableRow(row).contextMenu { migrationMenu(for: row) }
        }
      }
      .scrollPosition($scrollPosition)
      .onScrollTargetVisibilityChange(idType: String.self) { visibleRowIDs = Set($0) }
      .onChange(of: selection) { _, selectedID in revealIfNeeded(selectedID) }
      .overlay { if rows.isEmpty { EmptyResultsView() } }

      ArtifactDetails(title: "IPSW source", state: selected?.state, selectionCount: selection.count)
      {
        if let selected {
          DetailRow(label: "Platform", value: selected.platform)
          DetailRow(label: "Version", value: selected.version)
          DetailRow(label: "Build", value: selected.build)
          DetailRow(label: "Artifact key", value: selected.artifactKey)
          DetailRow(label: "File", value: selected.fileName)
          DetailRow(label: "Last run", value: "#\(selected.lastRun)")
          DetailRow(label: "SHA-1", value: selected.sha1 ?? "—")
          DetailRow(label: "Mirror path", value: selected.mirrorPath ?? "—")
          Link("Open source URL", destination: selected.link)
        }
      }
    }
  }

  @ViewBuilder
  private func migrationMenu(for row: IPSWSource) -> some View {
    let targets = selection.contains(row.id) ? rows.filter { selection.contains($0.id) } : [row]
    let actions = commonMigrationActions(for: targets)
    if actions.isEmpty {
      Button("No common migration") {}.disabled(true)
    } else {
      ForEach(actions, id: \.self) { action in
        Button(targets.count == 1 ? action.title : "\(action.title) (\(targets.count) artifacts)") {
          for target in targets { migrationQueue.add(target, action: action) }
          openWindow(id: "migration-queues")
        }
      }
    }
    Divider()
    Button("Show Migration Queues", systemImage: "tray.full") { openWindow(id: "migration-queues") }
  }

  private func revealIfNeeded(_ selectedIDs: Set<String>) {
    guard !selectedIDs.isEmpty else { return }
    Task { @MainActor in
      await Task.yield()
      guard selection == selectedIDs else { return }
      if !visibleRowIDs.isDisjoint(with: selectedIDs) {
        diagnostics.record("scroll", "IPSW restored selection already visible")
        return
      }
      guard let selectedID = rows.first(where: { selectedIDs.contains($0.id) })?.id else { return }
      diagnostics.record("scroll", "IPSW reveal restored selection count=\(selectedIDs.count)")
      scrollPosition.scrollTo(id: selectedID, anchor: .center)
    }
  }
}

private struct OTAListView: View {
  @Environment(\.openWindow) private var openWindow
  let rows: [OTAArtifact]
  @Binding var selection: Set<OTAArtifact.ID>
  let diagnostics: DiagnosticsLog
  let migrationQueue: MigrationQueueModel
  @Binding var sortOrder: [OTASortComparator]
  @State private var scrollPosition = ScrollPosition(idType: String.self)
  @State private var visibleRowIDs: Set<String> = []
  private var selected: OTAArtifact? {
    guard selection.count == 1, let id = selection.first else { return nil }
    return rows.first { $0.id == id }
  }

  var body: some View {
    HSplitView {
      Table(of: OTAArtifact.self, selection: $selection, sortOrder: $sortOrder) {
        TableColumn("State", sortUsing: OTASortComparator(.state)) { StateBadge(state: $0.state) }
          .width(min: 150, ideal: 180)
        TableColumn("Platform", sortUsing: OTASortComparator(.platform)) { Text($0.platform) }
          .width(min: 80, ideal: 100)
        TableColumn("Version", sortUsing: OTASortComparator(.version)) { Text($0.version) }
          .width(min: 80, ideal: 100)
        TableColumn("Build", sortUsing: OTASortComparator(.build)) { Text($0.build) }
          .width(min: 90, ideal: 110)
        TableColumn("Artifact", sortUsing: OTASortComparator(.artifactID)) { Text($0.artifactID) }
          .width(min: 220, ideal: 360)
        TableColumn("Modified", sortUsing: OTASortComparator(.lastModified)) {
          Text($0.lastModified?.formatted(date: .numeric, time: .shortened) ?? "—")
        }
        .width(min: 140, ideal: 170)
      } rows: {
        ForEach(rows) { row in
          TableRow(row).contextMenu { migrationMenu(for: row) }
        }
      }
      .scrollPosition($scrollPosition)
      .onScrollTargetVisibilityChange(idType: String.self) { visibleRowIDs = Set($0) }
      .onChange(of: selection) { _, selectedID in revealIfNeeded(selectedID) }
      .overlay { if rows.isEmpty { EmptyResultsView() } }

      ArtifactDetails(
        title: "OTA artifact", state: selected?.state, selectionCount: selection.count
      ) {
        if let selected {
          DetailRow(label: "Platform", value: selected.platform)
          DetailRow(label: "Version", value: selected.version)
          DetailRow(label: "Build", value: selected.build)
          DetailRow(label: "OTA key", value: selected.otaKey)
          DetailRow(label: "Artifact ID", value: selected.artifactID)
          DetailRow(
            label: "Modified",
            value: selected.lastModified?.formatted(date: .abbreviated, time: .standard) ?? "—"
          )
          DetailRow(label: "Last run", value: "#\(selected.lastRun)")
          DetailRow(label: "Hash", value: "\(selected.hashAlgorithm):\(selected.hash)")
          DetailRow(label: "Download path", value: selected.downloadPath ?? "—")
          Link("Open artifact URL", destination: selected.url)
        }
      }
    }
  }

  @ViewBuilder
  private func migrationMenu(for row: OTAArtifact) -> some View {
    let targets = selection.contains(row.id) ? rows.filter { selection.contains($0.id) } : [row]
    let actions = commonMigrationActions(for: targets)
    if actions.isEmpty {
      Button("No common migration") {}.disabled(true)
    } else {
      ForEach(actions, id: \.self) { action in
        Button(targets.count == 1 ? action.title : "\(action.title) (\(targets.count) artifacts)") {
          for target in targets { migrationQueue.add(target, action: action) }
          openWindow(id: "migration-queues")
        }
      }
    }
    Divider()
    Button("Show Migration Queues", systemImage: "tray.full") { openWindow(id: "migration-queues") }
  }

  private func revealIfNeeded(_ selectedIDs: Set<String>) {
    guard !selectedIDs.isEmpty else { return }
    Task { @MainActor in
      await Task.yield()
      guard selection == selectedIDs else { return }
      if !visibleRowIDs.isDisjoint(with: selectedIDs) {
        diagnostics.record("scroll", "OTA restored selection already visible")
        return
      }
      guard let selectedID = rows.first(where: { selectedIDs.contains($0.id) })?.id else { return }
      diagnostics.record("scroll", "OTA reveal restored selection count=\(selectedIDs.count)")
      scrollPosition.scrollTo(id: selectedID, anchor: .center)
    }
  }
}

private struct ArtifactDetails<Content: View>: View {
  let title: String
  let state: ProcessingState?
  let selectionCount: Int
  @ViewBuilder let content: Content

  var body: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 16) {
        Text(title).font(.title2.bold())
        if let state { StateBadge(state: state) }
        Divider()
        if selectionCount > 1 {
          ContentUnavailableView(
            "\(selectionCount) Artifacts Selected",
            systemImage: "checklist",
            description: Text("Right-click the selection to queue a common curated migration.")
          )
        } else if state == nil {
          ContentUnavailableView("Nothing Selected", systemImage: "cursorarrow.click")
        } else {
          Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 12) { content }
        }
      }
      .padding(20)
    }
    .frame(minWidth: 290, idealWidth: 340, maxWidth: 420)
  }
}

private struct DetailRow: View {
  let label: String
  let value: String

  var body: some View {
    GridRow {
      Text(label).foregroundStyle(.secondary)
      Text(value).textSelection(.enabled)
    }
  }
}

private struct StateBadge: View {
  let state: ProcessingState

  var body: some View {
    Label(state.title, systemImage: state.systemImage)
      .font(.caption.weight(.medium))
      .foregroundStyle(state.isFailure ? .orange : .secondary)
      .lineLimit(1)
  }
}

private struct DiagnosticsPane: View {
  @Bindable var log: DiagnosticsLog
  let close: () -> Void

  var body: some View {
    VStack(spacing: 0) {
      HStack {
        Label("Diagnostics", systemImage: "waveform.path.ecg")
          .font(.headline)
        Text("\(log.entries.count) events")
          .font(.caption)
          .foregroundStyle(.secondary)
        Spacer()
        Button("Copy", systemImage: "doc.on.doc") {
          NSPasteboard.general.clearContents()
          NSPasteboard.general.setString(log.text, forType: .string)
        }
        .disabled(log.entries.isEmpty)
        Button("Clear", systemImage: "trash") { log.clear() }
          .disabled(log.entries.isEmpty)
        Button("Close", systemImage: "xmark") { close() }
          .labelStyle(.iconOnly)
      }
      .padding(.horizontal, 10)
      .frame(height: 36)

      Divider()
      ScrollViewReader { proxy in
        ScrollView {
          Text(log.text.isEmpty ? "No diagnostic events yet." : log.text)
            .font(.system(.caption, design: .monospaced))
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .id(log.entries.last?.id)
        }
        .onChange(of: log.entries.count) {
          if let id = log.entries.last?.id {
            proxy.scrollTo(id, anchor: .bottom)
          }
        }
      }
      .background(.background.secondary)
    }
  }
}

private struct EmptyResultsView: View {
  var body: some View {
    ContentUnavailableView.search(text: "the current filters")
      .allowsHitTesting(false)
  }
}
