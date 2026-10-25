import SwiftUI

/// "Describe it": plan a job by talking it through with the AI. It asks what it needs (a folder,
/// a choice), then proposes a plan as a card; nothing runs until Run. Conversations are kept.
struct ChatPlanView: View {
    @Environment(Engine.self) private var engine
    @Environment(\.dismiss) private var dismiss

    @State var files: [URL] = []
    @State var chat: ChatInfo?
    var chatId: String?  // reopen this conversation (from a job's history row)
    @State private var draft = ""
    @State private var recent: [ChatListItem] = []
    @State private var turnedOff: Set<String> = []
    @State private var error: String?
    @State private var started: Job?
    // the plan card's choices
    @State private var output = ""
    @State private var slotsHere = 1
    @State private var cardFor: String?

    var macs: [Mac] { engine.status?.macs ?? [] }
    var usable: [Mac] { macs.filter { $0.available && !turnedOff.contains($0.nodeId) } }
    var agent: String { engine.ai?.using.map(AiInfo.label) ?? "The AI" }
    var thinking: Bool { chat?.state == "thinking" }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let ai = engine.ai, ai.using == nil {
                Label(ai.problem.isEmpty ? "No AI agent is available." : ai.problem, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.orange)
            }
            if let chat { conversation(chat) } else { start }
            if let error {
                Label(error, systemImage: "exclamationmark.triangle.fill").font(.callout).foregroundStyle(.orange)
            }
            composer
        }
        .frame(minHeight: 460)
        .task {
            if let chatId, chat == nil { await open(chatId) }
            recent = await engine.chats()
        }
    }

    // ---- before the first message ------------------------------------------------
    private var start: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Tell macmesh what to do. \(agent) asks for anything it needs, like which folder, then proposes a plan you can change before running it.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if !files.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    SectionLabel("Files")
                    ForEach(files, id: \.self) { url in
                        Label(url.lastPathComponent, systemImage: "doc").font(.callout).lineLimit(1)
                    }
                }
            }
            MacPicker(macs: macs, turnedOff: $turnedOff)
            if !recent.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    SectionLabel("Recent conversations")
                    ForEach(recent.prefix(5)) { item in
                        Button {
                            Task { await open(item.id) }
                        } label: {
                            HStack {
                                Image(systemName: item.jobIds.isEmpty ? "bubble.left.and.bubble.right" : "checkmark.bubble")
                                    .foregroundStyle(.secondary)
                                Text(item.title.isEmpty ? "Untitled" : item.title).lineLimit(1)
                                Spacer()
                                if !item.jobIds.isEmpty { Text("ran").font(.caption).foregroundStyle(.secondary) }
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .font(.callout)
                    }
                }
            }
            Spacer(minLength: 0)
        }
    }

    // ---- the conversation ----------------------------------------------------------
    private func conversation(_ chat: ChatInfo) -> some View {
        ScrollViewReader { scroll in
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array(chat.messages.enumerated()), id: \.offset) { _, m in bubble(m) }
                    if thinking {
                        HStack(spacing: 8) { ProgressView().controlSize(.small); Text("\(agent) is thinking\u{2026}").foregroundStyle(.secondary) }
                            .font(.callout)
                    }
                    if !thinking, let ask = chat.askFolder {
                        Button { pickFolder() } label: { Label("Choose folder\u{2026}", systemImage: "folder") }
                            .buttonStyle(.borderedProminent)
                            .help(ask)
                    }
                    if chat.state == "failed" {
                        HStack {
                            Label(chat.error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                            Button("Try again") { Task { await send("Please try again.") } }
                        }
                        .font(.callout)
                    }
                    if let card = chat.card, !thinking { planCard(card, chat) }
                    Color.clear.frame(height: 1).id("end")
                }
                .padding(.vertical, 4)
            }
            .onChange(of: chat) { _, _ in withAnimation { scroll.scrollTo("end", anchor: .bottom) } }
            .onAppear { scroll.scrollTo("end", anchor: .bottom) }
        }
    }

    @ViewBuilder
    private func bubble(_ m: ChatInfo.Message) -> some View {
        switch m.role {
        case "user":
            HStack {
                Spacer(minLength: 60)
                Text(m.text).textSelection(.enabled).padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color.accentColor.opacity(0.18)))
            }
        case "ai":
            HStack {
                Text(m.text).textSelection(.enabled).padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color.primary.opacity(0.06)))
                Spacer(minLength: 60)
            }
        default:
            Text(m.text).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .center)
        }
    }

    // ---- the plan, ready to run ------------------------------------------------------
    private func planCard(_ card: ChatInfo.PlanCard, _ chat: ChatInfo) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("The plan", systemImage: "checklist").fontWeight(.medium)
            if !card.explanation.isEmpty { Text(card.explanation).fixedSize(horizontal: false, vertical: true) }
            Text("\(card.tasks) \(card.tasks == 1 ? "task" : "tasks") using \(card.programs.joined(separator: ", "))")
                .font(.callout).foregroundStyle(.secondary)
            if let example = card.example.first {
                Text(example).font(.system(.caption, design: .monospaced)).lineLimit(2).textSelection(.enabled)
                    .padding(6).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.05)))
            }
            ForEach(card.reasons, id: \.self) { Text("\u{2022} It \($0).").font(.caption).foregroundStyle(.orange) }
            ForEach(card.needsInstall.sorted(by: { $0.key < $1.key }), id: \.key) { mac, programs in
                Text("\u{2022} \(mac) will be asked to install \(programs.joined(separator: ", ")); its owner decides.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            MacPicker(macs: macs, turnedOff: $turnedOff)
            LabeledContent("This Mac works on") {
                Stepper("\(slotsHere) at once", value: $slotsHere, in: 1...max(1, ProcessInfo.processInfo.activeProcessorCount))
            }
            .font(.callout)
            LabeledContent("Results go to") {
                HStack {
                    TextField("Folder", text: $output).textFieldStyle(.roundedBorder)
                    Button("Choose\u{2026}") { chooseOutput() }
                }
            }
            .font(.callout)
            HStack {
                Spacer()
                if started != nil {
                    Label("Started. Follow it on the Jobs page.", systemImage: "play.circle.fill").foregroundStyle(.green)
                } else {
                    Button("Run") { Task { await run(chat) } }
                        .buttonStyle(.borderedProminent)
                        .disabled(usable.isEmpty || output.isEmpty)
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.035)))
        .onAppear { applySuggestions(chat) }
        .onChange(of: chat.card) { _, _ in applySuggestions(chat) }
    }

    /// The card starts from the AI's suggestions; they're only defaults.
    private func applySuggestions(_ chat: ChatInfo) {
        guard cardFor != chat.card?.example.first else { return }
        cardFor = chat.card?.example.first
        let root = engine.status?.outputDir ?? NSString(string: "~/Movies/macmesh").expandingTildeInPath
        let name = chat.suggest?.outputFolder ?? "\(chat.card?.name ?? "job") (macmesh)"
        output = name.hasPrefix("/") || name.hasPrefix("~") ? NSString(string: name).expandingTildeInPath
                                                            : (root as NSString).appendingPathComponent(name)
        let here = engine.status?.name ?? ""
        slotsHere = chat.suggest?.slots?.first(where: { $0.key == here || $0.key == "This Mac" })?.value
            ?? engine.status?.slots ?? 1
        if let chosen = chat.suggest?.macs, !chosen.isEmpty {
            turnedOff = Set(macs.filter { mac in !chosen.contains { $0 == mac.name || ($0 == "This Mac" && mac.thisMac) } }.map(\.nodeId))
        }
    }

    // ---- typing ----------------------------------------------------------------------
    private var composer: some View {
        HStack(alignment: .bottom, spacing: 8) {
            Button { addFiles() } label: { Image(systemName: "paperclip") }
                .buttonStyle(.borderless).help("Add a project or files (the AI reads project files, never media)")
                .disabled(chat != nil)
            TextField(chat == nil ? "e.g. Split the 600 songs into stems, in a new folder" : "Reply\u{2026}", text: $draft, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...4)
                .onSubmit { Task { await sendDraft() } }
            Button(chat == nil ? "Start" : "Send") { Task { await sendDraft() } }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.return, modifiers: .command)
                .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || thinking
                          || usable.isEmpty || engine.ai?.using == nil)
        }
    }

    private func sendDraft() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !thinking else { return }
        draft = ""
        await send(text)
    }

    private func send(_ text: String? = nil, folder: String? = nil) async {
        error = nil
        do {
            let next: ChatInfo
            if let chat {
                next = try await engine.chatSend(chat.id, message: text, folder: folder, macs: usable.map(\.nodeId))
            } else {
                next = try await engine.chatStart(text ?? "", paths: files.map(\.path), macs: usable.map(\.nodeId))
            }
            await engine.follow(chat: next) { chat = $0 }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func open(_ id: String) async {
        do { chat = try await engine.chat(id) } catch { self.error = error.localizedDescription }
    }

    private func run(_ chat: ChatInfo) async {
        error = nil
        do {
            started = try await engine.chatRun(chat.id, macs: usable.map(\.nodeId), output: output,
                                               slots: slotsHere == engine.status?.slots ? nil : slotsHere)
            self.chat = try await engine.chat(chat.id)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func pickFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.message = chat?.askFolder ?? "Choose a folder"
        if panel.runModal() == .OK, let url = panel.url { Task { await send(folder: url.path) } }
    }

    private func chooseOutput() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.message = "Where should the results go?"
        if panel.runModal() == .OK, let url = panel.url { output = url.path }
    }

    private func addFiles() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        if panel.runModal() == .OK { files.append(contentsOf: panel.urls.filter { !files.contains($0) }) }
    }
}
