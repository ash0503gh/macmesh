import Foundation
import Observation

/// Where the Python engine lives: inside the app (Resources/engine), a source checkout for
/// development (MACMESH_ENGINE_PYTHON + MACMESH_SOURCE), or an installed `macmesh`.
struct EngineLocation {
    var executable: URL
    var prefix: [String]
    var environment: [String: String]

    static func find() -> EngineLocation? {
        let env = ProcessInfo.processInfo.environment
        // GUI apps start with a bare PATH; tools such as ffmpeg live in Homebrew.
        var base = ["PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + NSHomeDirectory() + "/.local/bin"]
        if let home = env["MACMESH_HOME"] { base["MACMESH_HOME"] = home }
        if let engine = Bundle.main.resourceURL?.appending(path: "engine"),
           FileManager.default.isExecutableFile(atPath: engine.appending(path: "python/bin/python3").path) {
            base["PYTHONPATH"] = engine.appending(path: "lib").path
            base["PYTHONNOUSERSITE"] = "1"
            return EngineLocation(executable: engine.appending(path: "python/bin/python3"), prefix: ["-m", "macmesh.cli"],
                                  environment: base)
        }
        if let python = env["MACMESH_ENGINE_PYTHON"], let source = env["MACMESH_SOURCE"] {
            base["PYTHONPATH"] = source
            return EngineLocation(executable: URL(filePath: python), prefix: ["-m", "macmesh.cli"], environment: base)
        }
        for dir in ["/opt/homebrew/bin", "/usr/local/bin", NSHomeDirectory() + "/.local/bin"] {
            let cli = URL(filePath: dir).appending(path: "macmesh")
            if FileManager.default.isExecutableFile(atPath: cli.path) {
                return EngineLocation(executable: cli, prefix: [], environment: base)
            }
        }
        return nil
    }

    func process(_ arguments: [String]) -> Process {
        let p = Process()
        p.executableURL = executable
        p.arguments = prefix + arguments
        p.environment = ProcessInfo.processInfo.environment.merging(environment) { $1 }
        return p
    }

    /// Runs a command, handing each line it prints to `line` as it comes; throws with its last error line.
    func stream(_ arguments: [String], line: @escaping @Sendable (String) -> Void) async throws {
        try await Task.detached {
            let p = process(arguments)
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err
            p.standardInput = FileHandle.nullDevice
            try p.run()
            var buffer = Data()
            while true {
                let chunk = out.fileHandleForReading.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 0x0A) {
                    line(String(decoding: buffer[..<newline], as: UTF8.self))
                    buffer.removeSubrange(...newline)
                }
            }
            p.waitUntilExit()
            guard p.terminationStatus == 0 else {
                let text = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                throw ControlClient.Failure.engine(text.split(separator: "\n").last.map(String.init)?
                    .replacingOccurrences(of: "error: ", with: "") ?? "it stopped with status \(p.terminationStatus)")
            }
        }.value
    }

    /// Runs a one-off command (init, join) and returns its output; throws with what it printed.
    func run(_ arguments: [String]) async throws -> String {
        try await Task.detached {
            let p = process(arguments)
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            p.standardInput = FileHandle.nullDevice
            try p.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            let text = String(decoding: data, as: UTF8.self)
            guard p.terminationStatus == 0 else {
                throw ControlClient.Failure.engine(text.split(separator: "\n").last.map(String.init)?
                    .replacingOccurrences(of: "error: ", with: "") ?? "it stopped with status \(p.terminationStatus)")
            }
            return text
        }.value
    }
}

/// The app's view of the engine on this Mac: starts the daemon, polls it, and runs actions.
@MainActor @Observable
final class Engine {
    enum Phase: Equatable {
        case checking, needsSetup, starting, running, missing, failed(String)
    }

    var phase: Phase = .checking
    var status: MeshStatus?
    var apps: [AppProfile] = []
    var ai: AiInfo?
    var notice: String?
    /// Views on screen that want quick updates; polled every second while any is open or a job runs.
    private var watchers: Set<String> = []
    var watching: Bool { !watchers.isEmpty }

