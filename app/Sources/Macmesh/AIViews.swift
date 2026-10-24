import SwiftUI
import UniformTypeIdentifiers

/// The AI's steps while it works, and its error if it failed.
struct AiProgress: View {
    var task: AiTask
    var agent: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(task.steps.enumerated()), id: \.offset) { i, step in
                HStack(spacing: 8) {
                    if task.state == "working", i == task.steps.count - 1 {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: "checkmark").foregroundStyle(.secondary).frame(width: 16)
                    }
                    Text(step).foregroundStyle(i == task.steps.count - 1 ? .primary : .secondary)
                }
                .font(.callout)
            }
            if task.steps.isEmpty, task.state == "working" {
                HStack(spacing: 8) { ProgressView().controlSize(.small); Text("\(agent) is thinking…").font(.callout) }
            }
            if task.state == "failed" {
                Label(task.error, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

// MARK: - Describe it (New job)

/// New job, described in words: the AI plans it; safe plans start on their own.
struct DescribeJobView: View {
    @Environment(Engine.self) private var engine
    @Environment(\.dismiss) private var dismiss

    @State var files: [URL] = []
    @State var request = ""
    @State private var turnedOff: Set<String> = []
    @State var task: AiTask?
    @State private var error: String?
    @State private var starting = false

    var macs: [Mac] { engine.status?.macs ?? [] }
    var usable: [Mac] { macs.filter { $0.available && !turnedOff.contains($0.nodeId) } }
    var agent: String { engine.ai?.using.map(AiInfo.label) ?? "The AI" }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let ai = engine.ai, ai.using == nil {
                Label(ai.problem.isEmpty ? "No AI agent is available." : ai.problem, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.orange)
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("What should macmesh do?").fontWeight(.medium)
                TextEditor(text: $request)
                    .font(.body)
                    .frame(height: 70)
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
                    .overlay(alignment: .topLeading) {
                        if request.isEmpty {
                            Text("Export Interview at 4K in HEVC, then make a 720p preview too")
                                .foregroundStyle(.tertiary).padding(.horizontal, 11).padding(.vertical, 6).allowsHitTesting(false)
                        }
                    }
            }
            files_
            MacPicker(macs: macs, turnedOff: $turnedOff)
            if let task { result(task) }
            if let error {
                Label(error, systemImage: "exclamationmark.triangle.fill").font(.callout).foregroundStyle(.orange)
            }
            footer
        }
    }

    private var files_: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                SectionLabel("Files")
                Spacer()
                Button("Add…") { add() }.controlSize(.small)
            }
            if files.isEmpty {
                Text("Add the project or media it's about (optional). The AI reads project files, never media.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(files, id: \.self) { url in
                HStack {
                    Image(systemName: "doc").foregroundStyle(.secondary)
                    Text(url.lastPathComponent).lineLimit(1)
                    Spacer()
                    Button { files.removeAll { $0 == url } } label: { Image(systemName: "minus.circle") }
                        .buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("Remove \(url.lastPathComponent)")
                }
                .font(.callout)
            }
        }
        .dropDestination(for: URL.self) { urls, _ in files.append(contentsOf: urls.filter { !files.contains($0) }); return true }
    }

    @ViewBuilder
    private func result(_ task: AiTask) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            AiProgress(task: task, agent: agent)
            if task.state == "done", let r = task.result {
                if let explanation = r.explanation, !explanation.isEmpty {
                    Text(explanation).fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 6) {
                    Text("\(r.tasks ?? 0) \(r.tasks == 1 ? "task" : "tasks") using \((r.programs ?? []).joined(separator: ", "))")
                        .font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    if r.safe == true {
                        Label("Safe: known apps only", systemImage: "checkmark.shield").font(.caption).foregroundStyle(.green)
                    } else {
                        Label("Needs your OK", systemImage: "hand.raised").font(.caption).foregroundStyle(.orange)
                    }
                }
                if let example = r.example, !example.isEmpty {
                    Text(example.joined(separator: "\n"))
                        .font(.system(.caption, design: .monospaced))
                        .lineLimit(4)
                        .textSelection(.enabled)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.05)))
                }
                ForEach(r.reasons ?? [], id: \.self) { reason in
                    Text("• It \(reason).").font(.caption).foregroundStyle(.secondary)
                }
                if r.jobId != nil {
                    Label("Started. Follow it on the Jobs page.", systemImage: "play.circle.fill").font(.callout).foregroundStyle(.green)
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.035)))
    }

    private var footer: some View {
        HStack {
            Spacer()
            Button(task?.result?.jobId != nil ? "Done" : "Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
            if let task, task.awaitingOk, task.result?.jobId == nil {
                Button(starting ? "Starting…" : "Run this plan") { Task { await run(task) } }
                    .buttonStyle(.borderedProminent)
                    .disabled(starting)
            } else if task?.result?.jobId == nil {
                Button(task?.state == "working" ? "Planning…" : task == nil ? "Plan it" : "Plan again") { Task { await plan() } }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(request.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || task?.state == "working"
                              || usable.isEmpty || engine.ai?.using == nil)
            }
        }
    }

    private func add() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        if panel.runModal() == .OK { files.append(contentsOf: panel.urls.filter { !files.contains($0) }) }
    }

    private func plan() async {
        error = nil
        do {
            let started = try await engine.plan(request: request, paths: files.map(\.path), macs: usable.map(\.nodeId))
            await engine.follow(started) { task = $0 }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func run(_ planned: AiTask) async {
        starting = true
        defer { starting = false }
        do {
            let job = try await engine.runPlan(planned.id)
            task?.result?.jobId = job.id
            task?.awaitingOk = false
        } catch {
            self.error = error.localizedDescription
        }
    }
}

// MARK: - Teach an app

struct TeachSheet: View {
    @Environment(Engine.self) private var engine
    @Environment(\.dismiss) private var dismiss

    @State var program: URL?
    @State var name = ""
    @State var sample: URL?
    @State var task: AiTask?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Teach macmesh an app").font(.title2).fontWeight(.semibold)
                Text("\(engine.ai?.using.map(AiInfo.label) ?? "The AI") reads the app's command-line help and writes its profile. macmesh then renders a few frames of your sample project whole and in chunks, and keeps the profile only if they match exactly.")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Form {
                LabeledContent("Command-line tool") {
                    HStack {
                        Text(program?.lastPathComponent ?? "None chosen").foregroundStyle(program == nil ? .secondary : .primary)
                        Button("Choose…") { choose(program: true) }
                    }
                }
                TextField("Name", text: $name, prompt: Text("blender"))
                LabeledContent("Sample project") {
                    HStack {
                        Text(sample?.lastPathComponent ?? "None chosen").foregroundStyle(sample == nil ? .secondary : .primary)
                        Button("Choose…") { choose(program: false) }
                    }
                }
            }
            .formStyle(.grouped)
            .scrollDisabled(true)
            .fixedSize(horizontal: false, vertical: true)
            if let task {
                VStack(alignment: .leading, spacing: 8) {
                    AiProgress(task: task, agent: engine.ai?.using.map(AiInfo.label) ?? "The AI")
                    if task.state == "done", let r = task.result {
                        Label("Learned \(name). \(r.verified.map { $0.prefix(1).uppercased() + $0.dropFirst() } ?? "")",
                              systemImage: "checkmark.seal.fill")
                            .foregroundStyle(.green)
                        if let explanation = r.explanation { Text(explanation).font(.callout).foregroundStyle(.secondary) }
                        Text("Your other Macs need the same tool, and this profile: copy \(r.path ?? "it") to ~/.macmesh/apps on each.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.035)))
            }
            HStack {
                Spacer()
                Button(task?.state == "done" ? "Done" : "Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                if task?.state != "done" {
                    Button(task?.state == "working" ? "Learning…" : "Teach") { Task { await start() } }
                        .buttonStyle(.borderedProminent)
                        .disabled(program == nil || sample == nil || name.isEmpty || task?.state == "working" || engine.ai?.using == nil)
                }
            }
        }
        .padding(24)
        .frame(width: 540)
    }

    private func choose(program choosingProgram: Bool) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = !choosingProgram
        panel.treatsFilePackagesAsDirectories = choosingProgram  // tools often live inside an app bundle
        panel.message = choosingProgram ? "Choose the app's command-line tool" : "Choose a project the tool can render"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        if choosingProgram {
            program = url
            if name.isEmpty { name = url.deletingPathExtension().lastPathComponent.lowercased().replacingOccurrences(of: "-cli", with: "") }
        } else {
            sample = url
        }
    }

    private func start() async {
        guard let program, let sample else { return }
        do {
            let started = try await engine.teach(program: program.path, name: name, sample: sample.path)
            await engine.follow(started) { task = $0 }
            if task?.state == "done" { await engine.refreshApps() }
        } catch {
            task = AiTask(id: "", kind: "teach", state: "failed", steps: [], result: nil, error: error.localizedDescription,
                          awaitingOk: false)
        }
    }
}

// MARK: - Explain a failure

struct ExplainSheet: View {
    @Environment(Engine.self) private var engine
    @Environment(\.dismiss) private var dismiss
    var job: Job
    @State var task: AiTask?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Why \(job.name) failed", systemImage: "questionmark.bubble").font(.title3).fontWeight(.semibold)
            Text(job.error).font(.caption).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
            if let task {
                if task.state == "done", let r = task.result {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(r.summary ?? "").fixedSize(horizontal: false, vertical: true)
                        if let fix = r.fix, !fix.isEmpty {
                            Label { Text(fix).fixedSize(horizontal: false, vertical: true) } icon: { Image(systemName: "wrench.and.screwdriver") }
                        }
                        if let place = r.where, place != "unknown" {
                            Text("Where to look: \(place)").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                } else {
                    AiProgress(task: task, agent: engine.ai?.using.map(AiInfo.label) ?? "The AI")
                }
            }
            HStack { Spacer(); Button("Done") { dismiss() }.keyboardShortcut(.defaultAction) }
        }
        .padding(22)
        .frame(width: 460)
        .task {
            guard task == nil else { return }
            do {
                let started = try await engine.explain(job)
                await engine.follow(started) { task = $0 }
            } catch {
                task = AiTask(id: "", kind: "explain", state: "failed", steps: [], result: nil, error: error.localizedDescription,
                              awaitingOk: false)
            }
        }
    }
}

