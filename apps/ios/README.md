# byobot for iPhone

The SwiftUI app. It pulls the plan from `byobot serve` on your laptop, logs workouts offline, and pushes them back on the same network. It reads body measurements, heart, activity, mobility, vitals, sleep and workouts from Apple Health, but not nutrition or cycle tracking. Its data stays on the phone, excluded from backups, and the laptop is the source of truth.

Logic lives in the `ByobotKit` Swift package; `Byobot/` is the UI. `ByobotKit/Sources/ByobotKit/Generated/Contract.swift` is generated from `schema/v1`, so don't edit it.

## Build and test

On a Mac with Xcode (the app targets iOS 17):

```bash
cd apps/ios/ByobotKit && swift test   # runs against schema/fixtures/v1
cd .. && brew install xcodegen && xcodegen generate && open Byobot.xcodeproj
```

To run on a device, set your own team and bundle id under Signing & Capabilities. HealthKit needs an Apple developer account (free or paid).

The app keeps its data in its own container, as free Personal Team signing has no App Groups. To share it with a widget or watch app, set `BYOBOT_APP_GROUP` in `project.yml` as its comment says (paid team only), before the first run: data from a build without it stays behind.

## Pair

Run `byobot serve` on the laptop and scan its QR code with the iPhone's Camera app, or enter the address, port and code by hand and check the fingerprint matches.
