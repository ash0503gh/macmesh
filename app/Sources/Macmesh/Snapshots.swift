import AppKit
import SwiftUI

/// Sample data and offscreen renders of each screen (`Macmesh --snapshot DIR`).
@MainActor
enum Snapshots {
    static func mac(_ name: String, _ model: String, _ chip: String, state: String = "ready", reason: String = "",
                    this: Bool = false, fanless: Bool = false) -> Mac {
        Mac(nodeId: name.lowercased().replacingOccurrences(of: " ", with: ""), name: name, address: "10.0.0.2:7477",
            thisMac: this, model: model, chip: chip, fanless: fanless, score: 7, cores: 10, memoryGb: 16, slots: 2, state: state, reason: reason,
            onBattery: false, batteryPct: nil, programs: ["ffmpeg", "filmcraft"], sandboxed: true,
            fingerprint: String(repeating: "a1b2c3d4", count: 8))
    }

    static let macs = [
        mac("Ashwin's MacBook Pro", "MacBook Pro", "Apple M5", state: "working", this: true),
        mac("MacBook Air", "MacBook Air", "Apple M3", state: "working", fanless: true),
        mac("Mac Studio", "Mac Studio", "Apple M4 Max", state: "working"),
        mac("Riya's MacBook", "MacBook Pro", "Apple M2 Pro", state: "paused", reason: "owner using 72% CPU"),
        mac("Old iMac", "iMac", "Apple M1", state: "unavailable", reason: "not lending"),
    ]

    static let songs = ["It's My Birthday.mp3", "Closer (Remix).mp3", "We Found Love.mp3", "Talk Dirty.mp3",
                        "Love You No More.mp3", "Get Free.mp3", "We Own It.mp3", "That Bass.mp3", "Firestone.mp3", "Wiggle.mp3"]
    static func load(_ base: Double, _ gpu: Double, duty: Double? = nil) -> [Job.LoadPoint] {
        (0 ..< 75).map { i in
            let t = Double(i)
            return Job.LoadPoint(at: 1000 + t * 2, cpu: max(0, base + 8 * sin(t / 4) + (i < 6 ? -base + t * base / 6 : 0)),
                                 gpu: max(0, gpu + 12 * sin(t / 3 + 1)), cores: 10, duty: duty, limit: 0.9)
        }
    }
    static let running = Job(id: "a1", name: "Hip hop stems", state: "running", error: "", output: "", tasks: 10, done: 6,
                             progress: 0.68, elapsed: 254, eta: 107,
                             macs: [.init(nodeId: "a", name: "This Mac", done: 2, running: 2,
                                          doing: [.init(label: "Firestone.mp3", phase: "running", since: 52, bytes: 0, total: 0),
                                                  .init(label: "Wiggle.mp3", phase: "queued", since: 40, bytes: 0, total: 0)],
                                          sent: 0, received: 61_000_000, upRate: 0, downRate: 0, load: load(31, 64)),
                                    .init(nodeId: "b", name: "Sonakshi\u{2019}s MacBook Air", done: 4, running: 2,
                                          doing: [.init(label: "That Bass.mp3", phase: "running", since: 21, bytes: 0, total: 0),
                                                  .init(label: "We Own It.mp3", phase: "uploading", since: 2, bytes: 5_400_000, total: 9_100_000)],
                                          sent: 46_200_000, received: 132_000_000, upRate: 2_700_000, downRate: 0, load: load(24, 78, duty: 0.72))],
                             chunks: songs.enumerated().map { i, song in
                                 let mac = [1, 0, 1, 1, 0, 1, 1, 1, 0, 0][i] == 0 ? "This Mac" : "Sonakshi\u{2019}s MacBook Air"
                                 return i < 6 ? Job.Chunk(state: "done", mac: mac, label: song, phase: "done", seconds: [79, 109, 72, 53, 105, 64][i], attempts: 1)
                                     : i == 6 ? Job.Chunk(state: "running", mac: mac, label: song, phase: "uploading", seconds: 2, attempts: 0)
                                     : i < 9 ? Job.Chunk(state: "running", mac: mac, label: song, phase: i == 7 ? "running" : "running", seconds: i == 7 ? 21 : 52, attempts: 0)
                                     : Job.Chunk(state: "running", mac: mac, label: song, phase: "queued", seconds: 40, attempts: 0)
                             },
                             speedup: 1.9, created: 0, kind: "batch",
                             events: [
                                 .init(at: 1_792_878_701, text: "job Hip hop stems (2ecf6214): 10 tasks on This Mac[7.1], Sonakshi\u{2019}s MacBook Air[4.8]"),
                                 .init(at: 1_792_878_714, text: "  → Love You No More.mp3 on Sonakshi\u{2019}s MacBook Air (upload 12.3s)"),
                                 .init(at: 1_792_878_793, text: "  ✓ Love You No More.mp3 on Sonakshi\u{2019}s MacBook Air in 79.2s  [1/10]"),
                                 .init(at: 1_792_878_811, text: "  ✓ It's My Birthday.mp3 on This Mac in 109.3s  [2/10]"),
                                 .init(at: 1_792_878_955, text: "  ✓ That Bass.mp3 on Sonakshi\u{2019}s MacBook Air in 64.3s  [6/10]"),
                                 .init(at: 1_792_878_957, text: "  → We Own It.mp3 on Sonakshi\u{2019}s MacBook Air (upload 4.3s)"),
                             ])
    static let finished = [
        Job(id: "b1", name: "Launch ad (motion)", state: "done", error: "", output: "/tmp/x.mov", tasks: 8, done: 8, progress: 1,
            elapsed: 74, eta: nil, macs: [.init(nodeId: "a", name: "This Mac", done: 5, running: 0)], chunks: [],
            speedup: 2.1, created: 0, kind: "app"),
        Job(id: "b2", name: "Vlog 12", state: "failed", error: "Every Mac with filmcraft has a different build", output: "",
            tasks: 10, done: 0, progress: 0, elapsed: 3, eta: nil, macs: [], chunks: [], speedup: nil, created: 0, kind: "app"),
    ]

