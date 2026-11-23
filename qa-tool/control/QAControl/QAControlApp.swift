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
            case "tabs": Tabs()
            case "motion": MotionPlant()
            case "underbars": UnderBars()
            case "scrollclip": ScrollClip()
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
            Text("$29.99 per year").font(.footnote)
            ZStack {
                Text("BEST VALUE PLAN").font(.headline)
                Text("MOST POPULAR PICK").font(.headline).offset(x: 12, y: 4)
            }
            HStack(alignment: .top) {
                Text("1,234,567").font(.system(size: 40, weight: .bold)).fixedSize()
                    .frame(width: 110, alignment: .leading).clipped()
                Text("Custom").font(.system(size: 36, weight: .bold)).frame(width: 78)
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

/// The journey helpers' control: two tabs, and a Settings list whose name field
/// (already holding a name) and Save button start below the fold. On the iPad the
/// tabs are the floating bar or the sidebar, which `tabBars` does not see.
struct Tabs: View {
    @State private var saved = ""

    var body: some View {
        if #available(iOS 18.0, *) {
            TabView {
                Tab("Today", systemImage: "sun.max") { today }
                Tab("Settings", systemImage: "gear") { settings }
            }
            .tabViewStyle(.sidebarAdaptable)
        } else {
            TabView {
                today.tabItem { Label("Today", systemImage: "sun.max") }
                settings.tabItem { Label("Settings", systemImage: "gear") }
            }
        }
    }

    private var today: some View {
        NavigationStack {
            Text(saved.isEmpty ? "Nothing saved" : "Saved \(saved)").navigationTitle("Today")
        }
    }

    private var settings: some View {
        NavigationStack {
            SettingsList(saved: $saved).navigationTitle("Settings")
        }
    }
}

struct SettingsList: View {
    @Binding var saved: String
    @State private var name = ""

    var body: some View {
        List {
            ForEach(1...40, id: \.self) { Text("Setting \($0)") }
            // Found by its prompt alone, which stops matching once typing starts (Quit Zyn's note).
            TextField("", text: $name, prompt: Text("Your name"))
            Button("Save") { saved = name }
        }
    }
}

/// The overlap oracle's control for what lies under pinned things. Rows scroll under
/// the tab bar and under an opaque Continue bar that cuts row 4's second line, as
/// fleet lists do under their bars: not an overlap. A pinned label with no backing
/// drawn over the first row shows the row through it: an overlap.
struct UnderBars: View {
    private let rows = (1...30).map { "Row \($0): every sunrise score for the week ahead" }
    /// Rows sit 60 pt apart from the top of the list and draw about 49 pt of text, so the
    /// bar's top edge at 270 pt crosses row 4 (240 to 289) below its first line and middle.
    private let pitch: CGFloat = 60

    var body: some View {
        TabView {
            list.tabItem { Label("Home", systemImage: "house") }
            Text("Second tab").tabItem { Label("More", systemImage: "ellipsis") }
        }
    }

    private var list: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Button {} label: { Text("MOST POPULAR PICK").font(.headline) }
                    .buttonStyle(.plain)
                    .frame(maxWidth: .infinity, minHeight: pitch, maxHeight: pitch, alignment: .topLeading)
                ForEach(rows, id: \.self) { row in
                    Button {} label: {
                        Text(row).font(.title3).lineLimit(2)
                            .frame(maxWidth: .infinity, minHeight: pitch, maxHeight: pitch, alignment: .topLeading)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 24)
        }
        .overlay(alignment: .topLeading) {
            Button {} label: { Text("BEST VALUE PLAN").font(.headline) }
                .buttonStyle(.plain)
                // Over most of the row's label but clear of its middle, so a tap still reaches both: drawn
                // over, not hidden. At 134 pt the phone's smaller overlap read cleanly half the time.
                .offset(x: 118, y: 4)
        }
        .overlay(alignment: .top) {
            Button {} label: {
                Text("Continue").font(.headline).foregroundStyle(.white)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color.blue)
            }
            .buttonStyle(.plain)
            .frame(height: 2 * pitch)
            .offset(y: 4 * pitch + 30)
        }
    }
}

/// The offscreen oracle inside scroll views: a rack centred and 30% wider than the screen in a scroll view that
/// scrolls vertically (cut on both sides, never reachable), a carousel that ends partway through a card (fine),
/// and a short horizontal strip only 10% wider than the screen (cannot be told from a clipping scroll view).
struct ScrollClip: View {
    var body: some View {
        GeometryReader { proxy in
            let width = proxy.size.width
            ScrollView {
                VStack(spacing: 24) {
                    Text("Your rack").font(.title.bold())
                    HStack(spacing: 6) {
                        ForEach(0..<Int(width * 1.3 / 50) + 1, id: \.self) { index in
                            RoundedRectangle(cornerRadius: 6)
                                .fill(Color.green.opacity(0.3))
                                .frame(width: 44, height: 58)
                                .accessibilityElement()
                                .accessibilityLabel("Tile \(index + 1)")
                                .accessibilityAddTraits(.isImage)
                        }
                    }
                    .fixedSize()
                    .frame(width: width - 32)
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 12) {
                            ForEach(1...12, id: \.self) { index in
                                Text("Card \(index)")
                                    .frame(width: 150, height: 90)
                                    .background(Color.blue.opacity(0.15), in: RoundedRectangle(cornerRadius: 12))
                            }
                        }
                        .padding(.horizontal, 16)
                    }
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 0) {
                            ForEach(1...2, id: \.self) { index in
                                Text("Strip \(index)").frame(width: width * 0.55, height: 44)
                            }
                        }
                    }
                    ForEach(1...30, id: \.self) { Text("Line \($0)") }
                }
            }
        }
    }
}
