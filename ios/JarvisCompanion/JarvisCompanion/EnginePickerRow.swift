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

    /// Why the stored pick cannot be used right now (nil = usable). A down or
    /// removed engine is never swapped for Hub behind the owner's back.
    func unavailableNote(_ id: String) -> String? {
        Self.unavailableNote(id, in: engines)
    }

    nonisolated private static func unavailableNote(_ id: String, in list: [EngineOption]) -> String? {
        guard id != EngineOption.hub.id else { return nil }
        let option = list.first { $0.id == id }
        if option?.available == true { return nil }
        let name = option?.label ?? id
        if let reason = option?.reason, !reason.isEmpty { return "\(name) unavailable: \(reason)" }
        return "\(name) unavailable"
    }

    /// Owner 2026-11-07: every successful send puts the picker back on Hub
    /// (the default), like the one-shot "New session" toggle.
    nonisolated static func resetToDefault() {
        UserDefaults.standard.set(EngineOption.hub.id, forKey: "engineID")
    }

    /// What a send carries: the engine id (nil = Hub, field omitted), or the
    /// reason the send must wait for another pick. Reads the stored pick and the
    /// cached list straight from UserDefaults, so non-main callers
    /// (AppModel.ask) need no actor hop.
    nonisolated static func sendChoice() -> (engine: String?, blocked: String?) {
        let id = UserDefaults.standard.string(forKey: "engineID") ?? EngineOption.hub.id
        guard id != EngineOption.hub.id else { return (nil, nil) }
        let list = UserDefaults.standard.data(forKey: cacheKey)
            .flatMap { try? JSONDecoder().decode([EngineOption].self, from: $0) } ?? []
        if let note = unavailableNote(id, in: list) {
            return (nil, note + ". Pick another engine.")
        }
        return (id, nil)
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

/// The ask bar's control row: engine picker, then the one-shot "New session"
/// toggle, aligned with the text field. Stacked when dynamic type no longer fits.
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
    @Environment(\.dynamicTypeSize) private var typeSize

    /// Paperclip (44) + its gap (8): the row starts where the text field starts.
    private static let fieldInset: CGFloat = 52

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                engineMenu
                newSessionToggle
                Spacer(minLength: 0)
            }
            VStack(alignment: .leading, spacing: 8) {
                engineMenu
                newSessionToggle
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        // at accessibility sizes the text needs the full width more than the alignment
        .padding(.leading, typeSize.isAccessibilitySize ? 0 : Self.fieldInset)
        .task { if refreshOnAppear { await store.refresh() } }
    }

    /// One line normally; wraps freely at accessibility sizes (never truncates there).
    private var labelLines: Int? { typeSize.isAccessibilitySize ? nil : 1 }

    /// Non-nil when the picked engine is down or gone (not shown while locked).
    private var warning: String? {
        lockedEngine == nil ? store.unavailableNote(engineID) : nil
    }

    private var currentLabel: String {
        if let locked = lockedEngine { return EngineStore.label(forSession: locked) }
        if warning != nil {
            let name = store.engines.first { $0.id == engineID }?.label ?? engineID
            return "\(name) unavailable"
        }
        return store.resolved(engineID).label
    }

    private var engineMenu: some View {
        Menu {
            ForEach(store.engines) { option in
                Button {
                    engineID = option.id
                } label: {
                    // A menu item shows its first Text as the title, the second as the subtitle.
                    if option.id == engineID {
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
                if lockedEngine != nil {
                    Image(systemName: "lock.fill").font(.caption)
                } else if warning != nil {
                    Image(systemName: "exclamationmark.triangle.fill").font(.caption)
                }
                Text(currentLabel).lineLimit(labelLines)
                if lockedEngine == nil {
                    Image(systemName: "chevron.down").font(.caption.weight(.semibold))
                }
            }
            .font(.subheadline)
            .foregroundColor(lockedEngine != nil ? Color.jarvisMuted : (warning != nil ? Color.jarvisDanger : Color.jarvisText))
            .padding(.horizontal, lockedEngine == nil ? 16 : 4)
            .frame(minHeight: 44)
            .background {
                // locked = plain muted text (fixed, not a control); otherwise the house chip
                if lockedEngine == nil {
                    RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface)
                }
            }
            .contentShape(Rectangle())
        }
        .disabled(lockedEngine != nil)
        .accessibilityLabel("Engine")
        .accessibilityValue(warning.map { "\($0). Pick another engine." } ?? currentLabel)
        .accessibilityHint(lockedEngine == nil ? "" : "Set by this session")
    }

    /// Second text of a menu item: menus render it as the subtitle line.
    private func subtitle(for option: EngineOption) -> String? {
        if option.id == EngineOption.hub.id { return "Default" }
        if !option.available { return option.reason }
        return nil
    }

    private func toggleLabel(on: Bool) -> some View {
        HStack(spacing: 6) {
            Image(systemName: on ? "checkmark" : "plus.bubble")
            Text("New session").lineLimit(labelLines)
            if on { Text("On").fontWeight(.semibold) }
        }
    }

    private var newSessionToggle: some View {
        Button {
            newSession.toggle()
        } label: {
            // the hidden "on" label reserves the width, so the pill never jumps
            ZStack(alignment: .leading) {
                toggleLabel(on: true).hidden()
                toggleLabel(on: newSession)
            }
            .font(.subheadline)
            // on: accent text on a faint accent tint (AA on the dark background);
            // the solid accent stays reserved for the send button
            .foregroundColor(newSession ? .jarvisAccent : .jarvisText)
            .padding(.horizontal, 16)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
            .background {
                if newSession {
                    RoundedRectangle(cornerRadius: jarvisRadius)
                        .fill(Color.jarvisAccent.opacity(0.18))
                        .overlay(RoundedRectangle(cornerRadius: jarvisRadius)
                            .strokeBorder(Color.jarvisAccent, lineWidth: 1.5))
                } else {
                    RoundedRectangle(cornerRadius: jarvisRadius).fill(Color.jarvisSurface)
                }
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.15), value: newSession)
        .accessibilityLabel("New session")
        .accessibilityValue(newSession ? "On" : "Off")
        .accessibilityHint("Starts a separate session for your next message, then turns off")
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
