# push probe

Measures how often a silent push wakes an iPhone app and how fast, to decide whether a phone could answer an agent's requests. Throwaway: delete this folder when done.

Push needs a paid [Apple Developer Program](https://developer.apple.com/programs/) membership. A free Personal Team can't use the Push Notifications capability.

## Set up

1. In your developer account, under Certificates, Identifiers & Profiles > Keys, add a key with Apple Push Notifications service (APNs). Download the `.p8` and note its Key ID and your Team ID. Keep the key outside the repo.
2. Build the app:

   ```bash
   cd experiments/push-probe/ios && xcodegen generate && open PushProbe.xcodeproj
   ```

   Set your team and your own bundle id under Signing & Capabilities, run it on the phone once, then stop it in Xcode (the debugger keeps it from sleeping) and open it from the Home Screen. Copy the device token it shows.
3. On the Mac, from `experiments/push-probe`:

   ```bash
   export APNS_KEY_PATH=~/keys/AuthKey_XXXXXXXXXX.p8 APNS_KEY_ID=XXXXXXXXXX APNS_TEAM_ID=XXXXXXXXXX
   export APNS_TOPIC=your.bundle.id APNS_DEVICE_TOKEN=paste-from-the-app
   caffeinate -i python3 send.py
   ```

   It sends every 20 minutes to the sandbox, which is right for a build run from Xcode. Use `--env production` for a TestFlight build. Leave the Mac plugged in and online.

## Run for a few days

Use the phone normally; locked or unlocked is recorded for you. In the app, set the condition before you change it: low power before turning on Low Power Mode, swiped away before swiping the app away (opening it again sets it back to normal). Leave it overnight at least once.

## Read the results

Export the log from the app, AirDrop it to the Mac, then:

```bash
python3 analyze.py push-sends.jsonl push-probe.jsonl
```
