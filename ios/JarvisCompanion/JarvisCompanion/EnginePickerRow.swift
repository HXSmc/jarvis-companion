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
    /// A local-only engine: its chats never reach any other model.
    var localOnly = false

    enum CodingKeys: String, CodingKey {
        case id, label, kind, available, reason
        case isDefault = "default"
        case localOnly = "local_only"
    }

    /// Fallback when the gateway is unreachable or an id no longer exists.
    static let hub = EngineOption(
        id: "hub", label: "Hub", kind: "hub", available: true, isDefault: true, reason: nil
    )

    /// The label without the gateway's own "· Local only" suffix.
    var baseLabel: String {
        label.replacingOccurrences(of: #"\s*·\s*local only"#, with: "", options: [.regularExpression, .caseInsensitive])
    }

    /// THE local test, for display and sending alike. Fails closed: an older gateway
    /// without `local_only` still counts an engine as local by its kind or label.
    var isLocalOption: Bool {
        localOnly || kind == "local" || label.range(of: "local only", options: .caseInsensitive) != nil
    }

    /// What the picker shows (desktop optionLabel): a usable local engine always says
    /// "Local only", once; a down one drops it (the menu subtitle gives the reason).
    var displayLabel: String {
        if !available { return baseLabel }
        if isLocalOption && label.range(of: "local only", options: .caseInsensitive) == nil { return label + " · Local only" }
        return label
    }

    /// VoiceOver value for a local engine; plain label otherwise.
    var spokenLabel: String {
        isLocalOption ? "\(baseLabel), local only, nothing leaves your devices" : label
    }
}

extension EngineOption {
    /// In an extension so the memberwise init stays; an older gateway sends no
    /// `local_only`, which decodes as false.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        label = try c.decode(String.self, forKey: .label)
        kind = try c.decode(String.self, forKey: .kind)
        available = try c.decode(Bool.self, forKey: .available)
        isDefault = try c.decode(Bool.self, forKey: .isDefault)
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        localOnly = try c.decodeIfPresent(Bool.self, forKey: .localOnly) ?? false
    }
}

@MainActor
final class EngineStore: ObservableObject {
    static let shared = EngineStore()
    nonisolated static let cacheKey = "engineListCache"

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
    /// (the default), like the one-shot "New session" toggle. Only when the pick is
    /// still the one that send used: a newer pick is the owner's, never undone.
    nonisolated static func resetToDefault(ifStill id: String?) {
        guard let id = id, storedPick() == id else { return }
        UserDefaults.standard.set(EngineOption.hub.id, forKey: "engineID")
    }

    /// What a send carries: the engine id (nil = Hub, field omitted), or the
    /// reason the send must wait for another pick. Reads the stored pick and the
    /// cached list straight from UserDefaults, so non-main callers
    /// (AppModel.ask) need no actor hop.
    nonisolated static func sendChoice() -> (engine: String?, blocked: String?) {
        let id = storedPick()
        guard id != EngineOption.hub.id else { return (nil, nil) }
        if let note = unavailableNote(id, in: cachedList()) {
            return (nil, note + ". Pick another engine.")
        }
        return (id, nil)
    }

    nonisolated private static func storedPick() -> String {
        UserDefaults.standard.string(forKey: "engineID") ?? EngineOption.hub.id
    }

    nonisolated private static func cachedList() -> [EngineOption] {
        UserDefaults.standard.data(forKey: cacheKey)
            .flatMap { try? JSONDecoder().decode([EngineOption].self, from: $0) } ?? []
    }

    /// True when `id` is a local-only engine in the cached list.
    nonisolated static func isLocal(_ id: String?) -> Bool {
        guard let id = id else { return false }
        return cachedList().contains { $0.id == id && $0.isLocalOption }
    }

    /// True when `id` is surely NOT local: a built-in cloud engine, or listed as non-local.
    nonisolated static func isKnownCloud(_ id: String?) -> Bool {
        guard let id = id else { return false }
        if ["hub", "claude", "glm", "agy"].contains(id) { return true }
        return cachedList().contains { $0.id == id && !$0.isLocalOption }
    }

    /// Desktop usesQuickAsk: /ask is a cloud pipeline, so "?" text takes it only for
    /// Hub or a listed engine that is not local-only. An unlisted pick never does.
    nonisolated static func usesQuickAsk(_ text: String) -> Bool {
        guard text.hasPrefix("?") else { return false }
        let id = storedPick()
        if id == EngineOption.hub.id { return true }
        guard let option = cachedList().first(where: { $0.id == id }) else { return false }
        return !option.isLocalOption
    }

    /// Router-worded local-model errors (desktop RAW_ERR).
    nonisolated static func isLocalModelError(_ message: String) -> Bool {
        ["Local model unavailable:", "This chat belongs to", "Local model failed:"].contains { message.hasPrefix($0) }
    }

    /// A local-model error shows verbatim; anything else keeps the caller's prefix.
    nonisolated static func errorText(_ message: String, prefix: String) -> String {
        isLocalModelError(message) ? message : prefix + message
    }

