import SwiftUI

/// Design B: the dashboard window. Jobs, Macs, Apps, Lending and Security in a sidebar.
struct DashboardView: View {
    @Environment(Engine.self) private var engine
    @State var page: Page = .jobs
    @State private var newJob = false
    @State private var dropped: URL?

    enum Page: String, CaseIterable, Identifiable {
        case jobs = "Jobs", macs = "Macs", apps = "Apps", ai = "AI", lending = "Lending", security = "Security"
        var id: Self { self }
        var symbol: String {
            switch self {
            case .jobs: "list.bullet.rectangle"
            case .macs: "laptopcomputer.and.desktopcomputer"
            case .apps: "square.grid.2x2"
            case .ai: "sparkles"
            case .lending: "hand.raised"
            case .security: "lock.shield"
            }
        }
    }

    var body: some View {
        NavigationSplitView {
            List(Page.allCases, selection: $page) { page in
                Label(page.rawValue, systemImage: page.symbol).tag(page)
            }
            .listStyle(.sidebar)
            .navigationSplitViewColumnWidth(min: 170, ideal: 190)
            .safeAreaInset(edge: .bottom) { meshBadge }
        } detail: {
            detail
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .navigationTitle(page.rawValue)
                .toolbar {
                    ToolbarItem(placement: .primaryAction) {
                        Button { newJob = true } label: { Label("New job", systemImage: "plus") }
                            .help("Split a project or video across your Macs")
                            .disabled(engine.phase != .running)
                    }
                }
        }
        .frame(minWidth: 860, minHeight: 560)
        .sheet(isPresented: $newJob, onDismiss: { dropped = nil }) {
            DropSheetView(initialURL: dropped).environment(engine)
        }
        .dropDestination(for: URL.self) { urls, _ in
            guard engine.phase == .running, let url = urls.first else { return false }
            dropped = url
            newJob = true
            return true
        }
        .onReceive(NotificationCenter.default.publisher(for: .macmeshOpenFiles)) { _ in
            if let url = AppDelegate.pendingFiles.popLast() { dropped = url; newJob = true; page = .jobs }
        }
        .onAppear { engine.watching = true; engine.launch() }
        .onDisappear { engine.watching = false }
    }

    @ViewBuilder
    private var detail: some View {
        switch engine.phase {
        case .running:
            switch page {
            case .jobs: JobsPage(newJob: $newJob)
            case .macs: MacsPage()
            case .apps: AppsSettings()
            case .ai: AIPage()
            case .lending: LendingSettings()
            case .security: SecurityPage()
            }
        case .needsSetup:
            OnboardingView().frame(maxWidth: .infinity, maxHeight: .infinity)
        case .missing:
            ContentUnavailableView("The engine is missing", systemImage: "exclamationmark.triangle",
                                   description: Text("This copy of macmesh can't find its engine. Reinstall the app."))
        case .failed(let why):
            ContentUnavailableView("macmesh stopped", systemImage: "exclamationmark.triangle", description: Text(why))
        case .checking, .starting:
            ProgressView("Starting macmesh…").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private var meshBadge: some View {
        HStack(spacing: 6) {
            Circle().fill(engine.status?.lend == true ? Color.green : Color.secondary).frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 0) {
                Text(engine.status?.name ?? "macmesh").font(.caption).lineLimit(1)
                Text(engine.status.map { $0.lend ? "Lending · \($0.macs.count) Macs" : "Not lending · \($0.macs.count) Macs" } ?? "Not running")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(12)
    }
}

// MARK: - Jobs

struct JobsPage: View {
    @Environment(Engine.self) private var engine
    @Binding var newJob: Bool
    @State private var confirmCancel: Job?
    @State private var explaining: Job?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                if engine.activeJobs.isEmpty {
                    emptyState
                } else {
                    ForEach(engine.activeJobs) { job in
                        LiveJob(job: job) { confirmCancel = job }
                    }
                }
                history
            }
            .padding(24)
        }
        .confirmationDialog("Cancel \(confirmCancel?.name ?? "this job")?",
                            isPresented: Binding(get: { confirmCancel != nil }, set: { if !$0 { confirmCancel = nil } }),
                            presenting: confirmCancel) { job in
            Button("Cancel job", role: .destructive) { Task { await engine.cancel(job) }; confirmCancel = nil }
            Button("Keep going", role: .cancel) { confirmCancel = nil }
        } message: { _ in
            Text("Every Mac stops its part. Finished chunks are thrown away.")
        }
        .sheet(item: $explaining) { job in ExplainSheet(job: job).environment(engine) }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "square.and.arrow.down.on.square").font(.system(size: 34)).foregroundStyle(.secondary)
            Text("Drop a project or video here").font(.title3).fontWeight(.medium)
            Text("macmesh splits it across your Macs and joins the result back losslessly.")
                .foregroundStyle(.secondary)
            Button("New job…") { newJob = true }.buttonStyle(.borderedProminent).padding(.top, 4)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
        .background(RoundedRectangle(cornerRadius: 14).strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [6, 5])).foregroundStyle(.tertiary))
    }

    @ViewBuilder
    private var history: some View {
        let past = engine.status?.jobs.filter { !$0.isActive } ?? []
        if !past.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("History").font(.headline)
                Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 10) {
                    GridRow {
                        Text("Job"); Text("Result"); Text("Time"); Text("Macs"); Text("")
                    }
                    .font(.caption).foregroundStyle(.secondary)
                    Divider().gridCellUnsizedAxes(.horizontal)
                    ForEach(past) { job in
                        GridRow {
                            Label(job.name, systemImage: job.kind == "video" ? "film" : "square.stack.3d.up").lineLimit(1)
                            HStack(spacing: 5) {
                                Image(systemName: job.state == "done" ? "checkmark.circle.fill" : job.state == "failed" ? "exclamationmark.triangle.fill" : "minus.circle")
                                    .foregroundStyle(job.state == "done" ? .green : job.state == "failed" ? .orange : .secondary)
                                Text(job.state == "done" ? "Done" : job.state == "failed" ? "Failed" : "Cancelled")
                            }
                            .help(job.error)
                            Text(job.state == "done" ? Format.duration(job.elapsed) + (job.speedup.map { ", \($0.formatted())×" } ?? "") : "–")
                                .monospacedDigit().foregroundStyle(.secondary)
                            Text(job.macs.isEmpty ? "–" : "\(job.macs.count)").foregroundStyle(.secondary)
                            if job.state == "done", !job.output.isEmpty {
                                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([URL(filePath: job.output)]) }
                                    .controlSize(.small)
                            } else if job.state == "failed" {
                                Button("Explain") { explaining = job }
                                    .controlSize(.small)
                                    .disabled(engine.ai?.using == nil)
                                    .help("Ask the AI what went wrong")
                            } else {
                                Text("")
                            }
                        }
                    }
                }
            }
        }
    }
}

