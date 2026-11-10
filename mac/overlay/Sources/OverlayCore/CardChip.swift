import SwiftUI

/// A parked card: one line, caps kind label then the title, on the card surface.
@MainActor
public struct CardChip: View {
    public let card: HUDCard

    public init(card: HUDCard) {
        self.card = card
    }

    public var body: some View {
        HStack(spacing: 8) {
            Text(card.typeTag)
                .font(Theme.fontMicro)
                .foregroundStyle(Theme.accent)
            Text(card.title)
                .font(Theme.fontSmall)
                .foregroundStyle(.white)
                .lineLimit(1)
                .truncationMode(.tail)
        }
        .padding(.horizontal, 10)
        .frame(width: ScatterLayout.chipSize.width, height: ScatterLayout.chipSize.height, alignment: .leading)
        .background(Theme.cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusCard))
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusCard).strokeBorder(Theme.cardBorder, lineWidth: 1))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(card.typeTag.capitalized) card: \(card.title), parked. Click to restore all cards.")
    }
}

extension HUDCard {
    /// The card's own id (the service always sets one); type + title when a card has none.
    public var listKey: String { id ?? "\(typeName):\(title)" }

    public var title: String {
        switch self {
        case .info(let p): return p.title
        case .stat(let p): return p.title
        case .bar(let p): return p.title
        case .flow(let p): return p.title
        case .link(let p): return p.title
        case .image(let p): return p.title
        }
    }
}
