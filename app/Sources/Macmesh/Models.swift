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
    var tls: Bool
    var holdsCa: Bool
    var fingerprint: String
    var outputDir: String
    var mesh: String?
    var firewall: Firewall?
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

    struct JobMac: Decodable, Equatable {
        var nodeId: String?
        var name: String
        var done: Int
        var running: Int
    }

    /// One chunk of the job: its state and the Mac that did it (or is doing it).
    struct Chunk: Decodable, Equatable {
        var state: String
        var mac: String
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
