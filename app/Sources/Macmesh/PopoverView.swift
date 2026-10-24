import SwiftUI

/// The small menu bar view: what's running, "Lend this Mac", and a way into the dashboard.
/// Enough for someone who only lends their Mac; everything else is in the dashboard.
struct PopoverView: View {
    @Environment(Engine.self) private var engine
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Label("macmesh", systemImage: "point.3.connected.trianglepath.dotted").font(.headline)
                Spacer()
                if let status = engine.status {
                    Text("\(status.macs.count) \(status.macs.count == 1 ? "Mac" : "Macs")")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            ForEach(engine.status?.invite.requests ?? []) { JoinRequestBanner(request: $0, compact: true) }
            ForEach((engine.status?.installs ?? []).filter { $0.state != "declined" }) { InstallRequestBanner(request: $0, compact: true) }
            switch engine.phase {
            case .running:
                ForEach(engine.activeJobs) { job in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Text(job.name).lineLimit(1)
                            Spacer()
                            Text(Format.jobState(job)).font(.caption).foregroundStyle(.secondary)
                        }
                        JobProgressBar(job: job)
                    }
                }
                ForEach(engine.lendingJobs) { job in
                    Label(job.name, systemImage: "arrow.triangle.2.circlepath").font(.callout).foregroundStyle(.secondary).lineLimit(1)
                }
                if engine.activeJobs.isEmpty && engine.lendingJobs.isEmpty {
                    Text(idleLine).font(.callout).foregroundStyle(.secondary)
                }
            case .needsSetup:
                Text("Set up macmesh to start lending or borrowing.").font(.callout).foregroundStyle(.secondary)
            case .failed(let why):
                Text(why).font(.callout).foregroundStyle(.orange)
            case .missing:
                Text("macmesh can't find its engine.").font(.callout).foregroundStyle(.orange)
            case .checking, .starting:
                HStack(spacing: 8) { ProgressView().controlSize(.small); Text("Starting…").foregroundStyle(.secondary) }
            }
            if let status = engine.status {
                Divider()
                HStack {
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Lend this Mac")
                        Text(lendDetail(status)).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Toggle("Lend this Mac", isOn: Binding(get: { status.lend }, set: { on in Task { await engine.setLending(on) } }))
                        .toggleStyle(.switch)
                        .labelsHidden()
                }
            }
            Divider()
            HStack {
                Button("Open macmesh") {
                    openWindow(id: "dashboard")
                    NSApp.activate()
                }
                .keyboardShortcut("o")
                Spacer()
                Text(Format.appVersion).font(.caption2).foregroundStyle(.tertiary)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }.keyboardShortcut("q")
            }
            .buttonStyle(.borderless)
        }
        .padding(14)
        .frame(width: 280)
        .onAppear { engine.watch("menu", true) }
        .onDisappear { engine.watch("menu", false) }
    }

    private var idleLine: String {
        let others = engine.otherMacs.filter(\.available).count
        return others == 0 ? "No other Macs are available right now." : "\(others) other \(others == 1 ? "Mac is" : "Macs are") ready to help."
    }

    private func lendDetail(_ status: MeshStatus) -> String {
        if !status.lend { return "Other Macs can't use this one" }
        if status.paused > 0 { return "Paused while you're using it" }
        if status.running > 0 { return "Working for your other Macs" }
        if status.accepting == false, !status.reason.isEmpty { return "Waiting: \(status.reason)" }
        return status.ownerMode == "idle" ? "Only while you're away" : "Steps aside when you need it"
    }
}