    /// Which machine answered a local chat (desktop upstreamNote); nil for anything else.
    nonisolated static func upstreamNote(_ upstream: String?) -> String? {
        switch upstream {
        case "gaming-pc": return "Gaming PC"
        case "mini-pc": return "Mini PC · Slower: running on the mini PC"
        default: return nil
        }
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

/// The ask bar's control row: engine picker and the one-shot "New session"
/// toggle, each taking half the composer width (owner 2026-11-07: no empty
/// sides). Stacked when dynamic type no longer fits.
@MainActor
struct EnginePickerRow: View {
    @Binding var engineID: String
    @Binding var newSession: Bool
    /// Non-nil (a bound chat session): the engine is that session's, no menu.
    var lockedEngine: String? = nil
    /// The locked chat is known to be local-only (its session row says so), even
    /// when the cached engine list does not list its engine.
    var lockedLocalOnly = false
    /// Shown instead of an engine name while the chat's engine is not known yet.
    var lockedLabel: String? = nil
    /// Tests seed the shared store and skip the network refresh.
    var refreshOnAppear = true
    @ObservedObject var store = EngineStore.shared
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                engineMenu
                newSessionToggle
            }
            VStack(alignment: .leading, spacing: 8) {
                engineMenu
                newSessionToggle
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .task { if refreshOnAppear { await store.refresh() } }
    }

    /// One line normally; wraps freely at accessibility sizes (never truncates there).
    private var labelLines: Int? { typeSize.isAccessibilitySize ? nil : 1 }

    /// One line never truncates: a chip keeps its label's full width, so a long
    /// label takes more than half the row, and the row stacks when both cannot fit.
    private var keepsLabelWidth: Bool { !typeSize.isAccessibilitySize }

    /// Non-nil when the picked engine is down or gone (not shown while locked).
    private var warning: String? {
        lockedEngine == nil ? store.unavailableNote(engineID) : nil
    }

    /// The usable local option the row shows (locked session engine or the pick), if any.
    private var localOption: EngineOption? {
        if warning != nil || lockedLabel != nil { return nil }
        let id = lockedEngine ?? engineID
        if let option = store.engines.first(where: { $0.id == id && $0.isLocalOption && $0.available }) { return option }
        // never a local chip for a cloud engine, whatever the caller says
        guard lockedLocalOnly, let locked = lockedEngine, !EngineStore.isKnownCloud(locked) else { return nil }
        return EngineOption(id: locked, label: EngineStore.label(forSession: locked), kind: "local",
                            available: true, isDefault: false, reason: nil, localOnly: true)
    }

    private var currentLabel: String {
        if let pending = lockedLabel { return pending }
        if let local = localOption { return local.displayLabel }
        if let locked = lockedEngine { return EngineStore.label(forSession: locked) }
        if warning != nil {
            let name = store.engines.first { $0.id == engineID }?.baseLabel ?? engineID
            return "\(name) unavailable"
        }
        return store.resolved(engineID).label
    }

    private var engineMenu: some View {
        Menu {
            ForEach(store.engines) { option in
                menuItem(option)
            }
        } label: {
            menuLabel
        }
        .frame(maxWidth: .infinity)
        .disabled(lockedEngine != nil)
        .accessibilityLabel("Engine")
        .accessibilityValue(lockedLabel ?? warning.map { "\($0). Pick another engine." } ?? localOption?.spokenLabel ?? currentLabel)
        .accessibilityHint(lockedEngine == nil ? "" : "Set by this session")
    }

    /// Local options get a spoken label (with the selected and down cues it replaces);
    /// every other option keeps the automatic one (title, subtitle, checkmark).
    @ViewBuilder
    private func menuItem(_ option: EngineOption) -> some View {
        if option.isLocalOption {
            let selected = option.id == engineID ? ", selected" : ""
            let down = option.available ? "" : ", unavailable" + (subtitle(for: option).map { ": \($0)" } ?? "")
            menuButton(option).accessibilityLabel(option.spokenLabel + selected + down)
        } else {
            menuButton(option)
        }
    }

    private func menuButton(_ option: EngineOption) -> some View {
        Button {
            engineID = option.id
        } label: {
            // A menu item shows its first Text as the title, the second as the subtitle.
            if option.id == engineID {
                Label(option.displayLabel, systemImage: "checkmark")
            } else if option.isLocalOption && option.available {
                Label(option.displayLabel, systemImage: "lock.shield")
            } else {
                Text(option.displayLabel)
            }
            if let subtitle = subtitle(for: option) {
                Text(subtitle)
            }
        }
        .disabled(!option.available)
    }

    private var menuLabel: some View {
        HStack(spacing: 6) {
            if localOption != nil {
                // local only: the shield says "nothing leaves your devices", locked or not
                Image(systemName: "lock.shield").font(.caption)
            } else if lockedEngine != nil {
                Image(systemName: "lock.fill").font(.caption)
            } else if warning != nil {
                Image(systemName: "exclamationmark.triangle.fill").font(.caption)
            }
            Text(currentLabel).lineLimit(labelLines)
            if lockedEngine == nil {
                Image(systemName: "chevron.down").font(.caption.weight(.semibold))
            }
        }
        .fixedSize(horizontal: keepsLabelWidth, vertical: false)
        .font(.subheadline)
        .foregroundColor(lockedEngine != nil ? Color.jarvisMuted : (warning != nil ? Color.jarvisDanger : Color.jarvisText))
        .padding(.horizontal, 16)
        .frame(maxWidth: .infinity, minHeight: 44)
        .background {
            // locked = a muted chip (fixed, not a control); otherwise the house chip
            RoundedRectangle(cornerRadius: jarvisRadius)
                .fill(lockedEngine != nil ? Color.jarvisSurface.opacity(0.5) : Color.jarvisSurface)
        }
        .contentShape(Rectangle())
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
            toggleLabel(on: newSession)
                .fixedSize(horizontal: keepsLabelWidth, vertical: false)
                .font(.subheadline)
                // on: accent text on a faint accent tint (AA on the dark background);
                // the solid accent stays reserved for the send button
                .foregroundColor(newSession ? .jarvisAccent : .jarvisText)
                .padding(.horizontal, 16)
                .frame(maxWidth: .infinity, minHeight: 44)
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
        .frame(maxWidth: .infinity)
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
