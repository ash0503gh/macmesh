import AppKit
import SwiftUI

// `Macmesh --snapshot DIR [--dark]` renders every screen with sample data to PNGs in DIR and
// quits: a way to look at the UI without screen recording. Anything else starts the app.
let arguments = CommandLine.arguments
if let i = arguments.firstIndex(of: "--snapshot"), i + 1 < arguments.count {
    MainActor.assumeIsolated {
        Snapshots.render(to: URL(filePath: arguments[i + 1]), dark: arguments.contains("--dark"))
    }
    exit(0)
}
if let i = arguments.firstIndex(of: "--check") {
    // Decode what a running engine says, as the app would: catches mismatches with the engine's JSON.
    let semaphore = DispatchSemaphore(value: 0)
    Task.detached {
        let client = ControlClient()
        do {
            let status: MeshStatus = try await client.call("status")
            print("status: \(status.name), lend \(status.lend), \(status.macs.count) Macs: " +
                  status.macs.map { "\($0.name) [\(Format.macState($0))]" }.joined(separator: ", "))
            print("jobs: " + status.jobs.map { "\($0.name) \($0.state) \($0.done)/\($0.tasks)" }.joined(separator: ", "))
            let apps: [AppProfile] = try await client.call("apps")
            print("apps: " + apps.map { "\($0.name)\($0.installed ? "" : " (missing)")" }.joined(separator: ", "))
            for path in arguments.dropFirst(i + 1) {
                let found: Inspection = try await client.call("inspect", ["path": path])
                print("inspect: \(found.name) \(found.kind) \(found.app ?? "") frames \(found.frames.map(String.init) ?? "?")")
            }
        } catch {
            print("check failed: \(error.localizedDescription)")
        }
        semaphore.signal()
    }
    semaphore.wait()
    exit(0)
}
MacmeshApp.main()
