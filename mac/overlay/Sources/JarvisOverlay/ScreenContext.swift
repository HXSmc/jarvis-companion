import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import ImageIO
import ScreenCaptureKit
import UniformTypeIdentifiers
import Vision

import OverlayCore

/// JSON keys match the HTTP API "screen" object verbatim.
struct ScreenPayload: Encodable {
    var scale: Double
    var app: String?
    var document: String?
    var image_jpeg_b64: String?
    var width_px: Int
    var height_px: Int
    var ax: [AXItem]
    var ocr: [OCRItem]
    var windows: [WindowItem]
}

/// A visible window, front to back. box = [x0, y0, x1, y1] in points, top-left.
struct WindowItem: Encodable {
    var app: String
    var title: String
    var box: [Double]
}

/// Points, top-left of the main display. box = [x0, y0, x1, y1].
struct AXItem: Encodable {
    var role: String
    var label: String
    var box: [Double]
}

/// Capture pixels, top-left. box = [x0, y0, x1, y1].
struct OCRItem: Encodable {
    var text: String
    var box: [Double]
}

enum ScreenContext {
    /// Roles always collected. AXSegmentedControl/AXToolbar children and titled
    /// AXCells are picked up by the keep-rule below while walking into them.
    private static let pointableRoles: Set<String> = [
        "AXButton", "AXMenuBarItem", "AXMenuItem", "AXCheckBox", "AXRadioButton",
        "AXPopUpButton", "AXMenuButton", "AXTextField", "AXLink", "AXTab",
    ]

    private static let nodeAttributes: [String] = [
        kAXRoleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute,
        kAXValueAttribute, kAXPositionAttribute, kAXSizeAttribute, kAXChildrenAttribute,
    ]

    // MARK: capture

    static func capture() async -> ScreenPayload? {
        guard CGPreflightScreenCaptureAccess() else { return nil }
        guard let shot = try? await captureMainDisplay() else { return nil }
        let front = await MainActor.run { NSWorkspace.shared.frontmostApplication }
        var items: [AXItem] = []
        var document: String?
        if let pid = front?.processIdentifier {
            let walk = walkFrontApp(pid: pid)
            items = walk.items.map {
                AXItem(role: $0.role, label: $0.label,
                       box: [$0.frame.minX, $0.frame.minY, $0.frame.maxX, $0.frame.maxY])
            }
            document = walk.document
        }
        let ocr = ocrText(in: shot.image).prefix(300).map { pair in
            OCRItem(text: String(pair.text.prefix(120)), box: pair.box)
        }
        return ScreenPayload(
            scale: shot.scale,
            app: front?.localizedName,
            document: document,
            image_jpeg_b64: jpegBase64(shot.image),
            width_px: shot.image.width,
            height_px: shot.image.height,
            ax: items,
            ocr: Array(ocr),
            windows: visibleWindows()
        )
    }