// MARK: - AI page

struct AIPage: View {
    @Environment(Engine.self) private var engine

    var body: some View {
        Form {
            if let ai = engine.ai {
                Section {
                    Picker("Agent", selection: Binding(get: { ai.choice }, set: { choice in Task { await engine.setAI(agent: choice) } })) {
                        Text("Whichever is installed").tag("auto")
                        Text("Claude Code").tag("claude")
                        Text("Antigravity").tag("antigravity")
                    }
                    LabeledContent("In use", value: ai.using.map(AiInfo.label) ?? "None")
                    if !ai.problem.isEmpty { Text(ai.problem).font(.caption).foregroundStyle(.orange) }
                    LabeledContent("Installed", value: ai.available.isEmpty ? "Neither" : ai.available.map(AiInfo.label).joined(separator: ", "))
                } footer: {
                    Text("macmesh uses the AI's command-line tool on this Mac, with your existing subscription. The AI only answers; macmesh checks its answer before anything runs.")
                }
                Section("What the AI does") {
                    Toggle("Run safe plans right away", isOn: Binding(get: { ai.autorun }, set: { on in Task { await engine.setAI(autorun: on) } }))
                    Text("Safe plans use only your app profiles or ffmpeg. Anything else waits for your OK.")
                        .font(.caption).foregroundStyle(.secondary)
                    LabeledContent("Plans", value: "New job > Describe it")
                    LabeledContent("Learns apps", value: "Apps > Teach an app")
                    LabeledContent("Explains failures", value: "Jobs > History > Explain")
                }
                Section("What the AI sees") {
                    Text("Your request, the names, sizes and formats of the files you add, your Macs' specs, and the text of project files. Never the media itself.")
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Section("Who decides what runs where") {
                    Text("Splitting a job, choosing which Mac takes which chunk, and joining the pieces are fixed rules, not AI: they're instant, exact, and adapt to each Mac's measured speed.")
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Text("macmesh isn't running.").foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}