/// The job that's running: how far, how fast, and which Mac did what.
struct LiveJob: View {
    var job: Job
    var onCancel: () -> Void

    static let palette: [Color] = [.blue, .teal, .purple, .orange, .pink, .indigo, .mint, .brown]

    var macNames: [String] {
        var names: [String] = []
        for m in job.macs where !names.contains(m.name) { names.append(m.name) }
        return names
    }

    func color(_ mac: String) -> Color {
        Self.palette[(macNames.firstIndex(of: mac) ?? 0) % Self.palette.count]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(job.name).font(.title2).fontWeight(.semibold)
                    Text(Format.jobState(job)).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Cancel", role: .destructive, action: onCancel)
            }
            HStack(spacing: 12) {
                metric("Done", "\(Int((job.progress * 100).rounded()))%")
                metric("Time left", job.eta.map { Format.duration($0) } ?? "–")
                metric("Faster by", job.speedup.map { "\($0.formatted())×" } ?? "–")
                metric("Chunks", "\(job.done) of \(job.tasks)")
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("Chunks by Mac").font(.caption).foregroundStyle(.secondary)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 16, maximum: 22), spacing: 4)], spacing: 4) {
                    ForEach(Array(job.chunks.enumerated()), id: \.offset) { _, chunk in
                        RoundedRectangle(cornerRadius: 3)
                            .fill(chunk.mac.isEmpty ? Color.clear : color(chunk.mac).opacity(chunk.state == "done" ? 1 : 0.35))
                            .overlay(RoundedRectangle(cornerRadius: 3)
                                .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: chunk.mac.isEmpty ? [3, 2] : []))
                                .foregroundStyle(chunk.mac.isEmpty ? Color.secondary.opacity(0.5) : .clear))
                            .frame(height: 18)
                            .help(chunk.mac.isEmpty ? "Waiting" : "\(chunk.mac): \(chunk.state == "done" ? "done" : "working")")
                    }
                }
                HStack(spacing: 14) {
                    ForEach(job.macs, id: \.name) { m in
                        HStack(spacing: 5) {
                            RoundedRectangle(cornerRadius: 2).fill(color(m.name)).frame(width: 9, height: 9)
                            Text("\(m.name) · \(m.done) done\(m.running > 0 ? ", \(m.running) running" : "")")
                        }
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(18)
        .background(RoundedRectangle(cornerRadius: 14).fill(Color.primary.opacity(0.035)))
    }

    private func metric(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title2).fontWeight(.medium).monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.04)))
    }
}

// MARK: - Macs

struct MacsPage: View {
    @Environment(Engine.self) private var engine
    @State private var inviting = false

