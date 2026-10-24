import SwiftUI
import UniformTypeIdentifiers

/// The dashboard's New job sheet: drop a project or video, see what macmesh will do, pick Macs, start.
struct DropSheetView: View {
    @Environment(Engine.self) private var engine
    @Environment(\.dismiss) private var dismiss

    var initialURL: URL?
    @State var file: Inspection?
    @State private var inspecting = false
    @State private var error: String?
    @State private var targeted = false
    @State private var allFrames = true
    @State private var startFrame = 0
    @State private var endFrame = 0
    @State private var values: [String: String] = [:]
    @State private var preset = "hevc"
    @State private var turnedOff: Set<String> = []  // Macs left out; new ones join on their own
    @State private var starting = false

    static let presets: [(String, String)] = [("hevc", "HEVC (smaller files)"), ("h264", "H.264 (plays anywhere)"),
                                              ("prores", "ProRes (for editing)")]

    var macs: [Mac] { engine.status?.macs ?? [] }
    var usable: [Mac] { macs.filter { $0.available && !turnedOff.contains($0.nodeId) } }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            dropZone
            if let file {
                settings(for: file)
                macPicker
                summary(for: file)
            }
            if let error {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if file == nil {
                HStack { Spacer(); Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction) }
            }
        }
        .padding(22)
        .frame(width: 500)
        .task { if let initialURL, file == nil { await open(initialURL) } }
    }

    // ---- the file --------------------------------------------------------
    private var dropZone: some View {
        HStack(spacing: 14) {
            Image(systemName: file == nil ? "square.and.arrow.down.on.square" : icon(for: file!))
                .font(.system(size: 26))
                .foregroundStyle(file == nil ? .secondary : .primary)
                .frame(width: 34)
            VStack(alignment: .leading, spacing: 2) {
                if let file {
                    Text(file.name).font(.headline).lineLimit(1)
                    Text(describe(file)).font(.callout).foregroundStyle(.secondary).lineLimit(2)
                } else if inspecting {
                    Text("Looking at the file…").foregroundStyle(.secondary)
                } else {
                    Text("Drop a project or video").font(.headline)
                    Text("FilmCraft, EffectCraft, Grade, or any app with a profile").font(.callout).foregroundStyle(.secondary)
                }
            }
            Spacer()
            Button(file == nil ? "Choose…" : "Change…") { choose() }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .strokeBorder(style: StrokeStyle(lineWidth: targeted ? 2 : 1, dash: file == nil ? [6, 4] : []))
                .foregroundStyle(targeted ? Color.accentColor : Color.secondary.opacity(0.4))
        )
        .contentShape(Rectangle())
        .onDrop(of: [.fileURL], isTargeted: $targeted) { providers in
            guard let provider = providers.first else { return false }
            _ = provider.loadObject(ofClass: URL.self) { url, _ in
                if let url { Task { @MainActor in await open(url) } }
            }
            return true
        }
    }

    private func icon(for file: Inspection) -> String {
        file.kind == "video" ? "film" : file.app == "effectcraft" ? "sparkles.rectangle.stack" : "square.stack.3d.up"
    }

    private func describe(_ file: Inspection) -> String {
        if file.kind == "video" {
            let size = file.video.flatMap { v in v.width.map { "\($0)×\(v.height ?? 0)" } } ?? ""
            return ["Video", size, file.duration.map { Format.duration($0) } ?? ""].filter { !$0.isEmpty }.joined(separator: " · ")
        }
        let app = engine.apps.first { $0.name == file.app }?.description.components(separatedBy: ":").first ?? file.app ?? ""
        var parts = [app]
        if let frames = file.frames { parts.append("\(frames.formatted()) frames") }
        if let fps = file.fps { parts.append("\(fps.formatted(.number.precision(.fractionLength(0 ... 3)))) fps") }
        return parts.joined(separator: " · ")
    }

    func choose() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true  // some projects are bundles
        panel.allowsMultipleSelection = false
        panel.message = "Choose a project or video to split across your Macs"
        if panel.runModal() == .OK, let url = panel.url { Task { await open(url) } }
    }

    func open(_ url: URL) async {
        error = nil
        inspecting = true
        defer { inspecting = false }
        do {
            let found = try await engine.inspect(url)
            file = found
            startFrame = 0
            endFrame = max((found.frames ?? 1) - 1, 0)
            allFrames = found.frames != nil
            values = found.vars ?? [:]
            if found.installed == false { error = "\(found.app ?? "This app") isn't installed on this Mac, so macmesh can't read the project." }
            else if let note = found.note { error = note }
        } catch {
            file = nil
            self.error = error.localizedDescription
        }
    }

    // ---- what to do -------------------------------------------------------
    @ViewBuilder
    private func settings(for file: Inspection) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if file.kind == "video" {
                Picker("Encode as", selection: $preset) {
                    ForEach(Self.presets, id: \.0) { Text($0.1).tag($0.0) }
                }
                .pickerStyle(.menu)
                .frame(maxWidth: 320)
            } else {
                ForEach(file.needsVars ?? [], id: \.self) { name in
                    LabeledContent(name == "comp" ? "Composition" : name.capitalized) {
                        TextField(name == "comp" ? "Main" : name, text: Binding(get: { values[name] ?? "" },
                                                                             set: { values[name] = $0 }))
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 200)
                    }
                }
                if let frames = file.frames {
                    Picker("Frames", selection: $allFrames) {
                        Text("All \(frames.formatted())").tag(true)
                        Text("A range").tag(false)
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 260)
                    if !allFrames {
                        HStack(spacing: 8) {
                            TextField("From", value: $startFrame, format: .number).frame(width: 80)
                            Text("to").foregroundStyle(.secondary)
                            TextField("To", value: $endFrame, format: .number).frame(width: 80)
                        }
                        .textFieldStyle(.roundedBorder)
                    }
                } else {
                    HStack(spacing: 8) {
                        Text("Frames")
                        TextField("From", value: $startFrame, format: .number).frame(width: 80)
                        Text("to").foregroundStyle(.secondary)
                        TextField("To", value: $endFrame, format: .number).frame(width: 80)
                    }
                    .textFieldStyle(.roundedBorder)
                }
            }
        }
    }

    private var macPicker: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel("Use these Macs")
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 88), spacing: 8)], alignment: .leading, spacing: 12) {
                    ForEach(macs) { mac in
                        Button {
                            guard mac.available else { return }
                            if turnedOff.contains(mac.nodeId) { turnedOff.remove(mac.nodeId) } else { turnedOff.insert(mac.nodeId) }
                        } label: {
                            MacAvatar(mac: mac, selected: mac.available && !turnedOff.contains(mac.nodeId))
                        }
                        .buttonStyle(.plain)
                        .help(mac.available ? "\(mac.chip ?? "") · \(Format.macState(mac))" : Format.macState(mac))
                    }
            }
        }
    }

    @ViewBuilder
    private func summary(for file: Inspection) -> some View {
        let problem = startProblem(file)
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 2) {
                Text(problem ?? plan(file)).font(.callout).foregroundStyle(problem == nil ? AnyShapeStyle(.secondary) : AnyShapeStyle(.orange))
                Text("Saved to your macmesh folder in Movies").font(.caption).foregroundStyle(.tertiary)
            }
            Spacer()
            Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
            Button(starting ? "Starting…" : "Start on \(usable.count) \(usable.count == 1 ? "Mac" : "Macs")") {
                Task { await start(file) }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .keyboardShortcut(.defaultAction)
            .disabled(problem != nil || starting)
        }
    }

    private func plan(_ file: Inspection) -> String {
        let pieces = file.kind == "video" ? "Split into short pieces" : "Split into chunks"
        return "\(pieces), joined losslessly"
    }

    private func startProblem(_ file: Inspection) -> String? {
        if usable.isEmpty { return "Pick at least one Mac" }
        if file.kind == "app" {
            if let missing = (file.needsVars ?? []).first(where: { (values[$0] ?? "").isEmpty }) {
                return "Enter the \(missing == "comp" ? "composition's name" : missing)"
            }
            if (!allFrames || file.frames == nil), endFrame < startFrame { return "The last frame comes before the first" }
            if file.installed == false { return "\(file.app ?? "The app") isn't on this Mac" }
        }
        return nil
    }

    private func start(_ file: Inspection) async {
        starting = true
        defer { starting = false }
        var spec: [String: Any] = ["kind": file.kind, "path": file.path, "macs": usable.map(\.nodeId)]
        if file.kind == "video" {
            spec["preset"] = preset
        } else {
            if !values.isEmpty { spec["vars"] = values }
            if !allFrames || file.frames == nil { spec["start"] = startFrame; spec["end"] = endFrame }
        }
        do {
            _ = try await engine.start(spec)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