    static let planTask = AiTask(id: "p1", kind: "plan", state: "done", steps: ["Asking Claude Code to plan it", "Checking the plan"],
                                 result: AiResult(explanation: "Render the sequence in 40 chunks of 120 frames with FilmCraft, join them, then make a 720p HEVC preview from the result.",
                                                  planner: "job", tasks: 41, programs: ["filmcraft", "ffmpeg"],
                                                  example: ["filmcraft --compact --project {in:project} exec file.exportMedia path={out:result} format=prores startFrame=0 endFrame=120"],
                                                  merge: ["concat_video", "command"], safe: false,
                                                  reasons: ["runs a command on this Mac to put the result together"]),
                                 error: "", awaitingOk: true)
    static let teachTask = AiTask(id: "t1", kind: "teach", state: "done",
                                  steps: ["reading the program's help", "asking the agent for a profile (attempt 1)",
                                          "test render: whole, then in chunks", "test render failed: rendering frames 0-11 gave 11 frames, not 12",
                                          "asking the agent for a profile (attempt 2)", "test render: whole, then in chunks"],
                                  result: AiResult(explanation: "Renders frame ranges with -s/-e into numbered PNGs, after relinking textures.",
                                                   verified: "frames 0-11 render the same whole and in 3 chunks", attempts: 2,
                                                   path: "~/.macmesh/apps/blender.json"),
                                  error: "", awaitingOk: false)
    static let explainTask = AiTask(id: "e1", kind: "explain", state: "done", steps: [],
                                    result: AiResult(summary: "The Mac Studio has FilmCraft 0.7.1, while this Mac has 0.7.2, so macmesh wouldn't mix their frames and no Mac was left to run the job.",
                                                     fix: "Update FilmCraft's command-line tool on the Mac Studio to 0.7.2, or run the job on this Mac only.",
                                                     where: "another Mac"),
                                    error: "", awaitingOk: false)

