# byobot for iPhone

The phone half of byobot: it pulls the week your agent planned from `byobot serve` on your laptop, lets you log each set, interval or stretch offline, and pushes what you logged back when it's on the same network. It also reads body mass from Apple Health (where a Hume scale, or any scale, writes its weigh-ins) and sends it to the laptop.

Nothing goes to a cloud service. The app's own data is kept on the phone, out of device backups and iCloud, and the laptop is the source of truth.

## Layout

```
ByobotKit/   a Swift package with the app's logic, tested with `swift test`
  Sources/ByobotKit/Generated/Contract.swift   written by tools/gen_swift.py from schema/v0; don't edit
Byobot/      the SwiftUI app
Support/     Info.plist and entitlements
project.yml  the Xcode project, generated with XcodeGen
```

## Build

On a Mac with Xcode 16 or newer:

```bash
brew install xcodegen
cd apps/ios
xcodegen generate
open Byobot.xcodeproj
```

To run on your own iPhone, set your team and a bundle id of your own under Signing & Capabilities. The app needs the HealthKit capability, which needs a (free or paid) Apple developer account.

## Test

```bash
cd apps/ios/ByobotKit
swift test
```

The tests decode and encode every valid fixture in `schema/fixtures/v0`, the same files the Python core tests against.

## Working out

The app opens to Today: the session you're in, or the next one planned, one tap from starting.

- **The set in front of you.** A big card with the targets, what you did last time, -/+ to nudge any number, and one tap for "Done as planned". Edit opens the full form.
- **Rest starts itself** from the plan's rest when you log a set; +30 s or skip. The phone buzzes when it's up.
- **Clocks for timed work.** An interval block (a Norwegian 4x4, an EMOM) or a hold runs on a clock of work and rest phases, with the heart-rate target shown. Each set logs as its work ends. A one-sided hold with no side planned runs left, then right.
- **Every exercise below.** Reorder them, or tap one to see its sets and swap it for another that works the same muscles, ranked by the equipment you have (Settings). Swaps and the new order go to your laptop with the sets, so your coach sees what changed and why.
- **Your gym.** The workout screen shows the gym you're at; tap it to change gym or add one. A session starts at the gym it was planned for, or else the last one you trained at. Loads compare only at the same gym: last time shows your sets there, and when you have none there it shows the latest elsewhere, marked with where. At a gym other than the planned one, "Done as planned" starts from your loads at this gym.
- **A summary** when you finish, and your past sessions under History.

The screen stays on while a session runs. Swaps come from the muscles on your activities, starting with the library that ships with byobot (see `library/README.md`).

## Pair and sync

1. On your laptop: `byobot serve`.
2. Scan the QR code it shows with the Camera app, then tap Pair. Or enter the address, port and code by hand and check that the fingerprint matches the one on your laptop's screen.
3. Pull down on the week to sync. The app also syncs each time it comes to the front.

What you log waits on the phone until the laptop accepts it. Events carry their own time, and the laptop applies them in time order, so logging offline and syncing later puts everything in its place.
