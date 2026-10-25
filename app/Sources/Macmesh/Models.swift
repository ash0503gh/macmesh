import Foundation

/// What the engine's control socket returns (macmesh/control.py), decoded with snake_case keys.

struct MeshStatus: Decodable, Equatable {
    var nodeId: String
    var name: String
    var lend: Bool
    var accepting: Bool?
    var reason: String
    var running: Int
    var paused: Int
    var ownerMode: String?
    var slots: Int?      // how many tasks this Mac runs at once
    var limit: Double?   // the most of this Mac (CPU and GPU, owner + macmesh) to use, 0.5-0.95
    var duty: Double?    // how much of the time macmesh's tasks run right now, to stay under it
    var tls: Bool
    var holdsCa: Bool
    var fingerprint: String
    var outputDir: String
    var mesh: String?
    var firewall: Firewall?
    var version: String?
    var installs: [InstallRequest]?

    /// Another Mac's job needs a program this Mac lacks; its owner decides whether to set it up.
    struct InstallRequest: Decodable, Equatable, Identifiable {
        var program: String
        var summary: String
        var description: String?
        var by: String
        var state: String   // asking, installing, done, failed, declined
        var detail: String?
        var error: String?
        var id: String { program }
    }
    var invite: Invite
    var macs: [Mac]
    var jobs: [Job]

    struct Invite: Decodable, Equatable {
        var code: String?
        var state: String?
        var expires: Double?
        var joined: String?
        var error: String?
        var requests: [JoinRequest]?
        var full: Bool?
        var maxMacs: Int?
    }

    /// A Mac asking to join (on the Mac that started the mesh): allow it if the numbers match.
    struct JoinRequest: Decodable, Equatable, Identifiable {
        var id: String
        var name: String
        var number: String
        var age: Int?
    }

    struct Firewall: Decodable, Equatable {
        var on: Bool
        var blockAll: Bool
        var blocked: Bool
        var program: String
    }
}

struct Mac: Decodable, Equatable, Identifiable {
    var nodeId: String
    var name: String
    var address: String
    var thisMac: Bool
    var model: String?
    var chip: String?
    var fanless: Bool
    var score: Double?
    var cores: Int?
    var memoryGb: Int?
    var slots: Int?
    var state: String          // ready | working | paused | unavailable
    var reason: String
    var onBattery: Bool?
    var batteryPct: Int?
    var programs: [String]
    var sandboxed: Bool
    var fingerprint: String

    var id: String { nodeId }
    var isLaptop: Bool { (model ?? "").localizedCaseInsensitiveContains("book") }
    var available: Bool { state != "unavailable" }
    var symbol: String { isLaptop ? "laptopcomputer" : "desktopcomputer" }
}

struct Job: Decodable, Equatable, Identifiable {
    var id: String
    var name: String
    var state: String          // starting | running | merging | done | failed | cancelled
    var error: String
    var output: String
    var tasks: Int
    var done: Int
    var progress: Double
    var elapsed: Double
    var eta: Double?
    var macs: [JobMac]
    var chunks: [Chunk]
    var speedup: Double?
    var created: Double
    var kind: String?
    var events: [Event]?
    var chat: String?

    struct JobMac: Decodable, Equatable {
        var nodeId: String?
        var name: String
        var done: Int
        var running: Int
        var doing: [Activity]?
        var sent: Int?
        var received: Int?
        var upRate: Double?
        var downRate: Double?
        var load: [LoadPoint]?
        var installing: [String]?
    }

    /// What one copy of a chunk is doing on a Mac: uploading, queued, running, paused or downloading.
    struct Activity: Decodable, Equatable {
        var label: String
        var phase: String
        var since: Double   // seconds in this phase
        var bytes: Int
        var total: Int
    }

    /// One sample of a Mac's load: CPU used by this job (100 = every core) and the whole GPU.
    struct LoadPoint: Decodable, Equatable {
        var at: Double
        var cpu: Double
        var gpu: Double?
        var cores: Int?
        var duty: Double?
        var limit: Double?
    }

    struct Event: Decodable, Equatable {
        var at: Double
        var text: String
    }

    /// One chunk of the job: its state and the Mac that did it (or is doing it).
    struct Chunk: Decodable, Equatable {
        var state: String
        var mac: String
        var label: String?
        var phase: String?
        var seconds: Double?
        var attempts: Int?
    }

    var isActive: Bool { ["starting", "running", "merging", "paused"].contains(state) }
}

struct AppProfile: Decodable, Equatable, Identifiable {
    var name: String
    var description: String
    var installed: Bool
    var build: String
    var extensions: [String]
    var vars: [String: String]
    var id: String { name }
}

struct Inspection: Decodable, Equatable {
    var kind: String           // app | video
    var path: String
    var name: String
    var app: String?
    var description: String?
    var vars: [String: String]?
    var needsVars: [String]?
    var installed: Bool?
    var frames: Int?
    var fps: Double?
    var note: String?
    var duration: Double?
    var video: VideoInfo?

    struct VideoInfo: Decodable, Equatable {
        var codec: String?
        var width: Int?
        var height: Int?
        var fps: String?
    }
}

enum Format {
    /// "12 MB", "1.4 GB".
    static func bytes(_ n: Int) -> String {
        n == 0 ? "\u{2013}" : ByteCountFormatter.string(fromByteCount: Int64(n), countStyle: .file)
    }