    static func status(jobs: [Job], macs: [Mac]? = nil, paused: Int = 0, requests: Bool = false,
                       blockedFirewall: Bool = false) -> MeshStatus {
        let macs = macs ?? Self.macs
        return MeshStatus(nodeId: "ashwinsmacbookpro", name: "Ashwin's MacBook Pro", lend: true, accepting: true, reason: "",
                   running: 2, paused: paused, ownerMode: "shared", tls: true, holdsCa: true,
                   fingerprint: "0b4109f4ba1c7033bf0ac00db1c03e381d2d4f9a1864c0d393d8efde5d1aeeaa",
                   outputDir: "/Users/me/Movies/macmesh", mesh: "0b4109f4ba1c",
                   firewall: .init(on: true, blockAll: false, blocked: blockedFirewall, program: "/Applications/macmesh.app/Contents/Resources/engine/python/bin/python3.13"),
                   invite: .init(code: "7K2Q-M9XD-4TRA", state: "waiting", expires: nil, joined: nil, error: nil,
                                 requests: requests ? [.init(id: "r1", name: "Riya's MacBook", number: "736 487", age: 4)] : []),
                   macs: macs, jobs: jobs)
    }

    static func engine(_ status: MeshStatus?, phase: Engine.Phase = .running) -> Engine {
        let e = Engine(client: ControlClient(socketPath: "/nonexistent"), location: nil)
        e.live = false
        e.status = status
        e.ai = AiInfo(choice: "auto", using: "claude", problem: "", available: ["claude", "antigravity"], autorun: true)
        e.phase = phase
        e.apps = [
            AppProfile(name: "filmcraft", description: "FilmCraft video editor: renders the active sequence", installed: true,
                       build: "filmcraft-cli 0.7.2", extensions: [".fcproj"], vars: [:]),
            AppProfile(name: "effectcraft", description: "EffectCraft motion graphics: renders a composition", installed: true,
                       build: "effectcraft-cli 0.9.2", extensions: [".ecproj"], vars: [:]),
            AppProfile(name: "grade", description: "Grade colour grader and finisher: renders the current timeline",
                       installed: false, build: "", extensions: [".gradeproject"], vars: [:]),
        ]
        return e
    }

