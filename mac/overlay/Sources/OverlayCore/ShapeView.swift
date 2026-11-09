import SwiftUI

/// Outline-shape annotation at its box position (screen points, top-left origin)
/// inside the surrounding canvas. Labels sit in a small dark capsule just above
/// the box, kept inside the canvas.
@MainActor
public struct ShapeView: View {
    public let shape: HUDShape

    public init(shape: HUDShape) {
        self.shape = shape
    }

    public var body: some View {
        GeometryReader { geo in
            let r = shape.rect
            ZStack(alignment: .topLeading) {
                switch shape.kind {
                case .rect:
                    stroked(RoundedRectangle(cornerRadius: Theme.radiusShape))
                        .frame(width: r.width, height: r.height)
                        .position(x: r.midX, y: r.midY)
                case .circle:
                    stroked(Ellipse())
                        .frame(width: r.width, height: r.height)
                        .position(x: r.midX, y: r.midY)
                case .triangle:
                    stroked(TriangleShape())
                        .frame(width: r.width, height: r.height)
                        .position(x: r.midX, y: r.midY)
                }
                if let label = shape.label, !label.isEmpty {
                    Text(label)
                        .font(Theme.microLabel)
                        .tracking(1.2)
                        .foregroundStyle(.white)
                        .lineLimit(1)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color.black.opacity(0.75), in: Capsule())
                        .overlay(Capsule().strokeBorder(Theme.accent, lineWidth: 1))
                        .position(x: min(max(r.midX, 60), geo.size.width - 60),
                                  y: max(14, r.minY - 14))
                }
            }
        }
    }

    /// Accent outline over a 1.5 pt-wider dark stroke, so it still reads on
    /// red or white UIs behind the overlay.
    private func stroked<S: Shape>(_ s: S) -> some View {
        ZStack {
            s.stroke(Color.black.opacity(0.7), style: StrokeStyle(lineWidth: 6))
            s.stroke(Theme.accent, style: StrokeStyle(lineWidth: 3))
        }
    }
}

/// Triangle pointing up, inscribed in the box.
private struct TriangleShape: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: rect.midX, y: rect.minY))
        p.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        p.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        p.closeSubpath()
        return p
    }
}
