import AVFoundation
import CoreImage
import ImageIO
import SwiftUI
import UIKit
import Vision

/// Back camera for Hardware Mode. Keeps the newest frame (portrait-oriented) so
/// the session can grab a JPEG at any moment, and hands every frame to the
/// tracker on the capture queue.
final class HWCamera: NSObject, ObservableObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    @Published var authorized = true
    /// Pixel size of the frames (portrait), used to map image coords onto the screen.
    @Published var frameSize = CGSize(width: 720, height: 1280)

    let queue = DispatchQueue(label: "jarvis.hwmode.camera")
    var onPixelBuffer: ((CVPixelBuffer) -> Void)?

    private let output = AVCaptureVideoDataOutput()
    private let ciContext = CIContext()
    private let lock = NSLock()
    private var latest: CVPixelBuffer?
    private var lastSignature: [UInt8]?

    func start() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            configureAndRun()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { ok in
                DispatchQueue.main.async {
                    self.authorized = ok
                    if ok { self.configureAndRun() }
                }
            }
        default:
            authorized = false
        }
    }

    func stop() {
        queue.async { if self.session.isRunning { self.session.stopRunning() } }
    }

    /// Torch for dark PC cases.
    func setTorch(_ on: Bool) {
        guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              device.hasTorch, (try? device.lockForConfiguration()) != nil else { return }
        if on { try? device.setTorchModeOn(level: 0.4) } else { device.torchMode = .off }
        device.unlockForConfiguration()
    }

    private func configureAndRun() {
        queue.async {
            if !self.session.inputs.isEmpty {
                if !self.session.isRunning { self.session.startRunning() }
                return
            }
            self.session.beginConfiguration()
            self.session.sessionPreset = .hd1280x720
            guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
                  let input = try? AVCaptureDeviceInput(device: device), self.session.canAddInput(input) else {
                self.session.commitConfiguration()
                return
            }
            self.session.addInput(input)
            self.output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
            self.output.alwaysDiscardsLateVideoFrames = true
            self.output.setSampleBufferDelegate(self, queue: self.queue)
            if self.session.canAddOutput(self.output) { self.session.addOutput(self.output) }
            if let connection = self.output.connection(with: .video), connection.isVideoOrientationSupported {
                connection.videoOrientation = .portrait
            }
            self.session.commitConfiguration()
            if (try? device.lockForConfiguration()) != nil {
                if device.isFocusModeSupported(.continuousAutoFocus) { device.focusMode = .continuousAutoFocus }
                device.unlockForConfiguration()
            }
            self.session.startRunning()
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lock.lock()
        let firstOrResized = latest.map { CVPixelBufferGetWidth($0) != CVPixelBufferGetWidth(buffer) } ?? true
        latest = buffer
        lock.unlock()
        if firstOrResized {
            let size = CGSize(width: CVPixelBufferGetWidth(buffer), height: CVPixelBufferGetHeight(buffer))
            DispatchQueue.main.async { self.frameSize = size }
        }
        onPixelBuffer?(buffer)
    }

    /// Newest frame as JPEG (long edge ≤ maxSide). With `onlyIfChanged`, returns
    /// nil when the view barely changed since the last frame sent this way.
    func jpeg(maxSide: CGFloat = 1024, onlyIfChanged: Bool = false) -> Data? {
        lock.lock()
        let buffer = latest
        lock.unlock()
        guard let buffer else { return nil }
        var image = CIImage(cvPixelBuffer: buffer)
        let scale = min(1, maxSide / max(image.extent.width, image.extent.height))
        image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        if onlyIfChanged {
            let signature = signature(of: image)
            if let last = lastSignature, Self.difference(last, signature) < 6 { return nil }
            lastSignature = signature
        }
        let options = [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.7]
        return ciContext.jpegRepresentation(of: image, colorSpace: CGColorSpaceCreateDeviceRGB(), options: options)
    }

    /// 16×16 brightness thumbnail: a cheap "did the picture change" fingerprint.
    private func signature(of image: CIImage) -> [UInt8] {
        let side: CGFloat = 16
        let s = CGAffineTransform(scaleX: side / image.extent.width, y: side / image.extent.height)
        var pixels = [UInt8](repeating: 0, count: 16 * 16 * 4)
        ciContext.render(image.transformed(by: s), toBitmap: &pixels, rowBytes: 16 * 4,
                         bounds: CGRect(x: 0, y: 0, width: side, height: side),
                         format: .RGBA8, colorSpace: CGColorSpaceCreateDeviceRGB())
        return stride(from: 0, to: pixels.count, by: 4).map { pixels[$0 + 1] }
    }

    static func difference(_ a: [UInt8], _ b: [UInt8]) -> Int {
        guard a.count == b.count, !a.isEmpty else { return 255 }
        return zip(a, b).reduce(0) { $0 + abs(Int($1.0) - Int($1.1)) } / a.count
    }
}

