# Privacy

byobot has no server. Its maintainers receive none of your data and collect no analytics.

## Where your data lives

- Your logbook lives on your own devices: a database on your computer (`~/.byobot`) and the iPhone app's own storage.
- The phone syncs with your computer over your local network, or through Drive sync. A phone that joined Drive sync can read the whole logbook there, so treat it like the computer.
- Profiles keep two people's logbooks apart, not private from each other: anyone, and any program, using the same macOS account can read every profile and its sign-ins. For privacy between people, give each their own macOS account.
- If you turn on Drive sync, your logbook is stored encrypted in a hidden app folder in your own Google Drive. byobot asks only for the `drive.appdata` scope, so it can't see your other Drive files, and it signs in through a Google Cloud project you control. It is encrypted on your devices before it leaves them, with keys Google can't read.

## Other services

- Connectors talk directly between your device and the service: WHOOP through WHOOP's API with your own developer app, and Apple Health on your iPhone.
- An agent you connect reads and writes what you let it, under its provider's own terms. With `byobot remote`, it reaches your computer through a tunnel you run and can do everything but erase, or only read if you give it read-only access when you approve it or make its key.
- Text that other apps and agents write, such as an app's or a device's name, reaches your agent. byobot keeps it to one short line and marks where it can appear, so the agent treats it as data. An agent can still be misled by what it reads.

## Deleting your data

- One record: `byobot erase <kind> <id>`.
- Everything on a computer: delete the `~/.byobot` folder. On the iPhone: delete the app.
- Drive sync: `byobot cloud off --delete yes` deletes the folder's files, all but a small signed note that holds no data and tells phones sync ended. Or, in Google Drive's settings under Manage apps, delete byobot's hidden app data and disconnect it.
