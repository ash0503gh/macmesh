import SwiftUI

/// First run: this Mac starts a mesh, or joins one with the code another Mac shows.
struct OnboardingView: View {
    @Environment(Engine.self) private var engine

    enum Choice { case start, join }
    @State var choice: Choice?
    @State private var name = Host.current().localizedName ?? ""
    @State private var code = ""
    @State private var host = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Share processing power between your Macs").font(.title2).fontWeight(.semibold)
                Text("Heavy renders and exports are split across the Macs on your network, and joined back losslessly. Everything between Macs is encrypted.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 12) {
                card(.start, "Start a mesh", "This is the first Mac. It will invite the others.", "plus.circle")
                card(.join, "Join a mesh", "Another Mac already runs macmesh and shows a code.", "link.circle")
            }
            if let choice {
                Form {
                    TextField("This Mac's name", text: $name)
                    if choice == .join {
                        TextField("Code", text: $code, prompt: Text("7K2Q-M9XD-4TRA"))
                            .font(.system(.body, design: .monospaced))
                        TextField("Address (only if it can't be found)", text: $host, prompt: Text("192.168.1.20"))
                    }
                }
                .formStyle(.grouped)
                .scrollDisabled(true)
                .fixedSize(horizontal: false, vertical: true)
            }
            if let error {
                Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange).font(.callout)
            }
            HStack {
                Spacer()
                Button(busy ? "Working…" : choice == .join ? "Join" : "Start") { Task { await go() } }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(choice == nil || busy || (choice == .join && code.count < 12))
            }
        }
        .padding(24)
        .frame(width: 520)
    }

    private func card(_ value: Choice, _ title: String, _ detail: String, _ symbol: String) -> some View {
        Button { choice = value; error = nil } label: {
            VStack(alignment: .leading, spacing: 6) {
                Image(systemName: symbol).font(.title2).foregroundStyle(choice == value ? Color.accentColor : .secondary)
                Text(title).fontWeight(.medium)
                Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, minHeight: 104, alignment: .topLeading)
            .padding(14)
            .background(RoundedRectangle(cornerRadius: 12).strokeBorder(choice == value ? Color.accentColor : Color.secondary.opacity(0.3),
                                                                        lineWidth: choice == value ? 2 : 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func go() async {
        busy = true
        defer { busy = false }
        do {
            if choice == .join { try await engine.join(code: code, host: host, name: name) }
            else { try await engine.createMesh(name: name) }
        } catch {
            self.error = error.localizedDescription
        }
    }
}
