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
    var invite: Invite
    var macs: [Mac]
    var jobs: [Job]

    struct Invite: Decodable, Equatable {
        var code: String?
        var state: String?
        var expires: Double?
        var joined: String?
        var error: String?
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

    var isActive: Bool { ["starting", "running", "merging"].contains(state) }
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
        case "running": return job.eta.map { "\(duration($0)) left" } ?? "Starting chunks…"
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