    func watch(_ view: String, _ on: Bool) {
        if on { watchers.insert(view) } else { watchers.remove(view) }
    }
    /// Off for sample data (snapshots): nothing is polled or started.
    var live = true

    let client: ControlClient
    let location: EngineLocation?
    private var daemon: Process?
    private var poller: Task<Void, Never>?

    init(client: ControlClient = ControlClient(), location: EngineLocation? = EngineLocation.find()) {
        self.client = client
        self.location = location
    }

    var home: String { (client.socketPath as NSString).deletingLastPathComponent }

    /// Set up means in a mesh: a config and this Mac's certificate.
    var configExists: Bool {
        FileManager.default.fileExists(atPath: home + "/config.json") && FileManager.default.fileExists(atPath: home + "/tls/node.crt")
    }

    /// This Mac's own jobs that are running.
    var activeJobs: [Job] { status?.jobs.filter { $0.isActive && $0.kind != "lending" } ?? [] }
    var recentJobs: [Job] { Array((status?.jobs.filter { !$0.isActive && $0.kind != "lending" } ?? []).prefix(3)) }
    /// Work this Mac is doing for other Macs right now.
    var lendingJobs: [Job] { status?.jobs.filter { $0.kind == "lending" } ?? [] }
    var otherMacs: [Mac] { status?.macs.filter { !$0.thisMac } ?? [] }

    func launch() {
        guard live, poller == nil else { return }
        poller = Task { [weak self] in
            while let self, !Task.isCancelled {
                await self.tick()
                let busy = self.watching || !self.activeJobs.isEmpty || !self.lendingJobs.isEmpty
                try? await Task.sleep(for: .seconds(busy ? 1 : 5))
            }
        }
    }

    func tick() async {
        do {
            status = try await client.call("status")
            if phase != .running {
                phase = .running
                apps = (try? await client.call("apps")) ?? []
                ai = try? await client.call("ai")
            }
        } catch {
            if !configExists { phase = .needsSetup; return }
            guard location != nil else { phase = .missing; return }
            if daemon?.isRunning != true { startDaemon() }
        }
    }

