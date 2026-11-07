import SwiftUI

/// One engine the gateway offers (GET /central/engines): `id` is what /chat
/// and /siri/hey send back, `label` is what the owner sees. The JSON key
/// `default` is a Swift keyword, hence the CodingKey.
struct EngineOption: Codable, Identifiable, Equatable {
    let id: String
    let label: String
    let kind: String
    let available: Bool
    let isDefault: Bool
    /// Why `available` is false (nil when available).
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case id, label, kind, available, reason
        case isDefault = "default"
    }

    /// Fallback when the gateway is unreachable or an id no longer exists.
    static let hub = EngineOption(
        id: "hub", label: "Hub", kind: "hub", available: true, isDefault: true, reason: nil
    )
}

@MainActor
final class EngineStore: ObservableObject {
    static let shared = EngineStore()
    nonisolated private static let cacheKey = "engineListCache"

    @Published private(set) var engines: [EngineOption]

    private init() {
        if let data = UserDefaults.standard.data(forKey: Self.cacheKey),
           let list = try? JSONDecoder().decode([EngineOption].self, from: data), !list.isEmpty {
            engines = Self.withHubFirst(list)
        } else {
            engines = [.hub]
        }
    }

    /// Tests: a fixed list, no network.
    func seed(_ list: [EngineOption]) {
        engines = Self.withHubFirst(list)
    }

    func refresh() async {
        do {
            let data = try await Central.data(Central.request("/central/engines"))
            let list = try JSONDecoder().decode(EngineList.self, from: data).engines
            engines = Self.withHubFirst(list)
            UserDefaults.standard.set(try? JSONEncoder().encode(engines), forKey: Self.cacheKey)
        } catch {
            // Offline or an older gateway: the cached list (or Hub alone) stays.
        }
    }

    /// The option a picker id resolves to: only an AVAILABLE engine is real;
    /// anything missing or down falls back to Hub.
    func resolved(_ id: String) -> EngineOption {
        engines.first { $0.id == id && $0.available } ?? .hub
    }

    /// The engine id a send carries, or nil for Hub (the field is then omitted).
    /// Reads the stored pick and the cached list straight from UserDefaults, so
    /// non-main callers (AppModel.ask) need no actor hop; same rule as resolved().
    nonisolated static func sendEngineID() -> String? {
        let id = UserDefaults.standard.string(forKey: "engineID") ?? EngineOption.hub.id
        guard id != EngineOption.hub.id,
              let data = UserDefaults.standard.data(forKey: cacheKey),
              let list = try? JSONDecoder().decode([EngineOption].self, from: data),
              list.contains(where: { $0.id == id && $0.available }) else { return nil }
        return id
    }

    /// Session rows carry raw engine ids ("claude" means the Hub).
    static func label(forSession engine: String?) -> String {
        guard let id = engine, !id.isEmpty, id != "claude", id != "hub" else { return EngineOption.hub.label }
        if id == "glm" { return "GLM" }
        if id == "agy" { return "Antigravity" }
        return shared.engines.first { $0.id == id }?.label ?? id
    }

    /// Hub always leads the list: it is the default and the fallback.
    private static func withHubFirst(_ list: [EngineOption]) -> [EngineOption] {
        var result = list
        if let index = result.firstIndex(where: { $0.id == EngineOption.hub.id }) {
            result.insert(result.remove(at: index), at: 0)
        } else {
            result.insert(.hub, at: 0)
        }
        return result
    }

    private struct EngineList: Codable {
        let engines: [EngineOption]
    }
}

/// The ask bar's control row: engine picker left, one-shot "New session"
/// toggle right. Below each other when dynamic type no longer fits side by side.
@MainActor
struct EnginePickerRow: View {
    @Binding var engineID: String
    @Binding var newSession: Bool
    /// Non-nil (a bound chat session): the engine is that session's, no menu.
    var lockedEngine: String? = nil
    /// Tests seed the shared store and skip the network refresh.
    var refreshOnAppear = true
    @ObservedObject var store = EngineStore.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack {
                engineMenu
                Spacer(minLength: 8)
                newSessionToggle
            }
            VStack(alignment: .leading, spacing: 8) {
                engineMenu
                newSessionToggle
            }
        }
        .task { if refreshOnAppear { await store.refresh() } }
    }

    private var currentLabel: String {
        if let locked = lockedEngine { return EngineStore.label(forSession: locked) }
        return store.resolved(engineID).label
    }

    private var engineMenu: some View {
        Menu {
            ForEach(store.engines) { option in
                Button {
                    engineID = option.id
                } label: {
                    // A menu item shows its first Text as the title, the second as the subtitle.
                    if option.id == store.resolved(engineID).id {
                        Label(option.label, systemImage: "checkmark")
                    } else {
                        Text(option.label)
                    }
                    if let subtitle = subtitle(for: option) {
                        Text(subtitle)
                    }
                }
                .disabled(!option.available)
            }
        } label: {
            HStack(spacing: 6) {
                Text(currentLabel).lineLimit(1)
                if lockedEngine == nil {
                    Image(systemName: "chevron.down").font(.caption.weight(.semibold))
                }
            }
            .font(.subheadline)
            .foregroundColor(.jarvisText)
            .padding(.horizontal, 16)
            .frame(minHeight: 44)
            .background(RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface))
            .contentShape(Rectangle())
        }
        .disabled(lockedEngine != nil)
        .accessibilityLabel("Engine")
        .accessibilityValue(currentLabel)
        .accessibilityHint(lockedEngine == nil ? "" : "Set by this session")
    }

    /// Second text of a menu item: menus render it as the subtitle line.
    private func subtitle(for option: EngineOption) -> String? {
        if option.id == EngineOption.hub.id { return "Default" }
        if !option.available { return option.reason }
        return nil
    }

    private var newSessionToggle: some View {
        Button {
            newSession.toggle()
        } label: {
            HStack(spacing: 6) {
                Image(systemName: newSession ? "checkmark" : "plus.bubble")
                Text("New session").lineLimit(1)
            }
            .font(.subheadline)
            .foregroundColor(newSession ? .white : .jarvisText)
            .padding(.horizontal, 16)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
            .background {
                if newSession {
                    RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisAccent)
                } else {
                    RoundedRectangle(cornerRadius: jarvisRadius)
                        .strokeBorder(Color.jarvisMuted.opacity(0.6), lineWidth: 1)
                }
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.15), value: newSession)
        .accessibilityLabel("New session")
        .accessibilityValue(newSession ? "On" : "Off")
        .accessibilityHint("Starts a separate session for the next message")
        .modifier(TogglePillTraits())
    }
}

/// iOS 17 announces toggles with their own trait; iOS 16 keeps the button one.
private struct TogglePillTraits: ViewModifier {
    func body(content: Content) -> some View {
        Group {
            if #available(iOS 17, *) {
                content.accessibilityAddTraits(.isToggle)
            } else {
                content.accessibilityAddTraits(.isButton)
            }
        }
    }
}
