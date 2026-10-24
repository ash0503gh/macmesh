// swift-tools-version: 6.0
import PackageDescription

// The macmesh menu bar app. The engine (Python, ../macmesh) does the work; this app shows it and
// starts jobs through the engine's control socket. Build the .app with scripts/build-app.sh.
let package = Package(
    name: "MacmeshApp",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(name: "Macmesh", path: "Sources/Macmesh"),
    ],
    swiftLanguageModes: [.v5]
)
