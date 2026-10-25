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
                .safeAreaInset(edge: .top, spacing: 0) { banners }
                .navigationTitle(page.rawValue)
                .toolbar {
                    ToolbarItem(placement: .automatic) { AppearanceMenu() }
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
        .onAppear { engine.watch("dashboard", true); engine.launch() }
        .onDisappear { engine.watch("dashboard", false) }
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
            if page == .jobs {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        OnboardingView()
                            .frame(maxWidth: .infinity)
                            .background(RoundedRectangle(cornerRadius: 14).fill(Color.primary.opacity(0.035)))
                        Label("Once this Mac is in a mesh, drop a project, video or songs here to split the work across your Macs.",
                              systemImage: "square.and.arrow.down.on.square")
                            .foregroundStyle(.secondary)
                    }
                    .padding(24)
                    .frame(maxWidth: 760)
                    .frame(maxWidth: .infinity)
                }
            } else {
                ContentUnavailableView {
                    Label("Join or start a mesh first", systemImage: "point.3.connected.trianglepath.dotted")
                } description: {
                    Text("macmesh shares work between the Macs in a mesh.")
                } actions: {
                    Button("Go to Jobs") { page = .jobs }.buttonStyle(.borderedProminent)
                }
            }
        case .missing:
            ContentUnavailableView("The engine is missing", systemImage: "exclamationmark.triangle",
                                   description: Text("This copy of macmesh can't find its engine. Reinstall the app."))
        case .failed(let why):
            ContentUnavailableView("macmesh stopped", systemImage: "exclamationmark.triangle", description: Text(why))
        case .checking, .starting:
            ProgressView("Starting macmesh…").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    /// A Mac asking to join, or the firewall in the way: above every page until dealt with.
    @ViewBuilder
    private var banners: some View {
        if engine.phase == .running, let status = engine.status,
           !(status.invite.requests ?? []).isEmpty || status.firewall?.blocked == true || !installs(status).isEmpty {
            VStack(spacing: 8) {
                ForEach(status.invite.requests ?? []) { JoinRequestBanner(request: $0) }
                ForEach(installs(status)) { InstallRequestBanner(request: $0) }
                if let firewall = status.firewall, firewall.blocked { FirewallBanner(firewall: firewall) }
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 12)
            .background(.bar)
        }
    }

    private func installs(_ status: MeshStatus) -> [MeshStatus.InstallRequest] {
        (status.installs ?? []).filter { $0.state != "declined" }
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
            Text(Format.appVersion).font(.caption2).foregroundStyle(.tertiary).monospacedDigit()
                .help("macmesh \(Format.appVersion)")
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
    @State private var chatting: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                MeshStrip()
                if engine.activeJobs.isEmpty {
                    emptyState
                } else {
                    ForEach(engine.activeJobs) { job in
                        LiveJob(job: job) { confirmCancel = job }
                    }
                }
                if !engine.lendingJobs.isEmpty { lending }
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
        .sheet(isPresented: Binding(get: { chatting != nil }, set: { if !$0 { chatting = nil } })) {
            VStack(alignment: .leading, spacing: 18) {
                SheetTitleBar(title: "Conversation") { EmptyView() }
                ChatPlanView(chatId: chatting).environment(engine)
            }
            .padding(22).frame(width: 880, height: 700)
        }
    }

    /// Work this Mac is doing for your other Macs.
    private var lending: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Helping other Macs").font(.headline)
            ForEach(engine.lendingJobs) { job in
                HStack(spacing: 10) {
                    Image(systemName: job.state == "paused" ? "pause.circle" : "arrow.triangle.2.circlepath")
                        .foregroundStyle(job.state == "paused" ? .orange : .secondary)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(job.name).lineLimit(1)
                        Text(Format.jobState(job)).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if job.state == "running" { ProgressView().controlSize(.small) }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.035)))
            }
        }
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
                            if let chat = job.chat {
                                Button("Chat") { chatting = chat }.controlSize(.small)
                                    .help("Reopen the conversation that planned this job, to change it and run it again")
                            } else if job.state == "done", !job.output.isEmpty {
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
            JobProgressBar(job: job)
            HStack(spacing: 12) {
                metric("Done", job.tasks == 0 ? "\u{2013}" : "\(job.done * 100 / job.tasks)%")
                metric("Time", job.eta.map { "\(Format.duration(job.elapsed)) of ~\(Format.duration(job.elapsed + $0))" }
                                ?? (job.elapsed > 0 ? Format.duration(job.elapsed) : "\u{2013}"))
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
            JobDetails(job: job, color: color)
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
        .safeAreaInset(edge: .bottom) {
            HStack {
                Spacer()
                Button("Manage mesh\u{2026}") { inviting = true }
                    .help("Add a Mac, answer join requests, remove a Mac, or leave this mesh")
            }
            .padding(12)
        }
        .sheet(isPresented: $inviting) { MeshSheet().environment(engine) }
    }
}

