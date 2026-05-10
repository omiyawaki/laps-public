import SwiftUI

extension View {
    func lapsGlassCard(cornerRadius: CGFloat = 8) -> some View {
        glassEffect(
            .regular,
            in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        )
    }

    func lapsTintedGlassCard(_ color: Color, isProminent: Bool, cornerRadius: CGFloat = 8) -> some View {
        let tintOpacity = isProminent ? 0.28 : 0.12
        let strokeOpacity = isProminent ? 0.75 : 0.24
        return glassEffect(
            .regular.tint(color.opacity(tintOpacity)),
            in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        )
        .overlay {
            RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                .stroke(color.opacity(strokeOpacity), lineWidth: isProminent ? 1.5 : 1)
        }
    }
}
