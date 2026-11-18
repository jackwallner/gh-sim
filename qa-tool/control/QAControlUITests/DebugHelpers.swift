import XCTest
@MainActor
final class DebugHelpers: XCTestCase {
    func bound(_ app: XCUIApplication) -> String { app.navigationBars.allElementsBoundByIndex.map(\.identifier).joined() }
    func testDebug() {
        let app = XCUIApplication()
        app.launchEnvironment["QA_CONTROL_SCREEN"] = "tabs"
        for round in 0..<3 {
            app.launch()
            QA.openTab(app, "Settings")
            let field = app.textFields["settings.name"]
            QA.reveal(field, in: app)
            field.tap()
            _ = app.keyboards.firstMatch.waitForExistence(timeout: 5)
            field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 6))
            field.typeText("QA Tester")
            field.coordinate(withNormalizedOffset: CGVector(dx: 0.99, dy: 0.5)).tap()
            sleep(1)
            print("DBG caret-tap round", round, "field=", QA.reads(field), "bound=", bound(app))
            app.terminate()
            app.launch()
            QA.openTab(app, "Settings")
            QA.type("QA Tester", into: app.textFields["settings.name"], in: app)
            sleep(1)
            print("DBG helper round", round, "field=", QA.reads(app.textFields["settings.name"]), "bound=", bound(app))
            app.terminate()
        }
    }
}
