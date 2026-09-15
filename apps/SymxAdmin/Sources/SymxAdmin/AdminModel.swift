import Foundation
import Observation

@MainActor
@Observable
final class AdminModel {
  private(set) var snapshot: Snapshot?
  private(set) var isLoading = false
  private(set) var isSyncing = false
  private(set) var errorMessage: String?
  private(set) var statusMessage: String?
  private(set) var ipswSources: [IPSWSource] = []
  private(set) var otaArtifacts: [OTAArtifact] = []
  private(set) var overviewIPSW: [IPSWSource] = []
  private(set) var overviewOTA: [OTAArtifact] = []
  private(set) var isFiltering = false
  private(set) var tableRevision = 0
  private(set) var ipswFacetFilter = ArtifactFacetFilter() {
    didSet { scheduleFiltering() }
  }
  private(set) var otaFacetFilter = ArtifactFacetFilter() {
    didSet { scheduleFiltering() }
  }
  private(set) var ipswFacetOptions = ArtifactFacetOptions()
  private(set) var otaFacetOptions = ArtifactFacetOptions()
  var ipswSortOrder = [IPSWSortComparator(.lastModified, order: .reverse)] {
    didSet {
      diagnostics.record("sort", "IPSW order=\(describe(ipswSortOrder))")
      scheduleFiltering()
    }
  }
  var otaSortOrder = [OTASortComparator(.lastModified, order: .reverse)] {
    didSet {
      diagnostics.record("sort", "OTA order=\(describe(otaSortOrder))")
      scheduleFiltering()
    }
  }
  var searchText = "" {
    didSet { scheduleFiltering() }
  }
  var selectedStates = ProcessingState.defaultFailures {
    didSet { scheduleFiltering() }
  }
  var dateRange = DateRangePreset.fourWeeks {
    didSet { scheduleFiltering() }
  }

  let cacheURL: URL
  let diagnostics = DiagnosticsLog()
  @ObservationIgnored lazy var migrationQueue = MigrationQueueModel(diagnostics: diagnostics)
  @ObservationIgnored lazy var workflowTasks = WorkflowTasksModel(diagnostics: diagnostics)
  private var filterTask: Task<Void, Never>?

  init(cacheURL: URL = SnapshotRepository.defaultCacheURL) {
    self.cacheURL = cacheURL
  }

  var failureCount: Int {
    overviewIPSW.count(where: { $0.state.isFailure })
      + overviewOTA.count(where: { $0.state.isFailure })
  }

  func start() async {
    await load()
    if snapshot == nil || snapshotIsStale {
      await sync()
    }
  }

  func load() async {
    guard !isLoading else { return }
    isLoading = true
    errorMessage = nil
    let repository = SnapshotRepository(cacheURL: cacheURL)
    do {
      snapshot = try await Task.detached { try repository.load() }.value
      if let snapshot {
        diagnostics.record(
          "snapshot",
          "loaded id=\(snapshot.info.id) ipsw=\(snapshot.ipswSources.count) ota=\(snapshot.otaArtifacts.count)"
        )
      }
      scheduleFiltering()
    } catch {
      errorMessage = error.localizedDescription
      diagnostics.record("snapshot", "load failed: \(error.localizedDescription)")
    }
    isLoading = false
  }

  func sync() async {
    guard !isSyncing else { return }
    isSyncing = true
    errorMessage = nil
    statusMessage = "Syncing metadata through GitHub Actions…"
    diagnostics.record("sync", "started")
    let service = AdminSyncService(cacheURL: cacheURL)
    do {
      let result = try await Task.detached { try service.sync() }.value
      statusMessage = result.lines.last ?? "Sync complete"
      diagnostics.record("sync", statusMessage ?? "completed")
      await load()
    } catch {
      errorMessage = error.localizedDescription
      statusMessage = nil
      diagnostics.record("sync", "failed: \(error.localizedDescription)")
    }
    isSyncing = false
  }

  var snapshotIsStale: Bool {
    guard let createdAt = snapshot?.info.createdAt else { return true }
    return createdAt < Date.now.addingTimeInterval(-24 * 60 * 60)
  }

  func dismissError() {
    errorMessage = nil
  }

  func resetFilters() {
    selectedStates = ProcessingState.defaultFailures
  }

  func showAllStates() {
    selectedStates = Set(ProcessingState.allCases)
  }

