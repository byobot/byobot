// swift-tools-version:5.9
// The byobot app's logic, kept apart from the UI so it builds and tests with `swift test`:
// the contract types (generated from schema/v1), pairing, sync with `byobot serve`, the
// outbox, and the events the app writes when you log a session.
import PackageDescription

let package = Package(
    name: "ByobotKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "ByobotKit", targets: ["ByobotKit"]),
    ],
    targets: [
        .target(name: "ByobotKit"),
        .testTarget(name: "ByobotKitTests", dependencies: ["ByobotKit"], exclude: ["plan-window.json"]),
    ]
)