struct MeshSheet: View {
    @Environment(Engine.self) private var engine
    @Environment(\.dismiss) private var dismiss
    @State private var showCode = false
    @State private var confirmLeave = false
    @State private var revoking: Mac?
    @State private var error: String?

    var body: some View {
        let status = engine.status
        VStack(alignment: .leading, spacing: 18) {
            Text("Your mesh").font(.title2).fontWeight(.semibold)
            if let firewall = status?.firewall, firewall.blocked { FirewallBanner(firewall: firewall) }
            ForEach(status?.invite.requests ?? []) { JoinRequestBanner(request: $0) }

            VStack(alignment: .leading, spacing: 6) {
                SectionLabel("Add a Mac")
                if status?.holdsCa == true, status?.invite.full == true {
                    if let joined = status?.invite.joined {
                        Label("\(joined) joined", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    }
                    Text("This mesh has \(status?.invite.maxMacs ?? 2) Macs, the most for now. To add a different Mac, remove one below first.")
                        .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                } else if status?.holdsCa == true {
                    Text("On the other Mac, install macmesh and open it. It finds \u{201C}\(status?.name ?? "this Mac")\u{201D} on the network: click Join there, then Allow here when the numbers match.")
                        .fixedSize(horizontal: false, vertical: true)
                    if let joined = status?.invite.joined {
                        Label("\(joined) joined", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    }
                    DisclosureGroup("Use a code instead", isExpanded: $showCode) {
                        if let code = status?.invite.code {
                            HStack {
                                Text(code).font(.system(.title3, design: .monospaced)).textSelection(.enabled)
                                Spacer()
                                Text("On the other Mac: macmesh join \(code)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                            }
                            .padding(.top, 4)
                        } else {
                            ProgressView().controlSize(.small).padding(.top, 4)
                        }
                    }
                    .font(.callout)
                    .onChange(of: showCode) { _, open in if open { Task { await engine.invite() } } }
                } else {
                    Text("New Macs are let in by the Mac that started this mesh: open macmesh there to allow them.")
                        .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                SectionLabel("Macs in this mesh")
                ForEach(status?.macs ?? []) { mac in
                    HStack {
                        MacRow(mac: mac)
                        if !mac.thisMac {
                            Button("Remove…", role: .destructive) { revoking = mac }.controlSize(.small)
                        }
                    }
                }
            }

            HStack {
                Button("Leave this mesh…", role: .destructive) { confirmLeave = true }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            if let error { Text(error).font(.callout).foregroundStyle(.orange) }
        }
        .padding(24)
        .frame(width: 520)
        .confirmationDialog("Leave this mesh?", isPresented: $confirmLeave) {
            Button("Leave", role: .destructive) {
                Task {
                    do { try await engine.leave(); dismiss() } catch { self.error = error.localizedDescription }
                }
            }
        } message: {
            Text("This Mac stops lending and borrowing, and forgets the mesh. You can join one again from the start screen.")
        }
        .confirmationDialog("Remove \(revoking?.name ?? "this Mac")?",
                            isPresented: Binding(get: { revoking != nil }, set: { if !$0 { revoking = nil } }),
                            presenting: revoking) { mac in
            Button("Remove", role: .destructive) { Task { await engine.revoke(mac) }; revoking = nil }
        } message: { _ in
            Text("This Mac will refuse it from now on. Do the same on your other Macs.")
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

/// The top of Jobs once this Mac is in a mesh: which Macs are in it, and where to manage it.
struct MeshStrip: View {
    @Environment(Engine.self) private var engine
    @State private var managing = false

    var body: some View {
        let macs = engine.status?.macs ?? []
        HStack(spacing: 10) {
            Image(systemName: "point.3.connected.trianglepath.dotted").foregroundStyle(.secondary)
            Text("Your mesh").fontWeight(.medium)
            if macs.count <= 1 {
                Text("Only this Mac so far. On another Mac, open macmesh and click Join.")
                    .foregroundStyle(.secondary).lineLimit(1)
            } else {
                ForEach(macs) { mac in
                    HStack(spacing: 5) {
                        Circle().fill(mac.state == "unavailable" ? Color.secondary.opacity(0.4) : mac.state == "paused" ? .orange : .green)
                            .frame(width: 7, height: 7)
                        Text(mac.thisMac ? "This Mac" : mac.name).lineLimit(1)
                    }
                    .help(Format.macState(mac))
                }
            }
            Spacer(minLength: 8)
            Button("Manage\u{2026}") { managing = true }
                .help("Add a Mac, answer join requests, remove a Mac, or leave this mesh")
        }
        .font(.callout)
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.035)))
        .sheet(isPresented: $managing) { MeshSheet().environment(engine) }
    }
}