    var body: some View {
        let macs = engine.status?.macs ?? []
        VStack(alignment: .leading, spacing: 0) {
            Table(macs) {
                TableColumn("Mac") { mac in
                    HStack(spacing: 6) {
                        Label(mac.name, systemImage: mac.symbol).lineLimit(1)
                        if mac.thisMac {
                            Text("This Mac").font(.caption2).padding(.horizontal, 5).padding(.vertical, 1)
                                .background(Capsule().fill(Color.accentColor.opacity(0.15)))
                        }
                    }
                }
                .width(min: 220, ideal: 260)
                TableColumn("Chip") { mac in
                    Text(mac.chip ?? "–").lineLimit(1).help(mac.fanless ? "Fanless: slows down under long loads" : "")
                }
                .width(min: 120, ideal: 140)
                TableColumn("Cores") { mac in Text(mac.cores.map(String.init) ?? "–").monospacedDigit() }.width(50)
                TableColumn("Memory") { mac in Text(mac.memoryGb.map { "\($0) GB" } ?? "–").monospacedDigit() }.width(70)
                TableColumn("Power") { mac in
                    Text(mac.onBattery == true ? "Battery\(mac.batteryPct.map { " \($0)%" } ?? "")" : "Plugged in")
                        .foregroundStyle(.secondary)
                }
                .width(90)
                TableColumn("State") { mac in StateLabel(mac: mac) }.width(min: 150, ideal: 200)
            }
            .tableStyle(.inset)
        }
        .toolbar {
            ToolbarItem {
                Button {
                    inviting = true
                } label: {
                    Label("Add or Join Macs", systemImage: "plus.circle")
                }
                .help("Add Macs, Join a mesh, Start a new mesh, or Revoke / Leave mesh")
            }
        }
        .sheet(isPresented: $inviting) { MeshSheet().environment(engine) }
    }
}

struct MeshSheet: View {
    @Environment(Engine.self) private var engine
    @Environment(\.dismiss) private var dismiss

    enum ActionTab: String, CaseIterable, Identifiable {
        case join = "Join Mesh"
        case start = "Start Mesh"
        case invite = "Add Mac"
        case revoke = "Revoke / Leave"
        var id: Self { self }
    }

    @State private var tab: ActionTab = .join
    @State private var code = ""
    @State private var host = ""
    @State private var name = Host.current().localizedName ?? ""
    @State private var busy = false
    @State private var error: String?
    @State private var confirmLeave = false

    var body: some View {
        VStack(spacing: 16) {
            Picker("Action", selection: $tab) {
                Text("Join Mesh").tag(ActionTab.join)
                Text("Start Mesh").tag(ActionTab.start)
                if engine.status?.holdsCa == true {
                    Text("Add Mac").tag(ActionTab.invite)
                }
                Text("Revoke / Leave").tag(ActionTab.revoke)
            }
            .pickerStyle(.segmented)
            .padding(.top, 4)

            Group {
                switch tab {
                case .join:
                    joinView
                case .start:
                    startView
                case .invite:
                    inviteView
                case .revoke:
                    revokeView
                }
            }
            .frame(minHeight: 180)

            if let error {
                Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange).font(.callout)
            }

            HStack {
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 480)
        .onAppear {
            if let status = engine.status {
                if !status.holdsCa || status.macs.count <= 1 {
                    tab = .join
                } else {
                    tab = .invite
                }
            } else {
                tab = .join
            }
        }
    }

