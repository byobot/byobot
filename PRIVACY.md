# Privacy

byobot has no server. Its maintainers receive none of your data and collect no analytics.

## Where your data lives

- Your logbook lives on your own devices: a database on your computer (`~/.byobot`) and the iPhone app's own storage.
- The phone and your computer sync directly over your local network.
- If you turn on Drive sync, your logbook is stored encrypted in a hidden app folder in your own Google Drive. byobot asks only for the `drive.appdata` scope, so it can't see your other Drive files, and it signs in through a Google Cloud project you control. It is encrypted on your devices before it leaves them, with keys Google can't read.

## Other services

- Connectors talk directly between your device and the service: WHOOP through WHOOP's API with your own developer app, and Apple Health on your iPhone.
- An agent you connect reads and writes what you let it, under its provider's own terms.

## Deleting your data

- One record: `byobot erase <kind> <id>`.
- Everything on a computer: delete the `~/.byobot` folder. On the iPhone: delete the app.
- Drive sync: in Google Drive's settings, under Manage apps, delete byobot's hidden app data and disconnect it.
