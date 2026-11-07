import SwiftUI

/// FleetQA's positive and negative control. `QA_CONTROL_SCREEN=broken` plants
/// one defect per oracle; `clean` is the same flow done right; `blank` draws
/// nothing. The release gate is only trusted while the broken flow fails every
/// oracle and the clean one passes.
@main
struct QAControlApp: App {
    var body: some Scene {
        WindowGroup {
            switch ProcessInfo.processInfo.environment["QA_CONTROL_SCREEN"] {
            case "broken": Flow(broken: true)
            case "blank": Color.white.ignoresSafeArea()
            default: Flow(broken: false)
            }
        }
    }
}

/// A welcome page, then the paywall, sharing one bottom button.
struct Flow: View {
    let broken: Bool
    @State private var page = 0

    var body: some View {
        if page == 0 {
            VStack(alignment: .leading, spacing: 16) {
                Text("Welcome").font(.largeTitle.bold())
                Text("Track the day in one glance").font(.body)
                if !broken { CardStack() }
                Spacer()
                Footer(legal: false) { page = 1 }
            }
            .padding(24)
        } else if broken {
            BrokenScreen()
        } else {
            CleanScreen()
        }
    }
}

/// The call to action with the legal row under it. The row keeps its space on
/// every page, hidden where it is not needed, so the button never moves.
struct Footer: View {
    let legal: Bool
    let action: () -> Void

    var body: some View {
        VStack(spacing: 8) {
            Button(action: action) {
                Text("Continue").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            HStack(spacing: 16) {
                Button("Terms of Use") {}
                Button("Privacy Policy") {}
                Button("Restore") {}
            }
            .font(.footnote)
            // System blue on white is 3.5:1, under WCAG's 4.5:1 for small text.
            .tint(Color(red: 0, green: 0.38, blue: 0.8))
            .opacity(legal ? 1 : 0)
            .disabled(!legal)
            .accessibilityHidden(!legal)
        }
    }
}

/// A deck with the same lines listed above it, like a rundown beside a card
/// stack. The cards behind the front one are covered, so their lines overlap
/// only in the accessibility tree; the listed copies are the ones a tap reaches.
struct CardStack: View {
    private let behind = ["The team lost the game and the fans booed loudly",
                          "The coach left the team and the fans cheered"]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Button {} label: {
                VStack(alignment: .leading) { ForEach(behind, id: \.self) { Text($0) } }
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .combine)
            ZStack(alignment: .topLeading) {
                ForEach(Array(behind.enumerated()), id: \.offset) { slot, line in
                    Text(line).padding(12).offset(y: CGFloat(slot + 1) * 8).allowsHitTesting(false)
                }
                // The front card is one tappable element over the whole card.
                Button {} label: {
                    Text("The team won the game and the fans went home happy")
                        .padding(12)
                        .frame(maxWidth: .infinity, minHeight: 90, alignment: .topLeading)
                        .background(Color(.systemBackground))
                        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.secondary))
                }
                .buttonStyle(.plain)
            }
        }
    }
}

struct CleanScreen: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Choose a plan").font(.largeTitle.bold())
            Text("Yearly, $29.99 per year").font(.title2.bold())
            Text("Just $2.49/mo, billed yearly").font(.footnote)
            Text("Monthly, then $4.99 per month").font(.body)
            Spacer()
            Footer(legal: true) {}
        }
        .padding(24)
    }
}

struct BrokenScreen: View {
    @State private var lateCard = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if lateCard {
                // Arrives after the screen looks settled and pushes everything down.
                Text("Your goal has been out of reach for two weeks, set a new one")
                    .font(.title3)
                    .frame(maxWidth: .infinity, minHeight: 160, alignment: .topLeading)
                    .background(Color.orange.opacity(0.2))
            }
            Text("Choose a plan").font(.largeTitle.bold())
            Text("Yearly plan with every feature, then $29.99 per year, billed once")
                .font(.title3)
                .lineLimit(1)
                .frame(width: 180, alignment: .leading)
            // The monthly figure shouts while the billed yearly price whispers.
            Text("$2.49/mo").font(.system(size: 40, weight: .bold))
            Text("$29.99 per year").font(.caption)
            ZStack {
                Text("BEST VALUE PLAN").font(.headline)
                Text("MOST POPULAR PICK").font(.headline).offset(x: 12, y: 4)
            }
            HStack(alignment: .top) {
                Text("1,234,567").font(.system(size: 40, weight: .bold)).fixedSize()
                    .frame(width: 110, alignment: .leading).clipped()
                Text("Custom").font(.title2).frame(width: 34)
            }
            Text("Save 20% \u{2014} today only")
            Text("Burned NaN kcal")
            HStack {
                ProgressView()
                Text("Couldn't load plans, try again later")
            }
            Spacer()
            // No Terms, Privacy or Restore; pushed sideways and off the edge.
            Button {} label: {
                Text("Continue").frame(width: 300)
            }
            .buttonStyle(.borderedProminent)
            .offset(x: 220)
        }
        .padding(24)
        .task {
            try? await Task.sleep(for: .seconds(3))
            lateCard = true
        }
    }
}
