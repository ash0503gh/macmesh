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
            CommandGroup(after: .sidebar) {
                Picker("Appearance", selection: Binding(get: { Appearance.current }, set: { choice in
                    UserDefaults.standard.set(choice.rawValue, forKey: Appearance.key)
                    Appearance.apply(choice)
                })) {
                    ForEach(Appearance.allCases) { Text($0.title).tag($0) }
                }
            }
        }

        MenuBarExtra {
            PopoverView()
                .environment(engine)
                .onAppear { engine.launch() }
        } label: {
            MenuBarLabel(engine: engine)
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

    func applicationDidFinishLaunching(_ notification: Notification) {
        MainActor.assumeIsolated { Appearance.apply() }
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

/// The menu bar icon. It also brings the dashboard forward when a Mac asks to join or to install
/// something, since those wait for the owner's answer.
struct MenuBarLabel: View {
    var engine: Engine
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let waiting = !engine.waitingOnOwner.isEmpty
        Image(nsImage: MenuBarLabel.icon(working: !engine.activeJobs.isEmpty || !engine.lendingJobs.isEmpty, waiting: waiting))
            .accessibilityLabel(waiting ? "macmesh, waiting for your answer"
                                : engine.activeJobs.isEmpty ? "macmesh" : "macmesh, a job is running")
            .onChange(of: engine.waitingOnOwner) { old, new in
                if !new.subtracting(old).isEmpty {
                    openWindow(id: "dashboard")
                    NSApp.activate()
                    NSApp.requestUserAttention(.criticalRequest)
                }
            }
    }
}

extension MenuBarLabel {
    /// The logo for the menu bar: two Macs and the link between them, as a template image so it
    /// follows the menu bar's colours. Screens fill in while work runs; a dot means a Mac is waiting
    /// for an answer.
    static func icon(working: Bool, waiting: Bool) -> NSImage {
        let image = NSImage(size: NSSize(width: 24, height: 16), flipped: false) { _ in
            NSColor.black.set()
            for x in [0.5, 15.0] {
                let screen = NSRect(x: x, y: 6, width: 8.5, height: 7)
                let bezel = NSBezierPath(roundedRect: screen, xRadius: 1.6, yRadius: 1.6)
                if working { bezel.fill() } else { bezel.lineWidth = 1.4; bezel.stroke() }
                NSBezierPath(rect: NSRect(x: x + 3.25, y: 3.2, width: 2, height: 2.8)).fill()
                NSBezierPath(roundedRect: NSRect(x: x + 1.75, y: 2, width: 5, height: 1.4), xRadius: 0.7, yRadius: 0.7).fill()
            }
            // The link: a small spark between the two, clear of both screens.
            let spark = NSBezierPath()
            spark.move(to: NSPoint(x: 12, y: 12))
            spark.line(to: NSPoint(x: 12.9, y: 9.5))
            spark.line(to: NSPoint(x: 12, y: 7))
            spark.line(to: NSPoint(x: 11.1, y: 9.5))
            spark.close()
            spark.fill()
            if waiting {
                NSGraphicsContext.current?.compositingOperation = .clear
                NSBezierPath(ovalIn: NSRect(x: 17, y: 9, width: 8, height: 8)).fill()
                NSGraphicsContext.current?.compositingOperation = .sourceOver
                NSBezierPath(ovalIn: NSRect(x: 18.3, y: 10.3, width: 5.4, height: 5.4)).fill()
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}
