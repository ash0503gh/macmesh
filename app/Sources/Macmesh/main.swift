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
if let i = arguments.firstIndex(of: "--watch"), i + 1 < arguments.count, let seconds = Double(arguments[i + 1]) {
    // Run the app's own polling loop against a live engine and print every update: tests live tracking.
    MainActor.assumeIsolated {
        let engine = Engine()
        engine.watch("harness", !arguments.contains("--idle"))
        engine.launch()
        var last = ""
        let start = Date()
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { timer in
            MainActor.assumeIsolated {
                let jobs = engine.activeJobs.map { "\($0.name) \($0.state) \(Int(($0.progress * 100).rounded()))% eta \($0.eta.map { "\(Int($0))s" } ?? "-")" }
                let line = jobs.isEmpty ? "no active job (\(engine.status?.jobs.first.map { "\($0.name) \($0.state)" } ?? "none"))" : jobs.joined(separator: " | ")
                if line != last {
                    print(String(format: "%5.1fs  ", Date().timeIntervalSince(start)) + line)
                    fflush(stdout)
                    last = line
                }
                if Date().timeIntervalSince(start) > seconds { timer.invalidate(); exit(0) }
            }
        }
    }
    RunLoop.main.run()
}
if arguments.contains("--join-nearby") {
    // The app's own join path, headless: find meshes nearby and ask the first to let this Mac in.
    MainActor.assumeIsolated {
        let engine = Engine()
        Task { @MainActor in
            let found = await engine.nearby()
            print("nearby: " + found.map { "\($0.name) \($0.addresses)" }.joined(separator: ", "))
            guard let mesh = found.first(where: { arguments.contains($0.name) }) ?? found.first else { print("none"); exit(1) }
            do {
                try await engine.requestJoin(mesh, name: "appD") { print("number: \($0)"); fflush(stdout) }
                print("joined \(mesh.name); phase \(engine.phase)")
                exit(0)
            } catch {
                print("join failed: \(error.localizedDescription)")
                exit(1)
            }
        }
    }
    RunLoop.main.run()
}
MacmeshApp.main()
