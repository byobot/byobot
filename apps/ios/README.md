# byobot for iPhone

The SwiftUI app. It pulls the plan from `byobot serve` on your laptop, logs workouts offline, and pushes them back on the same network. It reads body mass from Apple Health. Its data stays on the phone, excluded from backups, and the laptop is the source of truth.

Logic lives in the `ByobotKit` Swift package; `Byobot/` is the UI. `ByobotKit/Sources/ByobotKit/Generated/Contract.swift` is generated from `schema/v0`, so don't edit it.

## Build and test

On a Mac with Xcode (the app targets iOS 17):

```bash
cd apps/ios/ByobotKit && swift test   # runs against schema/fixtures/v0
cd .. && brew install xcodegen && xcodegen generate && open Byobot.xcodeproj
```

To run on a device, set your own team and bundle id under Signing & Capabilities. HealthKit needs an Apple developer account (free or paid).

## Pair

Run `byobot serve` on the laptop and scan its QR code with the iPhone's Camera app, or enter the address, port and code by hand and check the fingerprint matches.
