import ServiceManagement
import SwiftUI

/// Dashboard pages for lending and apps (Macs and Security live in DashboardView).
struct LendingSettings: View {
    @Environment(Engine.self) private var engine
    @State private var atLogin = SMAppService.mainApp.status == .enabled
    @State private var loginNote: String?
    @State private var limit: Double?

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
                         : "When your own apps need the CPU or GPU, macmesh slows its work to make room. It hands the work to another Mac only if you leave it almost nothing.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                Section {
                    let value = limit ?? status.limit ?? 0.9
                    LabeledContent("Use at most") {
                        HStack {
                            Slider(value: Binding(get: { value }, set: { limit = $0 }), in: 0.5...0.95, step: 0.05) { editing in
                                if !editing, let limit { Task { await engine.setOwnerLimit(limit) } }
                            }
                            .frame(width: 220)
                            Text("\(Int((value * 100).rounded()))%").monospacedDigit().frame(width: 40, alignment: .trailing)
                        }
                    }
                    if status.running > 0, let duty = status.duty, duty < 0.97 {
                        LabeledContent("Right now", value: "Others' work runs at \(Int((duty * 100).rounded()))% speed to stay under it")
                    }
                } header: {
                    Text("How much of this Mac")
                } footer: {
                    Text("This Mac as a whole, your apps and other Macs' work together, stays at or under this much of its CPU and GPU. The rest is always yours.")
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
    @State private var teaching = false

    var body: some View {
        Form {
            Section {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Teach macmesh an app").fontWeight(.medium)
                        Text("Any app with a command-line tool that renders frames. The AI writes the profile; a test render proves it.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Teach an app…") { teaching = true }.disabled(engine.ai?.using == nil)
                }
            }
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
                Text("Each app is described by a profile in macmesh's apps folder (~/.macmesh/apps).")
            }
        }
        .formStyle(.grouped)
        .sheet(isPresented: $teaching) { TeachSheet().environment(engine) }
    }
}
