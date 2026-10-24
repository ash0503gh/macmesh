import SwiftUI

/// First run: join a mesh found nearby with one click, or start a new one.
struct OnboardingView: View {
    @Environment(Engine.self) private var engine

    @State var nearby: [NearbyMesh] = []
    @State var searching = true
    @State private var name = Host.current().localizedName ?? ""
    @State var joining: NearbyMesh?
    @State var number: String?
    @State private var busy = false
    @State private var error: String?
    @State private var useCode = false
    @State private var code = ""
    @State private var host = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Share processing power between your Macs").font(.title2).fontWeight(.semibold)
                Text("Join the Macs already using macmesh on this network, or start a mesh on this Mac.")
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let joining { waiting(for: joining) } else { choices }
            if let error {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange).font(.callout).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(28)
        .frame(width: 540)
        .task { await search() }
    }

    // ---- what to do --------------------------------------------------------------
    @ViewBuilder
    private var choices: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                SectionLabel("Meshes nearby")
                if searching { ProgressView().controlSize(.small) }
            }
            if nearby.isEmpty {
                Text(searching ? "Looking on this network…" : "None found yet. On your first Mac, open macmesh: it shows up here.")
                    .font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 54, alignment: .leading)
            }
            ForEach(nearby) { mesh in
                HStack(spacing: 12) {
                    Image(systemName: "laptopcomputer.and.desktopcomputer").font(.title3).foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(mesh.name).fontWeight(.medium)
                        Text("Ask it to let this Mac in").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Join") { Task { await join(mesh) } }
                        .buttonStyle(.borderedProminent)
                        .disabled(busy)
                }
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.04)))
            }
        }
        LabeledContent("This Mac's name") {
            TextField("Name", text: $name).textFieldStyle(.roundedBorder).frame(width: 240)
        }
        Divider()
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text("First Mac?").fontWeight(.medium)
                Text("Start a mesh here. Other Macs then join it from their own macmesh.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            Button(busy && joining == nil && !useCode ? "Starting…" : "Start a mesh") { Task { await start() } }
                .disabled(busy)
        }
        DisclosureGroup("Use a code instead", isExpanded: $useCode) {
            HStack {
                TextField("Code from `macmesh invite`", text: $code).font(.system(.body, design: .monospaced))
                TextField("Address (if it isn't found)", text: $host).frame(width: 170)
                Button("Join") { Task { await joinWithCode() } }.disabled(busy || code.count < 12)
            }
            .textFieldStyle(.roundedBorder)
            .padding(.top, 6)
        }
        .font(.callout)
    }

    private func waiting(for mesh: NearbyMesh) -> some View {
        VStack(spacing: 14) {
            if let number {
                Text("On \(mesh.name), click Allow if it shows this number:")
                    .foregroundStyle(.secondary).multilineTextAlignment(.center)
                Text(number).font(.system(size: 44, weight: .semibold, design: .rounded)).monospacedDigit()
                    .accessibilityLabel("Number \(number)")
                HStack(spacing: 8) { ProgressView().controlSize(.small); Text("Waiting for \(mesh.name)…").foregroundStyle(.secondary) }
            } else {
                HStack(spacing: 8) { ProgressView().controlSize(.small); Text("Asking \(mesh.name)…") }
            }
            Text("If the numbers don't match, don't allow it: something else is answering on this network.")
                .font(.caption).foregroundStyle(.tertiary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 18)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.primary.opacity(0.035)))
    }

    // ---- actions -------------------------------------------------------------------
    private func search() async {
        guard engine.live else { searching = false; return }
        while engine.phase == .needsSetup || engine.phase == .checking, !Task.isCancelled {
            if joining == nil {
                searching = true
                nearby = await engine.nearby()
                searching = false
            }
            try? await Task.sleep(for: .seconds(4))
        }
    }

    private func join(_ mesh: NearbyMesh) async {
        error = nil
        busy = true
        joining = mesh
        number = nil
        defer { busy = false; joining = nil }
        do {
            try await engine.requestJoin(mesh, name: name) { number = $0 }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func start() async {
        error = nil
        busy = true
        defer { busy = false }
        do { try await engine.createMesh(name: name, force: true) } catch { self.error = error.localizedDescription }
    }

    private func joinWithCode() async {
        error = nil
        busy = true
        defer { busy = false }
        do { try await engine.join(code: code, host: host, name: name, force: true) } catch { self.error = error.localizedDescription }
    }
}

/// On the Mac that started the mesh: a Mac asking to join, with the number to compare.
struct JoinRequestBanner: View {
    @Environment(Engine.self) private var engine
    var request: MeshStatus.JoinRequest
    var compact = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Image(systemName: "person.badge.plus").foregroundStyle(Color.accentColor)
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(request.name) wants to join").fontWeight(.medium)
                    Text("Allow only if it shows \(request.number).").font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                if !compact {
                    Text(request.number).font(.system(.title2, design: .rounded)).fontWeight(.semibold).monospacedDigit()
                }
            }
            HStack {
                if compact {
                    Text(request.number).font(.system(.title3, design: .rounded)).fontWeight(.semibold).monospacedDigit()
                }
                Spacer()
                Button("Don't allow") { Task { await engine.approve(request, allow: false) } }
                Button("Allow") { Task { await engine.approve(request, allow: true) } }.buttonStyle(.borderedProminent)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.accentColor.opacity(0.1)))
    }
}

/// When the macOS firewall stops other Macs from reaching this one.
struct FirewallBanner: View {
    @Environment(Engine.self) private var engine
    var firewall: MeshStatus.Firewall

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Image(systemName: "flame").foregroundStyle(.orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text("The macOS firewall is blocking macmesh").fontWeight(.medium)
                    Text(firewall.blockAll
                         ? "It blocks all incoming connections, so your other Macs can't reach this one. Turn off \u{201C}Block all incoming connections\u{201D} in Firewall options."
                         : "Your other Macs can't reach this one until macmesh's engine may accept connections.")
                        .font(.callout).foregroundStyle(.secondary).lineLimit(3)
                }
            }
            HStack {
                Spacer()
                if firewall.blockAll {
                    Button("Open Firewall Settings") {
                        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Network-Settings.extension?Firewall")!)
                    }
                } else {
                    Button("Allow…") { Task { await engine.allowThroughFirewall() } }
                        .help("macOS asks for an administrator's password")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.orange.opacity(0.1)))
    }
}
