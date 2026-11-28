# Privacy

byobot has no server. Its maintainers receive none of your data and collect no analytics.

## Where your data lives

- Your logbook lives on your own devices: a database on your computer (`~/.byobot`) and the iPhone app's own storage.
- The phone syncs with your computer over your local network, nowhere else.
- Profiles keep two people's logbooks apart, not private from each other: anyone, and any program, using the same macOS account can read every profile and its sign-ins. For privacy between people, give each their own macOS account.

## Other services

- Connectors talk directly between your device and the service: WHOOP through WHOOP's API with your own developer app, and Apple Health on your iPhone.
- An agent you connect reads and writes what you let it, under its provider's own terms. With `byobot remote`, it reaches your computer through a tunnel you run and can do everything but erase, or only read if you give it read-only access when you approve it or make its key.
- Text that other apps and agents write, such as an app's or a device's name, reaches your agent. byobot keeps it to one short line and marks where it can appear, so the agent treats it as data. An agent can still be misled by what it reads.

## Deleting your data

- One record: `byobot erase <kind> <id>`.
- Everything on a computer: delete the `~/.byobot` folder. On the iPhone: delete the app.