struct HWCameraPreview: UIViewRepresentable {
    let session: AVCaptureSession

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspectFill
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {}

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
}

/// Keeps a highlight stuck to the part as the phone moves (Vision 2D tracking).
/// Tiny parts are tracked through a larger region around them (trackers need
/// texture), then shrunk back for drawing. Runs on the camera queue.
final class HWTracker {
    /// Normalized top-left rect of the part, or nil when tracking is lost. Main thread.
    var onUpdate: ((CGRect?) -> Void)?

    private let queue: DispatchQueue
    private var handler = VNSequenceRequestHandler()
    private var request: VNTrackObjectRequest?
    private var shrink = CGSize(width: 1, height: 1)
    private var frameCount = 0
    private static let minSide: CGFloat = 0.12

    init(queue: DispatchQueue) { self.queue = queue }

    /// Starts tracking `rect`, first on `frame` (the picture the brain answered about,
    /// already scaled to the camera's frame size), then on the live frames. A fresh
    /// sequence handler per part: one sequence must only see same-size frames.
    func seed(_ rect: CGRect, on frame: CIImage? = nil) {
        let tracked = CGRect(x: rect.midX - max(rect.width, Self.minSide) / 2,
                             y: rect.midY - max(rect.height, Self.minSide) / 2,
                             width: max(rect.width, Self.minSide), height: max(rect.height, Self.minSide))
            .intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
        let ratio = CGSize(width: rect.width / tracked.width, height: rect.height / tracked.height)
        queue.async {
            // Vision's normalized coordinates start at the bottom-left.
            let vision = CGRect(x: tracked.minX, y: 1 - tracked.maxY, width: tracked.width, height: tracked.height)
            let request = VNTrackObjectRequest(detectedObjectObservation: VNDetectedObjectObservation(boundingBox: vision))
            request.trackingLevel = .accurate
            self.shrink = ratio
            self.handler = VNSequenceRequestHandler()
            if let frame {
                try? self.handler.perform([request], on: frame, orientation: .up)
                if let first = request.results?.first as? VNDetectedObjectObservation {
                    request.inputObservation = first
                }
            }
            self.request = request
        }
    }

    func clear() { queue.async { self.request = nil } }

    func process(_ buffer: CVPixelBuffer) {
        guard let request else { return }
        frameCount += 1
        guard frameCount % 2 == 0 else { return }  // ~15 fps is plenty for a highlight
        do {
            try handler.perform([request], on: buffer, orientation: .up)
            guard let observation = request.results?.first as? VNDetectedObjectObservation else { return }
            request.inputObservation = observation
            let lost = observation.confidence < 0.3
            if lost { self.request = nil }
            let b = observation.boundingBox
            let w = b.width * shrink.width, h = b.height * shrink.height
            let rect = CGRect(x: b.midX - w / 2, y: (1 - b.midY) - h / 2, width: w, height: h)
            DispatchQueue.main.async { self.onUpdate?(lost ? nil : rect) }
        } catch {
            self.request = nil
            DispatchQueue.main.async { self.onUpdate?(nil) }
        }
    }
}