    private func startDaemon() {
        guard let location else { return }
        phase = .starting
        let p = location.process(["serve"])
        let log = URL(filePath: (client.socketPath as NSString).deletingLastPathComponent).appending(path: "serve.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: log) {
            p.standardOutput = handle
            p.standardError = handle
        }
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in
                guard let self, proc.terminationStatus != 0, proc.terminationStatus != 15 else { return }
                self.phase = .failed("The engine stopped (status \(proc.terminationStatus)). Its log is in ~/.macmesh/serve.log.")
            }
        }
        do { try p.run(); daemon = p } catch { phase = .failed("Couldn't start the engine: \(error.localizedDescription)") }
    }

    /// On quit: let the engine hand its work back to the other Macs.
    func stop() {
        daemon?.terminate()
        daemon?.waitUntilExit()
    }

    // ---- actions --------------------------------------------------------
    func setLending(_ on: Bool) async {
        do { status = try await client.call("lend", ["on": on]) } catch { notice = error.localizedDescription }
    }

    func setOwnerLimit(_ limit: Double) async {
        do { status = try await client.call("owner", ["limit": limit]) } catch { notice = error.localizedDescription }
    }

    func setOwnerMode(_ mode: String) async {
        do { status = try await client.call("owner", ["mode": mode]) } catch { notice = error.localizedDescription }
    }

    func inspect(_ url: URL) async throws -> Inspection {
        try await client.call("inspect", ["path": url.path])
    }

    func start(_ spec: [String: Any]) async throws -> Job {
        let job: Job = try await client.call("start", spec)
        await tick()
        return job
    }

    func cancel(_ job: Job) async {
        do { let _: Job = try await client.call("cancel", ["id": job.id]); await tick() } catch { notice = error.localizedDescription }
    }

    func invite() async {
        do { let _: MeshStatus.Invite = try await client.call("invite"); await tick() } catch { notice = error.localizedDescription }
    }

    func revoke(_ mac: Mac) async {
        do {
            let _: [String: String] = try await client.call("revoke", ["fingerprint": mac.fingerprint])
            await tick()
        } catch {
            notice = error.localizedDescription
        }
    }

    // ---- AI ------------------------------------------------------------------
    func refreshApps() async {
        apps = (try? await client.call("apps")) ?? apps
    }

    func setAI(agent: String? = nil, autorun: Bool? = nil) async {
        var params: [String: Any] = [:]
        if let agent { params["agent"] = agent }
        if let autorun { params["autorun"] = autorun }
        do { ai = try await client.call("set_ai", params) } catch { notice = error.localizedDescription }
    }

    func plan(request: String, paths: [String], macs: [String]) async throws -> AiTask {
        try await client.call("plan", ["request": request, "paths": paths, "macs": macs])
    }

    // ---- planning by conversation --------------------------------------------
    func chatStart(_ message: String, paths: [String], macs: [String]) async throws -> ChatInfo {
        try await client.call("chat_start", ["message": message, "paths": paths, "macs": macs])
    }

    func chatSend(_ id: String, message: String? = nil, folder: String? = nil, macs: [String]) async throws -> ChatInfo {
        var params: [String: Any] = ["id": id, "macs": macs]
        if let message { params["message"] = message }
        if let folder { params["folder"] = folder }
        return try await client.call("chat_send", params)
    }

    func chat(_ id: String) async throws -> ChatInfo {
        try await client.call("chat", ["id": id])
    }

    /// Saved conversations, or nil if the engine couldn't be asked (keep showing what you had).
    func chats() async -> [ChatListItem]? {
        try? await client.call("chats")
    }

    func chatDelete(_ id: String) async throws {
        struct Deleted: Decodable { var deleted: String }
        let _: Deleted = try await client.call("chat_delete", ["id": id])
    }

    func chatRestore(_ id: String) async throws -> ChatInfo {
        try await client.call("chat_restore", ["id": id])
    }

    func chatRun(_ id: String, macs: [String], output: String, slots: Int?) async throws -> Job {
        var params: [String: Any] = ["id": id, "macs": macs, "output": output]
        if let slots { params["slots"] = slots }
        let job: Job = try await client.call("chat_run", params)
        await tick()
        return job
    }

    /// Polls a conversation while the AI is answering.
    func follow(chat: ChatInfo, update: @escaping (ChatInfo) -> Void) async {
        var current = chat
        update(current)
        while current.state == "thinking", !Task.isCancelled {
            try? await Task.sleep(for: .seconds(1))
            if let next = try? await self.chat(current.id) { current = next; update(current) }
        }
    }

    func teach(program: String, name: String, sample: String) async throws -> AiTask {
        try await client.call("teach", ["program": program, "name": name, "sample": sample])
    }

    func explain(_ job: Job) async throws -> AiTask {
        try await client.call("explain", ["id": job.id])
    }

    func aiTask(_ id: String) async throws -> AiTask {
        try await client.call("ai_task", ["id": id])
    }

    func runPlan(_ id: String) async throws -> Job {
        let job: Job = try await client.call("run_plan", ["id": id])
        await tick()
        return job
    }

    /// Polls an AI task until it's done or failed, reporting each change.
    func follow(_ task: AiTask, update: @escaping (AiTask) -> Void) async {
        var current = task
        update(current)
        while current.state == "working", !Task.isCancelled {
            try? await Task.sleep(for: .seconds(1))
            if let next = try? await aiTask(current.id) { current = next; update(current) }
        }
        await tick()
    }

    // ---- joining and leaving ---------------------------------------------------
    /// Meshes nearby that can take this Mac (works before this Mac is set up).
    func nearby() async -> [NearbyMesh] {
        guard let location, let text = try? await location.run(["nearby", "--json", "--seconds", "3"]),
              let line = text.split(separator: "\n").last(where: { $0.hasPrefix("[") }) else { return [] }
        return (try? JSONDecoder().decode([NearbyMesh].self, from: Data(line.utf8))) ?? []
    }

    /// Ask a mesh's first Mac to let this one in. `onNumber` gets the number to compare;
    /// returns once someone clicks Allow there (or throws).
    func requestJoin(_ mesh: NearbyMesh, name: String, onNumber: @escaping @MainActor (String) -> Void) async throws {
        guard let location else { throw ControlClient.Failure.engine("the engine is missing from this app") }
        await stopEngine()
        var args = ["join", "--request", "--json", "--force", "--host", mesh.addresses.joined(separator: ",")]
        if !name.isEmpty { args += ["--name", name] }
        try await location.stream(args) { line in
            guard let data = line.data(using: .utf8),
                  let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  event["event"] as? String == "number", let number = event["number"] as? String else { return }
            Task { @MainActor in onNumber(number) }
        }
        phase = .starting
        await tick()
    }

    /// Leave the mesh: this Mac forgets its certificate and token, and setup starts again.
    func leave() async throws {
        guard let location else { throw ControlClient.Failure.engine("the engine is missing from this app") }
        await stopEngine()
        _ = try await location.run(["leave"])
        status = nil
        phase = .needsSetup
    }

    func approve(_ request: MeshStatus.JoinRequest, allow: Bool) async {
        do { let _: MeshStatus.Invite = try await client.call("approve", ["id": request.id, "allow": allow]); await tick() } catch {
            notice = error.localizedDescription
        }
    }

    /// The owner's answer to another Mac asking this one to install a program.
    func decideInstall(_ request: MeshStatus.InstallRequest, allow: Bool) async {
        do {
            struct Answer: Decodable { var program: String }
            let _: Answer = try await client.call("install", ["program": request.program, "allow": allow])
            await tick()
        } catch {
            notice = error.localizedDescription
        }
    }

    /// Requests that need the owner (a Mac asking to join, or to install something), so the
    /// window can come forward when a new one arrives.
    var waitingOnOwner: Set<String> {
        Set((status?.invite.requests ?? []).map { "join:\($0.id)" }
            + (status?.installs ?? []).filter { $0.state == "asking" }.map { "install:\($0.program)" })
    }

    /// Lets the firewall pass the engine, through macOS's own administrator prompt.
    func allowThroughFirewall() async {
        guard let program = status?.firewall?.program else { return }
        let tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        let quoted = "'" + program.replacingOccurrences(of: "'", with: "'\\''") + "'"
        let script = "do shell script \"\(tool) --add \(quoted); \(tool) --unblockapp \(quoted)\" with administrator privileges"
        var error: NSDictionary?
        NSAppleScript(source: script)?.executeAndReturnError(&error)
        if let error, (error[NSAppleScript.errorNumber] as? Int) != -128 {
            notice = "Couldn't change the firewall: \(error[NSAppleScript.errorMessage] ?? "unknown error")"
        }
        await tick()
    }

    private func stopEngine() async {
        let _: [String: Bool]? = try? await client.call("stop")
        daemon?.terminate()
        daemon = nil
        try? await Task.sleep(for: .milliseconds(500))
    }

    /// First Mac: start a new mesh. Then the daemon can run.
    func createMesh(name: String, force: Bool = false) async throws {
        guard let location else { throw ControlClient.Failure.engine("the engine is missing from this app") }
        await stopEngine()
        var args = ["init"]
        if force { args.append("--force") }
        if !name.isEmpty { args += ["--name", name] }
        _ = try await location.run(args)
        phase = .starting
        await tick()
    }

    /// Any other Mac: join with the code shown by `invite` on the first one.
    func join(code: String, host: String, name: String, force: Bool = false) async throws {
        guard let location else { throw ControlClient.Failure.engine("the engine is missing from this app") }
        await stopEngine()
        var args = ["join", code.trimmingCharacters(in: .whitespaces)]
        if force { args.append("--force") }
        if !host.isEmpty { args += ["--host", host] }
        if !name.isEmpty { args += ["--name", name] }
        _ = try await location.run(args)
        phase = .starting
        await tick()
    }
}
