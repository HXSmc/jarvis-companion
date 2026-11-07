import SwiftUI
import UIKit
import XCTest
@testable import JarvisCompanion

/// The engine picker row (EnginePickerRow.swift): every state drawn in a real
/// simulator window to PNG ($CARD_SHOTS_DIR, set by CI through
/// TEST_RUNNER_CARD_SHOTS_DIR) for the owner's before/after page. No network:
/// the shared store is seeded and the refresh is skipped.
@MainActor
final class EnginePickerSnapshotTests: XCTestCase {

    static let glm = EngineOption(id: "glm", label: "GLM", kind: "worker", available: true, isDefault: false, reason: nil)
    static let agy = EngineOption(id: "agy", label: "Antigravity", kind: "worker", available: true, isDefault: false, reason: nil)

    func testEngineRowStates() throws {
        EngineStore.shared.seed([.hub, Self.glm, Self.agy])
        try shot("11-engine-row-default",
                 EnginePickerRow(engineID: .constant("hub"), newSession: .constant(false), refreshOnAppear: false))
        try shot("12-engine-row-glm-on",
                 EnginePickerRow(engineID: .constant("glm"), newSession: .constant(true), refreshOnAppear: false))
        try shot("13-engine-row-locked",
                 EnginePickerRow(engineID: .constant("hub"), newSession: .constant(false),
                                 lockedEngine: "agy", refreshOnAppear: false))
        try shot("14-engine-row-ax5",
                 EnginePickerRow(engineID: .constant("hub"), newSession: .constant(true), refreshOnAppear: false)
                     .dynamicTypeSize(.accessibility5))
    }

    // MARK: drawing

    /// The row, sized to its content at phone width.
    private func shot<V: View>(_ name: String, _ view: V) throws {
        let content = view.padding(16).frame(width: 390).background(Color.jarvisBackground)
        let probe = UIHostingController(rootView: content)
        let height = probe.sizeThatFits(in: CGSize(width: 390, height: 2000)).height
        // + room for the window's top safe area, which pushes the content down.
        try draw(name, AnyView(content.frame(maxHeight: .infinity, alignment: .top).background(Color.jarvisBackground)),
                 size: CGSize(width: 390, height: max(80, ceil(height)) + 120))
    }

    private func outputDir() throws -> URL {
        let env = ProcessInfo.processInfo.environment["CARD_SHOTS_DIR"]
        let dir = env.map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.temporaryDirectory.appendingPathComponent("card-shots")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    private func draw(_ name: String, _ view: AnyView, size: CGSize) throws {
        let host = UIHostingController(rootView: view.preferredColorScheme(.dark))
        host.overrideUserInterfaceStyle = .dark
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        let window = scene.map { UIWindow(windowScene: $0) } ?? UIWindow()
        window.frame = CGRect(origin: .zero, size: size)
        window.rootViewController = host
        window.isHidden = false
        host.view.frame = window.bounds
        host.view.layoutIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.8))  // let SwiftUI lay out
        let format = UIGraphicsImageRendererFormat()
        format.scale = 3
        let image = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        window.isHidden = true
        let png = try XCTUnwrap(image.pngData())
        try png.write(to: outputDir().appendingPathComponent("\(name).png"))
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