    @ViewBuilder
    private var inviteView: some View {
        let invite = engine.status?.invite
        VStack(spacing: 12) {
            Image(systemName: "plus.circle").font(.system(size: 32)).foregroundStyle(Color.accentColor)
            Text("Invite Another Mac").font(.headline)
            if invite?.state == "paired" {
                Label("\(invite?.joined ?? "The Mac") joined the mesh!", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
            } else if invite?.state == "failed" {
                Text(invite?.error ?? "The invite failed.").foregroundStyle(.orange)
            } else if let c = invite?.code {
                Text("On the other Mac, open macmesh, choose Join a mesh and enter:").foregroundStyle(.secondary).font(.callout).multilineTextAlignment(.center)
                Text(c).font(.system(size: 26, weight: .semibold, design: .monospaced)).textSelection(.enabled)
                Text("Code valid for 10 minutes.").font(.caption).foregroundStyle(.secondary)
            } else {
                ProgressView("Generating invite code…")
            }
        }
        .task {
            if engine.status?.holdsCa == true && engine.status?.invite.code == nil {
                await engine.invite()
            }
        }
    }

    @ViewBuilder
    private var joinView: some View {
        VStack(spacing: 12) {
            Image(systemName: "link.circle").font(.system(size: 32)).foregroundStyle(Color.accentColor)
            Text("Join Another Mesh").font(.headline)
            Text("Enter the 12-character code shown on the other Mac:").foregroundStyle(.secondary).font(.callout)
            Form {
                TextField("Code", text: $code, prompt: Text("4HDG-X1PN-Q5VP"))
                    .font(.system(.body, design: .monospaced))
                TextField("Address (optional, e.g. 192.168.0.103)", text: $host)
                TextField("This Mac's Name", text: $name)
            }
            .formStyle(.grouped)
            .scrollDisabled(true)

            Button(busy ? "Joining…" : "Join Mesh") {
                Task { await doJoin() }
            }
            .buttonStyle(.borderedProminent)
            .disabled(busy || code.trimmingCharacters(in: .whitespaces).count < 12)
        }
    }

    @ViewBuilder
    private var startView: some View {
        VStack(spacing: 12) {
            Image(systemName: "crown").font(.system(size: 32)).foregroundStyle(Color.accentColor)
            Text("Start a New Mesh").font(.headline)
            Text("Make this Mac the Certificate Authority and invite others.").foregroundStyle(.secondary).font(.callout)
            Form {
                TextField("Mesh Name / Mac Name", text: $name)
            }
            .formStyle(.grouped)
            .scrollDisabled(true)

            Button(busy ? "Starting…" : "Start New Mesh") {
                Task { await doStart() }
            }
            .buttonStyle(.borderedProminent)
            .disabled(busy)
        }
    }

    @ViewBuilder
    private var revokeView: some View {
        VStack(spacing: 16) {
            Image(systemName: "xmark.shield").font(.system(size: 32)).foregroundStyle(.red)
            Text("Revoke or Leave Mesh").font(.headline)

            let peers = engine.status?.macs.filter { !$0.thisMac } ?? []
            if !peers.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Connected Macs:").font(.subheadline).foregroundStyle(.secondary)
                    ForEach(peers) { mac in
                        HStack {
                            VStack(alignment: .leading) {
                                Text(mac.name).font(.system(.body, weight: .medium))
                                Text(mac.chip ?? "Apple Silicon").font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Button("Revoke", role: .destructive) {
                                Task {
                                    await engine.revoke(mac)
                                    await engine.tick()
                                }
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                        }
                        .padding(.vertical, 4)
                        Divider()
                    }
                }
                .padding(.horizontal)
            }

            VStack(spacing: 8) {
                Text("Leave / Reset This Mac").font(.subheadline).foregroundStyle(.secondary)
                Text("Disconnects this Mac from the current mesh and resets its local certificate.")
                    .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)

                Button("Leave Mesh", role: .destructive) {
                    confirmLeave = true
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
                .disabled(busy)
            }
        }
        .confirmationDialog("Leave this mesh?", isPresented: $confirmLeave) {
            Button("Leave Mesh", role: .destructive) {
                Task { await doLeave() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This Mac will disconnect from all other Macs on this mesh.")
        }
    }

    private func doJoin() async {
        busy = true; error = nil
        defer { busy = false }
        do {
            try await engine.join(code: code, host: host, name: name, force: true)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func doStart() async {
        busy = true; error = nil
        defer { busy = false }
        do {
            try await engine.createMesh(name: name, force: true)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func doLeave() async {
        busy = true; error = nil
        defer { busy = false }
        do {
            try await engine.createMesh(name: name, force: true)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - Security

struct SecurityPage: View {
    @Environment(Engine.self) private var engine
    @State private var revoking: Mac?

    var body: some View {
        Form {
            if let status = engine.status {
                Section {
                    LabeledContent("Connections", value: status.tls ? "Encrypted (mutual TLS)" : "Not encrypted")
                    LabeledContent("This Mac's certificate") {
                        Text(status.fingerprint.prefix(32) + "…").font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    }
                    LabeledContent("Certificate authority", value: status.holdsCa ? "This Mac (it invites new Macs)" : "Another Mac")
                } footer: {
                    Text("Macs join with a one-time code and get their own certificate. Work for other Macs runs in a sandbox with no network and no access to your files, and its inputs are deleted afterwards.")
                }
                Section("Macs on the mesh") {
                    ForEach(status.macs.filter { !$0.thisMac }) { mac in
                        HStack {
                            MacRow(mac: mac)
                            Button("Remove…", role: .destructive) { revoking = mac }.controlSize(.small)
                        }
                    }
                    if status.macs.count < 2 { Text("No other Macs yet.").foregroundStyle(.secondary) }
                }
            }
        }
        .formStyle(.grouped)
        .confirmationDialog("Remove \(revoking?.name ?? "this Mac")?",
                            isPresented: Binding(get: { revoking != nil }, set: { if !$0 { revoking = nil } }),
                            presenting: revoking) { mac in
            Button("Remove", role: .destructive) { Task { await engine.revoke(mac) }; revoking = nil }
        } message: { _ in
            Text("This Mac will refuse it from now on. Do the same on your other Macs, and restart macmesh here.")
        }
    }
}
