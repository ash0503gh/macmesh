import SwiftUI

/// "Describe it": plan a job by talking it through with the AI. It asks what it needs (a folder,
/// a choice), then proposes a plan as a card; nothing runs until Run. Past conversations are in a
/// list beside it, like Messages.
struct ChatPlanView: View {
    @Environment(Engine.self) private var engine

    @State var files: [URL] = []
    @State var chat: ChatInfo?
    var chatId: String?  // reopen this conversation (from a job's history row)
    @State private var draft = ""
    @State var recent: [ChatListItem] = []
    @State private var turnedOff: Set<String> = []
    @State private var error: String?
    @State private var started: Job?
    @State private var deleted: ChatListItem?  // just deleted: Undo brings it back
    @State private var hovered: String?
    // the plan card's choices
    @State private var outputName = ""
    @State private var outputParent = ""
    @State private var slotsHere = 1
    @State private var cardFor: String?

    var macs: [Mac] { engine.status?.macs ?? [] }
    var usable: [Mac] { macs.filter { $0.available && !turnedOff.contains($0.nodeId) } }
    var agent: String { engine.ai?.using.map(AiInfo.label) ?? "The AI" }
    var thinking: Bool { chat?.state == "thinking" }
    var output: String { (outputParent as NSString).appendingPathComponent(outputName) }

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            sidebar.frame(width: 220)
            Divider()
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
            .padding(.leading, 16)
        }
        .frame(minHeight: 520)
        .task {
            if let chatId, chat == nil { await open(chatId) }
            await reloadList()
        }
    }

    // ---- past conversations ------------------------------------------------------------
    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button { newChat() } label: { Label("New conversation", systemImage: "square.and.pencil") }
                .buttonStyle(.borderless)
                .padding(.bottom, 4)
            if recent.isEmpty {
                Text("Your conversations show up here.").font(.caption).foregroundStyle(.tertiary)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(recent) { item in row(item) }
                }
            }
            Spacer(minLength: 0)
            if let deleted {
                HStack {
                    Text("Deleted").foregroundStyle(.secondary)
                    Spacer()
                    Button("Undo") { Task { await restore(deleted) } }.buttonStyle(.borderless)
                }
                .font(.caption)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.06)))
            }
        }
        .padding(.trailing, 12)
    }

    private func row(_ item: ChatListItem) -> some View {
        let selected = item.id == chat?.id
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title.isEmpty ? "Untitled" : item.title).lineLimit(1)
                Text("\(Format.when(item.updated ?? item.created ?? 0)) \u{00B7} \(item.jobIds.isEmpty ? "Not run" : "Ran")")
                    .font(.caption).foregroundStyle(item.jobIds.isEmpty ? .secondary : Color.green)
            }
            Spacer(minLength: 4)
            Button { Task { await delete(item) } } label: { Image(systemName: "trash") }
                .buttonStyle(.borderless).foregroundStyle(.secondary)
                .help("Delete this conversation (you can undo)")
                .opacity(selected || hovered == item.id ? 1 : 0)
        }
        .onHover { inside in hovered = inside ? item.id : (hovered == item.id ? nil : hovered) }
        .font(.callout)
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 7).fill(selected ? Color.accentColor.opacity(0.18) : .clear))
        .contentShape(Rectangle())
        .onTapGesture { Task { await open(item.id) } }
        .contextMenu { Button("Delete", role: .destructive) { Task { await delete(item) } } }
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
            VStack(alignment: .leading, spacing: 6) {
                SectionLabel("Use these Macs")
                MacChecks(macs: macs, turnedOff: $turnedOff)
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
                .padding(.trailing, 4)
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

    // ---- the plan, ready to run: compact rows ------------------------------------------
    private func planCard(_ card: ChatInfo.PlanCard, _ chat: ChatInfo) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(card.name).fontWeight(.semibold)
                Text("\(card.tasks) \(card.tasks == 1 ? "task" : "tasks") with \(card.programs.joined(separator: ", "))")
                    .font(.callout).foregroundStyle(.secondary)
                    .help(card.example.first.map { "First task: \($0)" } ?? "")
            }
            ForEach(card.reasons, id: \.self) { Label("It \($0).", systemImage: "hand.raised").font(.caption).foregroundStyle(.orange) }
            ForEach(card.needsInstall.sorted(by: { $0.key < $1.key }), id: \.key) { mac, programs in
                Label("\(mac) will be asked to install \(programs.joined(separator: ", "))", systemImage: "shippingbox")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 9) {
                GridRow {
                    Text("Macs").foregroundStyle(.secondary)
                    MacChecks(macs: macs, turnedOff: $turnedOff)
                }
                GridRow {
                    Text("At once").foregroundStyle(.secondary)
                    Stepper("This Mac: \(slotsHere)", value: $slotsHere, in: 1...max(1, ProcessInfo.processInfo.activeProcessorCount))
                        .help("How many tasks this Mac runs at the same time")
                }
                GridRow {
                    Text("Results").foregroundStyle(.secondary)
                    HStack(spacing: 6) {
                        TextField("Folder name", text: $outputName).textFieldStyle(.roundedBorder).frame(maxWidth: 200)
                        Text("in \((outputParent as NSString).abbreviatingWithTildeInPath)")
                            .foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        Spacer(minLength: 4)
                        Button("Change\u{2026}") { chooseOutput() }
                    }
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
                        .disabled(usable.isEmpty || outputName.trimmingCharacters(in: .whitespaces).isEmpty)
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
        let suggested = chat.suggest?.outputFolder ?? "\(chat.card?.name ?? "job") (macmesh)"
        if suggested.hasPrefix("/") || suggested.hasPrefix("~") {
            let full = NSString(string: suggested).expandingTildeInPath as NSString
            outputParent = full.deletingLastPathComponent
            outputName = full.lastPathComponent
        } else {
            outputParent = root
            outputName = suggested
        }
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

    // ---- actions -----------------------------------------------------------------------
    private func newChat() {
        chat = nil
        started = nil
        cardFor = nil
        error = nil
        draft = ""
    }

    private func reloadList() async {
        if let list = await engine.chats() { recent = list }
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
            await reloadList()
            await engine.follow(chat: next) { chat = $0 }
            await reloadList()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func open(_ id: String) async {
        do {
            chat = try await engine.chat(id)
            started = nil
            cardFor = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func delete(_ item: ChatListItem) async {
        do {
            try await engine.chatDelete(item.id)
            if chat?.id == item.id { newChat() }
            deleted = item
            await reloadList()
            try? await Task.sleep(for: .seconds(8))
            if deleted?.id == item.id { deleted = nil }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func restore(_ item: ChatListItem) async {
        deleted = nil
        do {
            chat = try await engine.chatRestore(item.id)
            await reloadList()
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func run(_ chat: ChatInfo) async {
        error = nil
        do {
            started = try await engine.chatRun(chat.id, macs: usable.map(\.nodeId), output: output,
                                               slots: slotsHere == engine.status?.slots ? nil : slotsHere)
            self.chat = try await engine.chat(chat.id)
            await reloadList()
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
        panel.message = "Where should the \u{201C}\(outputName)\u{201D} folder go?"
        panel.prompt = "Put it here"
        if panel.runModal() == .OK, let url = panel.url { outputParent = url.path }
    }

    private func addFiles() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        if panel.runModal() == .OK { files.append(contentsOf: panel.urls.filter { !files.contains($0) }) }
    }
}

/// Macs as a row of checkboxes (compact; the Mac picker's circles are for the file sheet).
struct MacChecks: View {
    var macs: [Mac]
    @Binding var turnedOff: Set<String>

    var body: some View {
        HStack(spacing: 14) {
            ForEach(macs) { mac in
                Toggle(isOn: Binding(get: { mac.available && !turnedOff.contains(mac.nodeId) },
                                     set: { on in if on { turnedOff.remove(mac.nodeId) } else { turnedOff.insert(mac.nodeId) } })) {
                    Text(mac.thisMac ? "This Mac" : mac.name).lineLimit(1)
                }
                .toggleStyle(.checkbox)
                .disabled(!mac.available)
                .help(mac.available ? mac.name : "\(mac.name): \(Format.macState(mac))")
            }
        }
    }
}

/// A sheet's title and its close button (Esc closes too).
struct SheetTitleBar<Accessory: View>: View {
    @Environment(\.dismiss) private var dismiss
    var title: String
    @ViewBuilder var accessory: Accessory

    var body: some View {
        HStack(spacing: 12) {
            Text(title).font(.title3).fontWeight(.semibold)
            Spacer()
            accessory
            Spacer()
            Button { dismiss() } label: {
                Image(systemName: "xmark.circle.fill").font(.title2).symbolRenderingMode(.hierarchical)
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .keyboardShortcut(.cancelAction)
            .help("Close (Esc)")
            .accessibilityLabel("Close")
        }
    }
}
