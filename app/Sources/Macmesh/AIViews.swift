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
    @State private var setupDone: [String]?
    @State private var settingUp = false

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
                Section {
                    HStack {
                        Text("Type /macmesh in Claude Code or Antigravity to run a job from there: it asks a few questions, shows the plan, and starts only when you say yes.")
                            .font(.callout).fixedSize(horizontal: false, vertical: true)
                        Spacer()
                        Button(settingUp ? "Setting up\u{2026}" : "Set up /macmesh") { Task { await setUp() } }
                            .disabled(settingUp)
                    }
                    ForEach(setupDone ?? [], id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary) }
                } header: {
                    Text("Use macmesh from your AI agent")
                } footer: {
                    Text("Adds a macmesh skill to each agent and registers macmesh's MCP server with it, for the agents installed on this Mac.")
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

    private func setUp() async {
        settingUp = true
        setupDone = await engine.setupAgents()
        settingUp = false
    }
}
