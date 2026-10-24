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
    var notice: String?
    /// Polled quickly while someone is looking or a job runs.
    var watching = false
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

    var configExists: Bool {
        FileManager.default.fileExists(atPath: (client.socketPath as NSString).deletingLastPathComponent + "/config.json")
    }

    var activeJobs: [Job] { status?.jobs.filter(\.isActive) ?? [] }
    var recentJobs: [Job] { Array((status?.jobs.filter { !$0.isActive } ?? []).prefix(3)) }
    var otherMacs: [Mac] { status?.macs.filter { !$0.thisMac } ?? [] }

    func launch() {
        guard live, poller == nil else { return }
        poller = Task { [weak self] in
            while let self, !Task.isCancelled {
                await self.tick()
                let busy = self.watching || !self.activeJobs.isEmpty
                try? await Task.sleep(for: .seconds(busy ? 1 : 5))
            }
        }
    }

    private func tick() async {
        do {
            status = try await client.call("status")
            if phase != .running {
                phase = .running
                apps = (try? await client.call("apps")) ?? []
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
        do { let _: [String: String] = try await client.call("revoke", ["fingerprint": mac.fingerprint]) } catch {
            notice = error.localizedDescription
        }
    }

    /// First Mac: start a new mesh. Then the daemon can run.
    func createMesh(name: String) async throws {
        guard let location else { throw ControlClient.Failure.engine("the engine is missing from this app") }
        var args = ["init"]
        if !name.isEmpty { args += ["--name", name] }
        _ = try await location.run(args)
        phase = .starting
        await tick()
    }

    /// Any other Mac: join with the code shown by `invite` on the first one.
    func join(code: String, host: String, name: String) async throws {
        guard let location else { throw ControlClient.Failure.engine("the engine is missing from this app") }
        var args = ["join", code.trimmingCharacters(in: .whitespaces)]
        if !host.isEmpty { args += ["--host", host] }
        if !name.isEmpty { args += ["--name", name] }
        _ = try await location.run(args)
        phase = .starting
        await tick()
    }
}