    /// Normal on-screen windows of other apps, front to back (titles need Screen Recording,
    /// which capture() already checked). "highlight the last debugging session" -> the
    /// Safari window titled "Jarvis voice debug".
    static func visibleWindows() -> [WindowItem] {
        let own = ProcessInfo.processInfo.processIdentifier
        let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                              kCGNullWindowID) as? [[String: Any]] ?? []
        var out: [WindowItem] = []
        for info in list {
            guard (info[kCGWindowLayer as String] as? Int) == 0,
                  (info[kCGWindowOwnerPID as String] as? Int32) != own,
                  let bounds = info[kCGWindowBounds as String] as? NSDictionary,
                  let rect = CGRect(dictionaryRepresentation: bounds as CFDictionary),
                  rect.width >= 80, rect.height >= 60
            else { continue }
            let app = info[kCGWindowOwnerName as String] as? String ?? ""
            let title = info[kCGWindowName as String] as? String ?? ""
            guard !app.isEmpty || !title.isEmpty else { continue }
            out.append(WindowItem(app: String(app.prefix(60)), title: String(title.prefix(120)),
                                  box: [rect.minX, rect.minY, rect.maxX, rect.maxY]))
            if out.count == 20 { break }
        }
        return out
    }

    /// Fresh AX frame of the same element, or nil when it vanished/moved >400 pt.
    static func recheck(role: String, label: String, near box: CGRect) -> CGRect? {
        guard let pid = frontmostPID() else { return nil }
        let centre = CGPoint(x: box.midX, y: box.midY)
        var best: (frame: CGRect, dist: CGFloat)?
        for item in walkFrontApp(pid: pid).items where item.role == role && item.label == label {
            let d = hypot(item.frame.midX - centre.x, item.frame.midY - centre.y)
            if d <= 400 && d < (best?.dist ?? .greatestFiniteMagnitude) { best = (item.frame, d) }
        }
        return best?.frame
    }

    /// Gutter OCR -> LineBandMapper; [] when no gutter found.
    static func lineBands(ranges: [ClosedRange<Int>]) async -> [CGRect] {
        guard !ranges.isEmpty, CGPreflightScreenCaptureAccess(),
              let shot = try? await captureMainDisplay(),
              let pid = frontmostPID(),
              let window = walkFrontApp(pid: pid).windowFrame
        else { return [] }
        // 1-5 digit observations inside the frontmost window, converted to points.
        var digits: [(line: Int, centre: CGPoint, right: CGFloat)] = []
        for pair in ocrText(in: shot.image) {
            guard (1...5).contains(pair.text.count), pair.text.allSatisfy(\.isNumber),
                  let line = Int(pair.text) else { continue }
            let centre = CGPoint(x: (pair.box[0] + pair.box[2]) / 2 / shot.scale,
                                 y: (pair.box[1] + pair.box[3]) / 2 / shot.scale)
            guard window.contains(centre) else { continue }
            digits.append((line, centre, pair.box[2] / shot.scale))
        }
        // Leftmost column of >=5 digit boxes with x-centres within 12 px.
        let sorted = digits.sorted { $0.centre.x < $1.centre.x }
        var gutter: [(line: Int, midY: CGFloat, right: CGFloat)] = []
        var start = 0
        while start < sorted.count {
            var end = start
            while end + 1 < sorted.count, sorted[end + 1].centre.x - sorted[start].centre.x <= 12 {
                end += 1
            }
            if end - start + 1 >= 5 {
                gutter = sorted[start...end].map { ($0.line, $0.centre.y, $0.right) }
                break
            }
            start = end + 1
        }
        guard let mapper = LineBandMapper(marks: gutter.map { (line: $0.line, midY: $0.midY) })
        else { return [] }
        let x = (gutter.map(\.right).max() ?? 0) + 4
        let width = window.maxX - 8 - x
        guard width > 0 else { return [] }
        return ranges.map { mapper.band(for: $0, x: x, width: width) }
    }

    // MARK: screen shot
    // Adapted from milind-soni/tiptour-macos (MIT), see THIRD_PARTY_NOTICES.txt.

    private enum CaptureError: Error { case noDisplay }

    private struct Shot {
        let image: CGImage
        /// Image pixels per screen point.
        let scale: Double
    }

    private static func captureMainDisplay() async throws -> Shot {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        let mainID = CGMainDisplayID()
        guard let display = content.displays.first(where: { $0.displayID == mainID }) ?? content.displays.first
        else { throw CaptureError.noDisplay }
        let backing = await MainActor.run { () -> Double in
            for screen in NSScreen.screens {
                if let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber,
                   number.uint32Value == mainID {
                    return Double(screen.backingScaleFactor)
                }
            }
            return 1
        }
        // Own windows excluded by PID so the orb/card never occlude the capture.
        let ownWindows = content.windows.filter {
            $0.owningApplication?.processID == ProcessInfo.processInfo.processIdentifier
        }
        let filter = SCContentFilter(display: display, excludingWindows: ownWindows)
        let config = SCStreamConfiguration()
        config.width = Int(Double(display.width) * backing)
        config.height = Int(Double(display.height) * backing)
        config.showsCursor = false
        let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
        return Shot(image: image, scale: Double(image.width) / Double(display.width))
    }

    private static func jpegBase64(_ image: CGImage) -> String? {
        var output = image
        let longEdge = max(image.width, image.height)
        if longEdge > 1600 {
            let ratio = CGFloat(1600) / CGFloat(longEdge)
            let w = max(1, Int(CGFloat(image.width) * ratio))
            let h = max(1, Int(CGFloat(image.height) * ratio))
            if let ctx = CGContext(
                data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue
            ) {
                ctx.interpolationQuality = .medium
                ctx.draw(image, in: CGRect(x: 0, y: 0, width: CGFloat(w), height: CGFloat(h)))
                if let scaled = ctx.makeImage() { output = scaled }
            }
        }
        let data = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil)
        else { return nil }
        CGImageDestinationAddImage(dest, output, [kCGImageDestinationLossyCompressionQuality: 0.6] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return data.base64EncodedString()
    }

    /// Text observations as top-left pixel boxes [x0, y0, x1, y1].
    private static func ocrText(in image: CGImage) -> [(text: String, box: [Double])] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.minimumTextHeight = 0.008
        let handler = VNImageRequestHandler(cgImage: image)
        try? handler.perform([request])
        let w = Double(image.width), h = Double(image.height)
        var out: [(String, [Double])] = []
        for observation in request.results ?? [] {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let bb = observation.boundingBox
            let x0 = bb.minX * w, y0 = (1 - bb.maxY) * h
            out.append((text, [x0, y0, x0 + bb.width * w, y0 + bb.height * h]))
        }
        return out
    }

    // MARK: accessibility walk
    // Adapted from milind-soni/tiptour-macos (MIT), see THIRD_PARTY_NOTICES.txt.

    private struct Candidate {
        let role: String
        let label: String
        let frame: CGRect
    }

    private struct WalkResult {
        var items: [Candidate]
        var document: String?
        var windowFrame: CGRect?
    }

    private struct NodeInfo {
        var role = ""
        var title = ""
        var description = ""
        var help = ""
        var valueString = ""
        var frame: CGRect?
        var children: [AXUIElement] = []
    }

    private static func walkFrontApp(pid: pid_t) -> WalkResult {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.25)
        var queue: [(element: AXUIElement, depth: Int, parentRole: String, expand: Bool)] = []
        var document: String?
        var windowFrame: CGRect?
        if let window = axAttribute(app, kAXFocusedWindowAttribute) {
            windowFrame = readNode(window)?.frame
            if let doc = axString(window, kAXDocumentAttribute), let url = URL(string: doc), !url.path.isEmpty {
                document = url.path
            }
            queue.append((element: window, depth: 0, parentRole: "", expand: true))
        } else {
            queue.append((element: app, depth: 0, parentRole: "", expand: true))
        }
        if let bar = readNode(axAttribute(app, kAXMenuBarAttribute)) {
            for child in bar.children {
                queue.append((element: child, depth: 1, parentRole: bar.role, expand: false))
            }
        }

        var items: [Candidate] = []
        let deadline = Date().addingTimeInterval(0.3)
        var index = 0
        while index < queue.count, items.count < 400, Date() < deadline {
            let entry = queue[index]
            index += 1
            guard entry.depth <= 20, let node = readNode(entry.element) else { continue }
            let label = !node.title.isEmpty ? node.title
                : !node.description.isEmpty ? node.description
                : !node.help.isEmpty ? node.help
                : node.valueString
            let keep = pointableRoles.contains(node.role)
                || (node.role == "AXCell" && !node.title.isEmpty)
                || (node.role == "AXStaticText" && !node.title.isEmpty && entry.parentRole == "AXToolbar")
            if keep, !label.isEmpty, let frame = node.frame, frame.width > 0, frame.height > 0 {
                items.append(Candidate(role: node.role, label: label, frame: frame))
            }
            if entry.expand {
                for child in node.children {
                    queue.append((element: child, depth: entry.depth + 1, parentRole: node.role, expand: true))
                }
            }
        }
        return WalkResult(items: items, document: document, windowFrame: windowFrame)
    }

    /// One IPC for role/title/description/help/value/position/size/children.
    private static func readNode(_ element: AXUIElement?) -> NodeInfo? {
        guard let element else { return nil }
        var values: CFArray?
        let status = AXUIElementCopyMultipleAttributeValues(
            element, nodeAttributes as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &values
        )
        guard status == .success, let raw = values as [AnyObject]? else { return nil }
        guard raw.count == nodeAttributes.count else { return nil }
        var node = NodeInfo()
        node.role = raw[0] as? String ?? ""
        node.title = raw[1] as? String ?? ""
        node.description = raw[2] as? String ?? ""
        node.help = raw[3] as? String ?? ""
        node.valueString = raw[4] as? String ?? ""
        node.children = raw[7] as? [AXUIElement] ?? []
        let position = raw[5], size = raw[6]
        if CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() {
            var point = CGPoint.zero, dimensions = CGSize.zero
            if AXValueGetValue(position as! AXValue, .cgPoint, &point),
               AXValueGetValue(size as! AXValue, .cgSize, &dimensions),
               dimensions.width > 0, dimensions.height > 0 {
                node.frame = CGRect(origin: point, size: dimensions)
            }
        }
        return node
    }

    private static func axAttribute(_ element: AXUIElement, _ attribute: String) -> AXUIElement? {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
              let value, CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        return (value as! AXUIElement)
    }

    private static func axString(_ element: AXUIElement, _ attribute: String) -> String? {
        var value: AnyObject?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success
        else { return nil }
        return value as? String
    }

    /// Frontmost app without AppKit, so recheck/lineBands stay off the main actor.
    private static func frontmostPID() -> pid_t? {
        let system = AXUIElementCreateSystemWide()
        guard let app = axAttribute(system, kAXFocusedApplicationAttribute) else { return nil }
        var pid: pid_t = 0
        let error = AXUIElementGetPid(app, &pid)
        guard error == .success, pid != 0 else { return nil }
        return pid
    }
}
