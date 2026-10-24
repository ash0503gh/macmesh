import SwiftUI

/// A Mac as a small round avatar (the drop sheet's Mac picker and the job cards).
struct MacAvatar: View {
    var mac: Mac
    var selected: Bool
    var size: CGFloat = 52

    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                Circle()
                    .strokeBorder(selected ? Color.accentColor : Color.secondary.opacity(0.3), lineWidth: selected ? 2 : 1)
                    .background(Circle().fill(selected ? Color.accentColor.opacity(0.08) : .clear))
                Image(systemName: mac.symbol)
                    .font(.system(size: size * 0.4, weight: .regular))
                    .foregroundStyle(mac.available ? .primary : .tertiary)
            }
            .frame(width: size, height: size)
            Text(mac.thisMac ? "This Mac" : mac.name)
                .font(.caption)
                .lineLimit(1)
                .foregroundStyle(mac.available ? .primary : .secondary)
            if !mac.available {
                Text(Format.macState(mac))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(width: size + 34)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// One Mac as a list row: icon, name, what it's doing.
struct MacRow: View {
    var mac: Mac

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: mac.symbol)
                .frame(width: 20)
                .foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(mac.thisMac ? "\(mac.name) (this Mac)" : mac.name)
                    .lineLimit(1)
                if let chip = mac.chip {
                    Text(chip + (mac.fanless ? " · fanless" : ""))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 8)
            StateLabel(mac: mac)
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .combine)
    }
}

struct StateLabel: View {
    var mac: Mac

    var color: Color {
        switch mac.state {
        case "working": .green
        case "ready": .secondary
        case "paused": .orange
        default: .secondary
        }
    }

    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(mac.state == "unavailable" ? Color.secondary.opacity(0.4) : color).frame(width: 7, height: 7)
            Text(Format.macState(mac))
                .font(.caption)
                .foregroundStyle(mac.state == "paused" ? Color.orange : .secondary)
                .lineLimit(1)
        }
    }
}

/// A running or finished job.
struct JobCard: View {
    var job: Job
    var onCancel: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Image(systemName: job.kind == "video" ? "film" : "square.stack.3d.up")
                    .foregroundStyle(.secondary)
                Text(job.name).fontWeight(.medium).lineLimit(1)
                Spacer()
                if job.isActive {
                    Button(action: onCancel) { Image(systemName: "xmark.circle.fill") }
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                        .help("Cancel this job")
                        .accessibilityLabel("Cancel \(job.name)")
                }
            }
            if job.isActive {
                ProgressView(value: job.state == "merging" ? 1 : job.progress)
                    .progressViewStyle(.linear)
            }
            HStack {
                Text(Format.jobState(job))
                    .font(.caption)
                    .foregroundStyle(job.state == "failed" ? .red : .secondary)
                    .lineLimit(2)
                Spacer()
                if job.isActive, job.tasks > 0 {
                    Text("\(job.done) of \(job.tasks) chunks")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
            if job.isActive, !job.macs.isEmpty {
                Text(job.macs.map { "\($0.name) \($0.done)" }.joined(separator: " · "))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.04)))
    }
}

/// A row for a finished job: name, how it went, and a way to the result.
struct RecentJobRow: View {
    var job: Job

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: job.state == "done" ? "checkmark.circle.fill" : job.state == "failed" ? "exclamationmark.triangle.fill" : "minus.circle")
                .foregroundStyle(job.state == "done" ? .green : job.state == "failed" ? .orange : .secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(job.name).lineLimit(1)
                Text(Format.jobState(job)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            if job.state == "done", !job.output.isEmpty {
                Button("Show") { NSWorkspace.shared.activateFileViewerSelecting([URL(filePath: job.output)]) }
                    .controlSize(.small)
                    .help("Show the result in Finder")
            }
        }
    }
}

struct SectionLabel: View {
    var text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text).font(.caption).fontWeight(.medium).foregroundStyle(.secondary)
    }
}
