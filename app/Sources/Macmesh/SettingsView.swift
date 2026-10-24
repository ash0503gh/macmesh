import ServiceManagement
import SwiftUI

/// Dashboard pages for lending and apps (Macs and Security live in DashboardView).
struct LendingSettings: View {
    @Environment(Engine.self) private var engine
    @State private var atLogin = SMAppService.mainApp.status == .enabled
    @State private var loginNote: String?

    var body: some View {
        Form {
            if let status = engine.status {
                Section {
                    Toggle("Lend this Mac to your other Macs", isOn: Binding(get: { status.lend },
                                                                              set: { on in Task { await engine.setLending(on) } }))
                    LabeledContent("Right now", value: status.lend ? (status.running > 0 ? "Working for another Mac" : status.paused > 0 ? "Paused while you work" : "Ready") : "Not lending")
                    Toggle("Open macmesh at login", isOn: Binding(get: { atLogin }, set: { setLogin($0) }))
                    if let loginNote { Text(loginNote).font(.caption).foregroundStyle(.orange) }
                } footer: {
                    Text("Work for other Macs runs at low priority in a sandbox: no network, no access to your files, and nothing left behind when it's done.")
                }
                Section("When you're using this Mac") {
                    Picker("Lend", selection: Binding(get: { status.ownerMode ?? "shared" },
                                                      set: { mode in Task { await engine.setOwnerMode(mode) } })) {
                        Text("Step aside when I need it").tag("shared")
                        Text("Only while I'm away").tag("idle")
                    }
                    Text(status.ownerMode == "idle"
                         ? "Work stops as soon as you touch the keyboard or trackpad, and starts again after two minutes away."
                         : "When your own apps need the CPU or GPU, macmesh pauses at once and hands the work to another Mac if it lasts.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            } else {
                Text("macmesh isn't running.").foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }

    /// Login item through macOS (System Settings > General > Login Items shows and controls it too).
    private func setLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            loginNote = SMAppService.mainApp.status == .requiresApproval
                ? "Allow macmesh in System Settings > General > Login Items." : nil
        } catch {
            loginNote = "Couldn't change this: \(error.localizedDescription)"
        }
        atLogin = SMAppService.mainApp.status == .enabled
    }
}

struct AppsSettings: View {
    @Environment(Engine.self) private var engine

    var body: some View {
        Form {
            Section {
                ForEach(engine.apps) { app in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(app.name.capitalized).fontWeight(.medium)
                            Text(app.description).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if app.installed {
                            Text(app.build).font(.caption).foregroundStyle(.secondary)
                        } else {
                            Text("Not on this Mac").font(.caption).foregroundStyle(.orange)
                        }
                    }
                }
            } footer: {
                Text("Each app is described by a profile in macmesh's apps folder. Add a file there to teach macmesh a new app.")
            }
        }
        .formStyle(.grouped)
    }
}