    static func render(to dir: URL, dark: Bool) {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let project = Inspection(kind: "app", path: "/Users/me/Movies/Interview.fcproj", name: "Interview", app: "filmcraft",
                                 description: nil, vars: [:], needsVars: [], installed: true, frames: 4800, fps: nil,
                                 note: nil, duration: nil, video: nil)
        let comp = Inspection(kind: "app", path: "/Users/me/Ads/Launch.ecproj", name: "Launch ad", app: "effectcraft",
                              description: nil, vars: [:], needsVars: ["comp"], installed: true, frames: 900, fps: 29.97,
                              note: nil, duration: nil, video: nil)
        let working = status(jobs: [running] + finished)
        let idle = status(jobs: finished, paused: 1)
        func dash(_ page: DashboardView.Page, _ e: Engine) -> AnyView {
            AnyView(DashboardView(page: page).environment(e).frame(width: 980, height: 640))
        }
        let screens: [(String, AnyView)] = [
            ("dashboard-jobs-running", dash(.jobs, engine(working))),
            ("dashboard-jobs-empty", dash(.jobs, engine(idle))),
            ("dashboard-macs", dash(.macs, engine(working))),
            ("dashboard-apps", dash(.apps, engine(working))),
            ("dashboard-lending", dash(.lending, engine(working))),
            ("dashboard-security", dash(.security, engine(working))),
            ("dashboard-setup", dash(.jobs, engine(nil, phase: .needsSetup))),
            ("newjob-empty", AnyView(DropSheetView().environment(engine(working)))),
            ("newjob-project", AnyView(DropSheetView(file: project).environment(engine(working)))),
            ("newjob-comp", AnyView(DropSheetView(file: comp).environment(engine(working)))),
            ("dashboard-ai", dash(.ai, engine(working))),
            ("newjob-describe", AnyView(DescribeJobView(files: [URL(filePath: "/Users/me/Movies/Interview.fcproj")],
                                                        request: "Export Interview at 4K in HEVC, then make a 720p preview too",
                                                        task: planTask).padding(22).frame(width: 520).environment(engine(working)))),
            ("teach-done", AnyView(TeachSheet(program: URL(filePath: "/Applications/Blender.app/Contents/MacOS/Blender"), name: "blender",
                                              sample: URL(filePath: "/Users/me/Scenes/Robot.blend"), task: teachTask)
                                   .environment(engine(working)))),
            ("explain", AnyView(ExplainSheet(job: finished[1], task: explainTask).environment(engine(working)))),
            ("menubar-working", AnyView(PopoverView().environment(engine(working)))),
            ("setup-nearby", AnyView(OnboardingView(nearby: [NearbyMesh(name: "Ashwin's MacBook Pro", addresses: ["192.168.0.103:7478"], mesh: "0b41")],
                                                    searching: false).environment(engine(nil, phase: .needsSetup)))),
            ("setup-number", AnyView(OnboardingView(nearby: [], searching: false,
                                                    joining: NearbyMesh(name: "Ashwin's MacBook Pro", addresses: [], mesh: "0b41"),
                                                    number: "736 487").environment(engine(nil, phase: .needsSetup)))),
            ("mesh-sheet", AnyView(MeshSheet().environment(engine(status(jobs: [], requests: true))))),
            ("dashboard-request", dash(.jobs, engine(status(jobs: finished, requests: true, blockedFirewall: true)))),
            ("dashboard-request-only", dash(.jobs, engine(status(jobs: finished, requests: true)))),
            ("dashboard-firewall-only", dash(.jobs, engine(status(jobs: finished, blockedFirewall: true)))),
            ("menubar-request", AnyView(PopoverView().environment(engine(status(jobs: [], requests: true))))),
            ("menubar-idle", AnyView(PopoverView().environment(engine(idle)))),
            ("menubar-icons", AnyView(HStack(spacing: 28) {
                ForEach(0 ..< 3) { i in
                    Image(nsImage: MenuBarLabel.icon(working: i == 1, waiting: i == 2))
                        .resizable().interpolation(.high).frame(width: 96, height: 64)
                }
            }.padding(24).background(Color(white: 0.93)))),
            ("install-banners", AnyView(VStack(spacing: 10) {
                ForEach(["asking", "installing", "done", "failed"], id: \.self) { state in
                    InstallRequestBanner(request: .init(program: "demucs-stem",
                        summary: "Demucs stem separation: Python packages and the htdemucs model, about 1 GB",
                        by: "Ashwin\u{2019}s MacBook Pro", state: state, detail: "Step 2 of 3: pip install demucs==4.1.0",
                        error: "step 2 failed (exit 1): No space left on device"))
                }
            }.padding(20).frame(width: 560).environment(engine(idle)))),
        ]
        NSApplication.shared.setActivationPolicy(.prohibited)
        NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        for (name, view) in screens {
            // A real (offscreen) window, so AppKit-backed controls draw too; the view caches its own
            // drawing, which needs no screen recording permission.
            let host = NSHostingView(rootView: view.background(Color(nsColor: .windowBackgroundColor)))
            host.frame.size = host.fittingSize
            // Dashboards get a real titled window (as in the app); sheets and popovers a plain one.
            let style: NSWindow.StyleMask = name.hasPrefix("dashboard") ? [.titled, .closable, .resizable] : [.borderless]
            let window = NSWindow(contentRect: NSRect(origin: NSPoint(x: -10_000, y: -10_000), size: host.fittingSize),
                                  styleMask: style, backing: .buffered, defer: false)
            window.contentView = host
            window.orderFrontRegardless()
            RunLoop.main.run(until: Date().addingTimeInterval(0.6))
            host.layoutSubtreeIfNeeded()
            guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { continue }
            host.cacheDisplay(in: host.bounds, to: rep)
            window.orderOut(nil)
            let url = dir.appending(path: "\(name)\(dark ? "-dark" : "").png")
            try? rep.representation(using: .png, properties: [:])?.write(to: url)
            print(url.path)
        }
    }
}
