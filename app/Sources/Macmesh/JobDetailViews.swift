import Charts
import SwiftUI

/// Below a running job's progress: each Mac's live activity, load and transfers; every chunk; and
/// what has happened so far.
struct JobDetails: View {
    var job: Job
    var color: (String) -> Color

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Macs").font(.caption).foregroundStyle(.secondary)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 300), spacing: 12)], spacing: 12) {
                    ForEach(job.macs, id: \.name) { mac in MacActivityCard(mac: mac, color: color(mac.name)) }
                }
            }
            ChunkList(job: job, color: color)
            ActivityFeed(events: job.events ?? [])
        }
    }
}

/// One Mac in a running job: what it's doing now, its CPU and GPU over the last 2 minutes, and
/// how much has gone to and from it. Every part has a fixed size, so the card never jumps.
struct MacActivityCard: View {
    var mac: Job.JobMac
    var color: Color

    var body: some View {
        let last = mac.load?.last
        let lines = activities
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 9, height: 9)
                Text(mac.name).font(.body).fontWeight(.medium).foregroundStyle(.primary).lineLimit(1)
                Spacer()
                if let duty = last?.duty, duty < 0.97 {
                    Text("\(Int((duty * 100).rounded()))% speed \u{00B7}")
                        .help("Slowed to keep \(mac.name) under \(Int(((last?.limit ?? 0.9) * 100).rounded()))% for its owner")
                }
                Text("\(mac.done) done")
            }
            .font(.caption.weight(.regular)).foregroundStyle(.secondary).monospacedDigit()
            VStack(alignment: .leading, spacing: 2) {
                Text(lines.first ?? "").font(.callout).lineLimit(1).truncationMode(.middle)
                Text(lines.count > 1 ? lines[1] : " ").font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            .frame(maxWidth: .infinity, minHeight: 36, maxHeight: 36, alignment: .topLeading)
            LoadChart(points: mac.load ?? [])
            HStack(spacing: 10) {
                reading("CPU", last.map { $0.cpu }, .blue)
                    .help("CPU used by this job, as a share of all \(last?.cores.map(String.init) ?? "") cores")
                reading("GPU", last?.gpu, .orange)
                    .help("The whole GPU's load (macOS shows GPU per app only to an administrator)")
                Spacer(minLength: 4)
                Text("\u{2191} \(Format.bytes(mac.sent ?? 0))  \u{2193} \(Format.bytes(mac.received ?? 0))")
                    .foregroundStyle(.secondary).lineLimit(1).fixedSize().layoutPriority(1)
                    .help("Sent to \(mac.name) for this job, and results fetched back")
            }
            .font(.caption).monospacedDigit()
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.04)))
    }

    private func reading(_ name: String, _ value: Double?, _ tint: Color) -> some View {
        HStack(spacing: 4) {
            Circle().fill(tint).frame(width: 6, height: 6)
            Text(name).foregroundStyle(.secondary)
            Text(value.map { "\(Int($0.rounded()))%" } ?? "\u{2013}").frame(width: 32, alignment: .leading)
        }
    }

    /// What the Mac is doing, most important first: at most two lines.
    private var activities: [String] {
        var lines = (mac.installing ?? []).map { "Installing \($0) (its owner allowed it)" }
        let doing = (mac.doing ?? []).sorted { order($0.phase) < order($1.phase) }
        lines += doing.map { a in
            switch a.phase {
            case "uploading": "Sending \(a.label) \u{00B7} \(Format.bytes(a.bytes)) of \(Format.bytes(a.total))"
            case "queued": "Next: \(a.label)"
            case "paused": "\(a.label) \u{00B7} paused while its owner works"
            case "downloading": "Fetching \(a.label)\u{2019}s result \u{00B7} \(Format.bytes(a.bytes)) of \(Format.bytes(a.total))"
            default: "Working on \(a.label) \u{00B7} \(Format.clock(a.since))"
            }
        }
        return lines.isEmpty ? ["Idle"] : lines
    }

    private func order(_ phase: String) -> Int {
        ["running": 0, "paused": 1, "downloading": 2, "uploading": 3, "queued": 4][phase] ?? 5
    }
}

/// CPU used by the job and the Mac's GPU load, over the last 2 minutes (a point every 2 s), in a
/// fixed-size box: no axis labels, one faint line at 50%.
struct LoadChart: View {
    var points: [Job.LoadPoint]