    /// "0:52", "12:05", "1:02:03".
    static func clock(_ seconds: Double) -> String {
        let s = max(0, Int(seconds.rounded()))
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
                         : String(format: "%d:%02d", s / 60, s % 60)
    }

    /// The scheduler's log line in plain words: "→ x on Air" becomes "Started x on Air".
    static func event(_ line: String) -> String {
        var t = line.trimmingCharacters(in: .whitespaces)
        let rules: [(String, String)] = [("→ ", "Started "), ("✓ ", "Finished "), ("↻ ", "Copied "), ("✗ ", ""),
                                         ("! ", ""), ("? ", "")]
        for (prefix, word) in rules where t.hasPrefix(prefix) {
            t = word + t.dropFirst(prefix.count)
            break
        }
        t = t.replacingOccurrences(of: #"\s+\[\d+/\d+\]$"#, with: "", options: .regularExpression)
        t = t.replacingOccurrences(of: ": duplicating on ", with: " to ")
        t = t.replacingOccurrences(of: " (straggler)", with: " to finish sooner")
        return t
    }

    /// This app's version, as "v0.3.4".
    static var appVersion: String {
        "v" + ((Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "dev")
    }

    /// "2 min", "45 s", "1 h 5 min".
    static func duration(_ seconds: Double) -> String {
        let s = Int(seconds.rounded())
        if s < 60 { return "\(s) s" }
        if s < 3600 { return "\(Int((Double(s) / 60).rounded())) min" }
        return "\(s / 3600) h \((s % 3600) / 60) min"
    }

    /// The job's state as a person would say it.
    static func jobState(_ job: Job) -> String {
        switch job.state {
        case "starting": return "Finding Macs…"
        case "running" where job.kind == "lending": return "Working · \(job.tasks) \(job.tasks == 1 ? "chunk" : "chunks")"
        case "paused": return "Paused while you use this Mac"
        case "running": return job.eta.map { "About \(duration($0)) left" } ?? (job.done > 0 ? "Working…" : "Measuring speed…")
        case "merging": return "Joining the pieces…"
        case "done": return "Done in \(duration(job.elapsed))"
        case "cancelled": return "Cancelled"
        default: return job.error.isEmpty ? "Failed" : job.error
        }
    }

    /// A Mac's state, short enough for a list row.
    static func macState(_ mac: Mac) -> String {
        switch mac.state {
        case "working": return "Working"
        case "paused": return "Paused while its owner works"
        case "ready": return "Ready"
        default:
            if mac.reason == "not lending" { return "Not lending" }
            if mac.reason.hasPrefix("owner") { return "Its owner is busy" }
            if mac.reason.hasPrefix("battery") || mac.reason == "on battery" { return "On low battery" }
            return mac.reason.isEmpty ? "Unavailable" : mac.reason.prefix(1).uppercased() + mac.reason.dropFirst()
        }
    }
}

/// Which AI agent the engine uses for planning, teaching and explaining.
struct AiInfo: Decodable, Equatable {
    var choice: String          // auto | claude | antigravity
    var using: String?
    var problem: String
    var available: [String]
    var autorun: Bool

    static func label(_ agent: String) -> String {
        switch agent {
        case "claude": "Claude Code"
        case "antigravity": "Antigravity"
        default: "Whichever is installed"
        }
    }
}

/// Something the AI is working on: a plan, a new app's profile, or an explanation.
/// A job being planned by talking it through with the AI (control methods chat_*).
struct ChatInfo: Decodable, Equatable, Identifiable {
    var id: String
    var title: String
    var created: Double
    var messages: [Message]
    var folders: [String]
    var state: String          // thinking | waiting | failed
    var error: String
    var askFolder: String?
    var card: PlanCard?
    var suggest: Suggest?
    var jobIds: [String]

    struct Message: Decodable, Equatable {
        var role: String       // user | ai | note
        var text: String
        var at: Double
    }

    /// What the agreed plan would do.
    struct PlanCard: Decodable, Equatable {
        var explanation: String
        var planner: String
        var tasks: Int
        var programs: [String]
        var example: [String]
        var name: String
        var reasons: [String]
        var needsInstall: [String: [String]]
    }

    /// The AI's optional suggestions; the plan card starts from them.
    struct Suggest: Decodable, Equatable {
        var slots: [String: Int]?
        var macs: [String]?
        var outputFolder: String?
    }
}

struct ChatListItem: Decodable, Equatable, Identifiable {
    var id: String
    var title: String
    var created: Double?
    var jobIds: [String]
    var hasPlan: Bool
}

struct AiTask: Decodable, Equatable, Identifiable {
    var id: String
    var kind: String            // plan | teach | explain
    var state: String           // working | done | failed
    var steps: [String]
    var result: AiResult?
    var error: String
    var awaitingOk: Bool
}

/// The fields of every kind of AI result; each kind fills its own.
struct AiResult: Decodable, Equatable {
    // plan
    var explanation: String?
    var planner: String?
    var tasks: Int?
    var programs: [String]?
    var example: [String]?
    var merge: [String]?
    var safe: Bool?
    var reasons: [String]?
    var jobId: String?
    // teach
    var verified: String?
    var attempts: Int?
    var path: String?
    // explain
    var summary: String?
    var fix: String?
    var `where`: String?
}

/// A mesh found on the network that can take this Mac (its first Mac is running macmesh).
struct NearbyMesh: Decodable, Equatable, Identifiable {
    var name: String
    var addresses: [String]
    var mesh: String
    var id: String { mesh.isEmpty ? name : mesh }
}
