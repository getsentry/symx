// swift-tools-version: 6.3

import PackageDescription

let package = Package(
  name: "SymxAdmin",
  platforms: [.macOS(.v26)],
  products: [
    .executable(name: "SymxAdmin", targets: ["SymxAdmin"])
  ],
  targets: [
    .executableTarget(
      name: "SymxAdmin",
      linkerSettings: [.linkedLibrary("sqlite3")]
    ),
    .testTarget(
      name: "SymxAdminTests",
      dependencies: ["SymxAdmin"]
    ),
  ]
)