  func facetFilter(for store: ArtifactStore) -> ArtifactFacetFilter {
    store == .ipsw ? ipswFacetFilter : otaFacetFilter
  }

  func facetOptions(for store: ArtifactStore) -> ArtifactFacetOptions {
    store == .ipsw ? ipswFacetOptions : otaFacetOptions
  }

  func selectAll(_ facet: ArtifactFacet, for store: ArtifactStore) {
    diagnostics.record("facet", "\(store) \(facet) reset=all")
    updateFacetFilter(for: store) { filter in
      switch facet {
      case .platform:
        filter.platforms = []
        filter.versions = []
        filter.builds = []
      case .version:
        filter.versions = []
        filter.builds = []
      case .build:
        filter.builds = []
      }
    }
  }

  func toggleFacetValue(_ value: String, facet: ArtifactFacet, store: ArtifactStore) {
    diagnostics.record("facet", "\(store) \(facet) toggle=\(value)")
    updateFacetFilter(for: store) { filter in
      switch facet {
      case .platform:
        toggle(value, in: &filter.platforms)
        filter.versions = []
        filter.builds = []
      case .version:
        toggle(value, in: &filter.versions)
        filter.builds = []
      case .build:
        toggle(value, in: &filter.builds)
      }
    }
  }

  func resetFacetFilters(for store: ArtifactStore) {
    if store == .ipsw {
      ipswFacetFilter = ArtifactFacetFilter()
    } else {
      otaFacetFilter = ArtifactFacetFilter()
    }
  }

  private func updateFacetFilter(
    for store: ArtifactStore, update: (inout ArtifactFacetFilter) -> Void
  ) {
    if store == .ipsw {
      var filter = ipswFacetFilter
      update(&filter)
      ipswFacetFilter = filter
    } else {
      var filter = otaFacetFilter
      update(&filter)
      otaFacetFilter = filter
    }
  }

  private func scheduleFiltering() {
    filterTask?.cancel()
    guard let snapshot else {
      ipswSources = []
      otaArtifacts = []
      overviewIPSW = []
      overviewOTA = []
      return
    }

    let states = selectedStates
    let search = searchText
    let ipswFacets = ipswFacetFilter
    let otaFacets = otaFacetFilter
    let ipswSort = ipswSortOrder
    let otaSort = otaSortOrder
    let selectedDateRange = dateRange
    let modifiedSince = selectedDateRange.cutoff(relativeTo: .now)
    isFiltering = true
    diagnostics.record(
      "filter",
      "requested range=\(selectedDateRange.title) states=\(states.count) "
        + "search=\(search.isEmpty ? "<empty>" : search) "
        + "ipsw=\(describe(ipswFacets)) ota=\(describe(otaFacets))"
    )
    filterTask = Task {
      let (result, elapsed) = await Task.detached {
        let started = ContinuousClock.now
        let result = filterSnapshot(
          snapshot,
          states: states,
          search: search,
          modifiedSince: modifiedSince,
          ipswFacets: ipswFacets,
          otaFacets: otaFacets,
          ipswSort: ipswSort,
          otaSort: otaSort
        )
        return (result, started.duration(to: .now))
      }.value
      guard !Task.isCancelled else { return }

      // This is deliberately one main-actor publication. Changing the table identity
      // tells SwiftUI to construct the new virtualized table instead of diffing two
      // unrelated filtered result sets through its AppKit selection coordinator.
      ipswSources = result.ipsw
      otaArtifacts = result.ota
      overviewIPSW = result.overviewIPSW
      overviewOTA = result.overviewOTA
      ipswFacetOptions = result.ipswOptions
      otaFacetOptions = result.otaOptions
      tableRevision += 1
      isFiltering = false
      diagnostics.record(
        "filter",
        "published revision=\(tableRevision) ipsw=\(result.ipsw.count) ota=\(result.ota.count) "
          + "duration=\(milliseconds(elapsed))ms"
      )
    }
  }
}

struct FilteredSnapshotRows: Sendable {
  let ipsw: [IPSWSource]
  let ota: [OTAArtifact]
  let overviewIPSW: [IPSWSource]
  let overviewOTA: [OTAArtifact]
  let ipswOptions: ArtifactFacetOptions
  let otaOptions: ArtifactFacetOptions
}

