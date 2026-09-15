# Symx Admin for macOS

An operator-focused native SwiftUI app for inspecting the read-only snapshot
produced by `symx/admin`. It deliberately targets the current operator environment:
macOS 26 and Swift tools 6.3. The tested toolchain is Swift 6.3.3 from Xcode 26.6.
The app uses only Apple frameworks plus the system SQLite library.

The existing admin TUI remains available as a fallback. This app does not replace
or remove any TUI command in its initial version.

The app currently provides:

- the active snapshot and generation metadata,
- overview and processing-state counts,
- searchable IPSW source and OTA artifact tables over complete result sets,
- independent cascading platform, version, and build filters for each artifact store,
- naturally sortable artifact-table columns,
- a copyable runtime diagnostics pane for filter, sort, selection, scroll, snapshot, and sync events,
- an auto-refreshing window for active and recently finished artifact/admin workflow runs,
- multi-artifact selection with context menus offering only common valid migrations,
- independently executable curated migration queues,
- artifact details and source links,
- automatic background sync when the snapshot is absent or over 24 hours old,
- manual sync from the toolbar or Command-R.

Sync delegates to the existing Python admin backend so snapshot creation and its
concurrency guarantees stay in one implementation. The app invokes `uv` itself and
shows failures in the window. It does **not** mutate GCS directly or download
artifacts for extraction.

## Run

Launch the app from the checkout:

```sh
cd apps/SymxAdmin
swift run
```

You can also open `Package.swift` directly in Xcode 26.6.

Verify the package with the installed Swift toolchain:

```sh
swift format lint --strict --recursive --parallel Sources Tests Package.swift
swift test
```

The default cache is `~/.cache/symx/admin`. The SQLite database is opened with
`SQLITE_OPEN_READONLY`; the app never initializes or modifies its schema. Sync
requires `uv` and the same `gh` authentication as the Python admin CLI.

## Model contract

`Sources/SymxAdmin/Models.swift` mirrors the processing-state values in
`symx/model.py`. `SnapshotRepository.swift` reads the schema built by
`symx/admin/db.py`, including nullable IPSW and OTA `last_modified` timestamps.
When either Python contract changes, update this app and its model-contract tests
in the same change.
