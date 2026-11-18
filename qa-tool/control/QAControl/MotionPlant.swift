import SwiftUI

/// Reduce Motion's control for `qa-gate selftest`: a bar that slides for as long as the screen is up.
/// `QA_CONTROL_MOTION=honors` holds it still under Reduce Motion; `ignores` slides it anyway, the defect
/// `reduce-motion-ignored` must catch.
struct MotionPlant: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var across = false
    private let honors = ProcessInfo.processInfo.environment["QA_CONTROL_MOTION"] != "ignores"

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            Text("Motion").font(.largeTitle.bold())
            GeometryReader { box in
                RoundedRectangle(cornerRadius: 24)
                    .fill(.blue)
                    .frame(width: box.size.width / 2, height: box.size.height)
                    .offset(x: across ? box.size.width / 2 : 0)
                    .animation(moves ? .easeInOut(duration: 1).repeatForever(autoreverses: true) : nil, value: across)
            }
            .frame(height: 320)
            Spacer()
        }
        .padding(24)
        .onAppear { if moves { across = true } }
    }

    private var moves: Bool { !(honors && reduceMotion) }
}
