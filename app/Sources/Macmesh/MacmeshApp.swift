import SwiftUI

/// The macmesh app: a dashboard window (design B) for jobs, Macs, apps, lending and security, and a
/// small menu bar icon for status and "Lend this Mac" that keeps macmesh running with the window closed.
struct MacmeshApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var engine = Engine()

    var body: some Scene {
        Window("macmesh", id: "dashboard") {
            DashboardView()
                .environment(engine)
                .onAppear { delegate.engine = engine }
        }
        .defaultSize(width: 980, height: 640)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }

        MenuBarExtra {
            PopoverView()
                .environment(engine)
                .onAppear { engine.launch() }
        } label: {
            Image(systemName: engine.activeJobs.isEmpty ? "point.3.connected.trianglepath.dotted"
                                                         : "point.3.filled.connected.trianglepath.dotted")
                .accessibilityLabel(engine.activeJobs.isEmpty ? "macmesh" : "macmesh, a job is running")
                .onAppear {
                    engine.launch()
                    delegate.engine = engine
                }
        }
        .menuBarExtraStyle(.window)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var engine: Engine?
    /// Files opened with macmesh (Finder's Open With, or dropped on the app) wait here for the drop window.
    static var pendingFiles: [URL] = []

    func application(_ application: NSApplication, open urls: [URL]) {
        AppDelegate.pendingFiles.append(contentsOf: urls)
        NotificationCenter.default.post(name: .macmeshOpenFiles, object: nil)
    }

    /// Closing the window leaves macmesh lending in the menu bar.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { engine?.stop() }
    }
}

extension Notification.Name {
    static let macmeshOpenFiles = Notification.Name("macmeshOpenFiles")
}
