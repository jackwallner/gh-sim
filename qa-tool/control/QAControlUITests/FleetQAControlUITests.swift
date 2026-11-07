import XCTest

/// FleetQA checks itself: every oracle must fire on the broken flow, and no
/// blocking oracle may fire on the clean one. Run with
/// `qa-gate selftest` (or this scheme's tests) after any FleetQA change.
@MainActor
final class FleetQAControlUITests: XCTestCase {

    func testEveryOracleFiresOnTheBrokenScreen() {
        let app = XCUIApplication()
        QA.launch(app, environment: ["QA_CONTROL_SCREEN": "broken"])
        XCTAssertTrue(app.staticTexts["Welcome"].waitForExistence(timeout: 20))
        QA.checkpoint(app, "welcome")
        app.buttons["Continue"].tap()
        XCTAssertTrue(app.staticTexts["Choose a plan"].waitForExistence(timeout: 10))
        QA.checkpoint(app, "broken", grace: 0.3, paywall: true)
        let fired = Set(QA.lastFindings.map(\.oracle))
        // The 300 pt button overflows a phone and is its main button; an unfolded Duo fits it.
        let phone = app.windows.firstMatch.frame.width < 600 ? ["offscreen", "cta-jump"] : []
        for oracle in ["truncated", "overlap", "copy", "layout-shift", "error-state", "still-loading",
                       "paywall-terms", "paywall-privacy", "paywall-restore", "paywall-prominence"] + phone {
            XCTAssertTrue(fired.contains(oracle), "\(oracle) did not fire; fired: \(fired.sorted())")
        }
        let clipped = QA.lastFindings.filter { $0.oracle == "clipped" }.map(\.detail)
        XCTAssertTrue(clipped.contains { $0.contains("no ellipsis") }, "clipped number not caught: \(clipped)")
        XCTAssertTrue(clipped.contains { $0.contains("broken across") }, "word broken across lines not caught: \(clipped)")
        let copy = QA.lastFindings.filter { $0.oracle == "copy" }.map(\.detail)
        XCTAssertTrue(copy.contains { $0.contains("dash") }, "em dash not caught: \(copy)")
        XCTAssertTrue(copy.contains { $0.contains("failed to compute") }, "NaN not caught: \(copy)")
    }

    /// The pixel check that confirms the audit's contrast findings: it must fail
    /// pale text and pass plain text, or it would hide real failures.
    func testContrastMeasurement() {
        func ratio(_ ink: UIColor, on paper: UIColor) -> Double? {
            let size = CGSize(width: 240, height: 40)
            let image = UIGraphicsImageRenderer(size: size).image { context in
                paper.setFill()
                context.fill(CGRect(origin: .zero, size: size))
                ("Fine print nobody can read" as NSString).draw(at: CGPoint(x: 6, y: 10), withAttributes: [
                    .font: UIFont.preferredFont(forTextStyle: .footnote), .foregroundColor: ink])
            }
            guard let pixels = Idle.pixels(image) else { return nil }
            return Audit.contrast(in: CGRect(origin: .zero, size: size), pixels, scale: image.scale)
        }
        let pale = ratio(UIColor(white: 0.82, alpha: 1), on: .white) ?? 0
        let plain = ratio(.black, on: .white) ?? 0
        let link = ratio(UIColor(red: 0.04, green: 0.52, blue: 1, alpha: 1), on: .black) ?? 0
        XCTAssertLessThan(pale, 2, "pale gray on white measured \(pale)")
        XCTAssertGreaterThan(plain, 15, "black on white measured \(plain)")
        XCTAssertGreaterThan(link, 4.5, "dark-mode system blue on black measured \(link)")
    }

    func testBlankPaywallIsCaught() {
        let app = XCUIApplication()
        QA.launch(app, environment: ["QA_CONTROL_SCREEN": "blank"])
        sleep(2)
        QA.checkpoint(app, "blank", paywall: true)
        let fired = Set(QA.lastFindings.map(\.oracle))
        for oracle in ["blank-screen", "paywall-price"] {
            XCTAssertTrue(fired.contains(oracle), "\(oracle) did not fire; fired: \(fired.sorted())")
        }
    }

    func testNothingBlocksOnTheCleanScreen() {
        let app = XCUIApplication()
        QA.launch(app, environment: ["QA_CONTROL_SCREEN": "clean"])
        XCTAssertTrue(app.staticTexts["Welcome"].waitForExistence(timeout: 20))
        QA.checkpoint(app, "welcome")
        var blocking = QA.lastFindings.filter { $0.severity == "P0" || $0.severity == "P1" }
        XCTAssertTrue(blocking.isEmpty, "clean welcome blocked: \(blocking.map { "\($0.oracle): \($0.detail)" })")
        app.buttons["Continue"].tap()
        XCTAssertTrue(app.staticTexts["Choose a plan"].waitForExistence(timeout: 10))
        QA.checkpoint(app, "paywall")
        blocking = QA.lastFindings.filter { $0.severity == "P0" || $0.severity == "P1" }
        XCTAssertTrue(blocking.isEmpty, "clean paywall blocked: \(blocking.map { "\($0.oracle): \($0.detail)" })")
        XCTAssertFalse(QA.lastFindings.contains { $0.oracle.hasPrefix("paywall") }, "clean paywall flagged: \(QA.lastFindings)")
        let contrast = QA.lastFindings.filter { $0.oracle == "a11y-contrast" }
        XCTAssertTrue(contrast.isEmpty, "clean paywall has contrast findings: \(contrast.map(\.detail))")
    }
}