    var body: some View {
        let now = points.last?.at ?? 0
        let shown = points.filter { now - $0.at <= 120 }  // the window; older points would draw outside it
        Chart {
            ForEach(Array(shown.enumerated()), id: \.offset) { _, p in
                LineMark(x: .value("Time", p.at - now), y: .value("Load", min(p.cpu, 100)), series: .value("Kind", "CPU"))
                    .foregroundStyle(.blue)
                    .lineStyle(StrokeStyle(lineWidth: 1.5))
                if let gpu = p.gpu {
                    LineMark(x: .value("Time", p.at - now), y: .value("Load", min(gpu, 100)), series: .value("Kind", "GPU"))
                        .foregroundStyle(.orange)
                        .lineStyle(StrokeStyle(lineWidth: 1.5))
                }
            }
        }
        .chartPlotStyle { $0.clipped() }
        .chartXScale(domain: -120...0)
        .chartYScale(domain: 0...100)
        .chartXAxis(.hidden)
        .chartYAxis {
            AxisMarks(values: [50]) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5, dash: [3, 3]))
            }
        }
        .chartLegend(.hidden)
        .frame(maxWidth: .infinity, minHeight: 56, maxHeight: 56)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.03)))
        .overlay {
            if shown.count < 2 {
                Text("Measuring\u{2026}").font(.caption).foregroundStyle(.tertiary)
            }
        }
        .transaction { $0.animation = nil }  // new samples replace the lines; no morphing
        .accessibilityLabel("CPU \(Int(points.last?.cpu ?? 0)) percent, GPU \(Int(points.last?.gpu ?? 0)) percent")
    }
}

/// Every chunk: what it is, where it ran, how far it got.
struct ChunkList: View {
    var job: Job
    var color: (String) -> Color
    @State private var open = true

    var body: some View {
        DisclosureGroup(isExpanded: $open) {
            LazyVStack(spacing: 0) {
                ForEach(Array(job.chunks.enumerated()), id: \.offset) { i, chunk in
                    HStack(spacing: 10) {
                        icon(chunk).frame(width: 16)
                        Text(chunk.label ?? "Chunk \(i + 1)").lineLimit(1).truncationMode(.middle)
                        Spacer(minLength: 12)
                        if !chunk.mac.isEmpty {
                            HStack(spacing: 4) {
                                RoundedRectangle(cornerRadius: 2).fill(color(chunk.mac)).frame(width: 7, height: 7)
                                Text(chunk.mac).lineLimit(1)
                            }
                            .frame(width: 170, alignment: .leading)
                        } else {
                            Text("").frame(width: 170)
                        }
                        Text(phase(chunk)).foregroundStyle(.secondary).frame(width: 110, alignment: .leading)
                        Text(chunk.seconds.map { Format.clock($0) } ?? "").monospacedDigit().foregroundStyle(.secondary)
                            .frame(width: 48, alignment: .trailing)
                    }
                    .font(.callout)
                    .padding(.vertical, 5)
                    .padding(.horizontal, 8)
                    .background(i.isMultiple(of: 2) ? Color.primary.opacity(0.025) : .clear)
                }
            }
            .padding(.top, 6)
        } label: {
            Text("Chunks (\(job.done) of \(job.tasks) done)").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func phase(_ c: Job.Chunk) -> String {
        switch c.phase ?? c.state {
        case "done": (c.attempts ?? 0) > 1 ? "Done (retried)" : "Done"
        case "uploading": "Sending"
        case "queued": "Next in line"
        case "running": "Working"
        case "paused": "Paused"
        case "downloading": "Fetching result"
        default: "Waiting"
        }
    }

    @ViewBuilder private func icon(_ c: Job.Chunk) -> some View {
        switch c.phase ?? c.state {
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case "running": ProgressView().controlSize(.mini)
        case "uploading", "downloading": Image(systemName: "arrow.up.arrow.down.circle").foregroundStyle(.blue)
        case "paused": Image(systemName: "pause.circle").foregroundStyle(.orange)
        default: Image(systemName: "circle.dotted").foregroundStyle(.tertiary)
        }
    }
}

/// The job's events as they happened: sent, finished, retried, paused, copied to a faster Mac.
struct ActivityFeed: View {
    var events: [Job.Event]
    @State private var open = true

    var body: some View {
        DisclosureGroup(isExpanded: $open) {
            VStack(alignment: .leading, spacing: 3) {
                ForEach(Array(events.suffix(14).reversed().enumerated()), id: \.offset) { _, e in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(Date(timeIntervalSince1970: e.at), format: .dateTime.hour().minute().second())
                            .monospacedDigit().foregroundStyle(.tertiary)
                        Text(Format.event(e.text)).lineLimit(2)
                    }
                    .font(.caption)
                }
                if events.isEmpty { Text("Nothing yet").font(.caption).foregroundStyle(.tertiary) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 6)
        } label: {
            Text("Activity").font(.caption).foregroundStyle(.secondary)
        }
    }
}