func filterSnapshot(
  _ snapshot: Snapshot,
  states: Set<ProcessingState>,
  search: String,
  modifiedSince: Date? = nil,
  ipswFacets: ArtifactFacetFilter = ArtifactFacetFilter(),
  otaFacets: ArtifactFacetFilter = ArtifactFacetFilter(),
  ipswSort: [IPSWSortComparator] = [IPSWSortComparator(.lastModified, order: .reverse)],
  otaSort: [OTASortComparator] = [OTASortComparator(.lastModified, order: .reverse)]
) -> FilteredSnapshotRows {
  let overviewIPSW = snapshot.ipswSources.filter {
    isInDateRange($0.lastModified, since: modifiedSince)
  }
  let overviewOTA = snapshot.otaArtifacts.filter {
    isInDateRange($0.lastModified, since: modifiedSince)
  }

  // Facet choices come from rows that can actually be displayed under the shared
  // date/state/search constraints. Each downstream facet then applies its parents.
  let ipswCandidates = overviewIPSW.filter { row in
    states.contains(row.state)
      && matches(
        search, fields: row.fileName, row.artifactKey, row.platform, row.version, row.build)
  }
  let otaCandidates = overviewOTA.filter { row in
    states.contains(row.state)
      && matches(search, fields: row.artifactID, row.otaKey, row.platform, row.version, row.build)
  }
  let ipsw =
    ipswCandidates
    .filter { ipswFacets.matches(platform: $0.platform, version: $0.version, build: $0.build) }
    .sorted(using: ipswSort)
  let ota =
    otaCandidates
    .filter { otaFacets.matches(platform: $0.platform, version: $0.version, build: $0.build) }
    .sorted(using: otaSort)
  return FilteredSnapshotRows(
    ipsw: ipsw,
    ota: ota,
    overviewIPSW: overviewIPSW,
    overviewOTA: overviewOTA,
    ipswOptions: facetOptions(
      ipswCandidates.map { ($0.platform, $0.version, $0.build) },
      filter: ipswFacets
    ),
    otaOptions: facetOptions(
      otaCandidates.map { ($0.platform, $0.version, $0.build) },
      filter: otaFacets
    )
  )
}

private func isInDateRange(_ lastModified: Date?, since cutoff: Date?) -> Bool {
  guard let cutoff else { return true }
  guard let lastModified else { return false }
  return lastModified >= cutoff
}

private func facetOptions(
  _ values: [(platform: String, version: String, build: String)],
  filter: ArtifactFacetFilter
) -> ArtifactFacetOptions {
  let platforms = naturallySorted(Set(values.map(\.platform)))
  let platformRows = values.filter {
    filter.platforms.isEmpty || filter.platforms.contains($0.platform)
  }
  let versions = naturallySorted(Set(platformRows.map(\.version)))
  let versionRows = platformRows.filter {
    filter.versions.isEmpty || filter.versions.contains($0.version)
  }
  let builds = naturallySorted(Set(versionRows.map(\.build)))
  return ArtifactFacetOptions(platforms: platforms, versions: versions, builds: builds)
}

private func naturallySorted(_ values: Set<String>) -> [String] {
  values.sorted { $0.localizedStandardCompare($1) == .orderedAscending }
}

private func toggle(_ value: String, in selection: inout Set<String>) {
  if selection.isEmpty {
    selection = [value]
  } else if selection.contains(value) {
    selection.remove(value)
  } else {
    selection.insert(value)
  }
}

private func matches(_ search: String, fields: String...) -> Bool {
  search.isEmpty || fields.contains { $0.localizedStandardContains(search) }
}

private func describe(_ filter: ArtifactFacetFilter) -> String {
  "platform=\(filter.platforms.sorted()) version=\(filter.versions.sorted()) build=\(filter.builds.sorted())"
}

private func describe(_ order: [IPSWSortComparator]) -> String {
  order.map { "\($0.field):\($0.order)" }.joined(separator: ",")
}

private func describe(_ order: [OTASortComparator]) -> String {
  order.map { "\($0.field):\($0.order)" }.joined(separator: ",")
}

private func milliseconds(_ duration: Duration) -> String {
  let components = duration.components
  let value =
    Double(components.seconds) * 1_000 + Double(components.attoseconds) / 1_000_000_000_000_000
  return String(format: "%.1f", value)
}

extension String {
  fileprivate var lines: [String] { split(whereSeparator: \.isNewline).map(String.init) }
}
