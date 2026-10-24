import SwiftUI

/// Light, dark, or whatever macOS is set to: the owner's choice, kept between launches.
enum Appearance: String, CaseIterable, Identifiable {
    case system, light, dark

    static let key = "appearance"
    var id: String { rawValue }
    var title: String { self == .system ? "Match System" : rawValue.capitalized }
    var symbol: String {
        switch self {
        case .system: "circle.lefthalf.filled"
        case .light: "sun.max"
        case .dark: "moon"
        }
    }

    static var current: Appearance { Appearance(rawValue: UserDefaults.standard.string(forKey: key) ?? "") ?? .system }

    @MainActor static func apply(_ choice: Appearance = current) {
        NSApp.appearance = switch choice {
        case .system: nil
        case .light: NSAppearance(named: .aqua)
        case .dark: NSAppearance(named: .darkAqua)
        }
    }
}

/// The toolbar's appearance menu: Match System, Light or Dark.
struct AppearanceMenu: View {
    @AppStorage(Appearance.key) private var choice: Appearance = .system

    var body: some View {
        Menu {
            Picker("Appearance", selection: $choice) {
                ForEach(Appearance.allCases) { Label($0.title, systemImage: $0.symbol).tag($0) }
            }
            .pickerStyle(.inline)
            .labelsHidden()
        } label: {
            Label("Appearance", systemImage: choice.symbol)
        }
        .help("Light, dark, or match macOS")
        .onChange(of: choice) { _, new in Appearance.apply(new) }
    }
}
