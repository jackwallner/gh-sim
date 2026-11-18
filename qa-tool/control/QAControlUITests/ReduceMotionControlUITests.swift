import XCTest

/// Reduce Motion's control. `qa-gate selftest` records these with Reduce Motion off, then on with the plant
/// ignoring it and honoring it, and compares the motion as the gate does. The trimmed Reduce Motion persona runs
/// only test1; test2 adds motion that newcomer alone sees, so judging against newcomer's whole run would miss
/// the planted defect.
@MainActor
final class ReduceMotionControlUITests: XCTestCase {
    func test1MotionPlant() { watch() }

    func test2MoreMotion() { watch() }

    private func watch(journey: String = #function) {
        let app = XCUIApplication()
        QA.launch(app, environment: ["QA_CONTROL_SCREEN": "motion"], journey: journey)
        XCTAssertTrue(app.staticTexts["Motion"].waitForExistence(timeout: 20))
        sleep(8)
    }
}
