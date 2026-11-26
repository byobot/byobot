<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/byobot_banner_dark.svg">
  <img src="docs/assets/byobot_banner_light.svg" alt="byobot" width="100%">
</picture>

an open source personal health operating system. you own all your own data locally. bring any agent.

byobot keeps your training and health data in one SQLite database on your machine. Any agent can read it and write plans to it through a CLI or an MCP server, and an iPhone app logs workouts offline and syncs with your laptop over your local network.

Pre-alpha: not yet on PyPI or the App Store.

## Quickstart

Needs Python 3.10 or newer.

```bash
git clone https://github.com/byobot/byobot.git
pip install ./byobot/python

byobot init    # creates ~/.byobot/byobot.db
byobot log effort '{"activity_id": "barbell_back_squat", "metrics": {"reps": {"value": 5}, "load": {"value": 100, "unit": "kg"}}}'
byobot query "SELECT activity_id, reps, load_kg FROM strength_sets"
```

`byobot` lists every command. Each prints JSON.

## Connect an agent

```bash
claude mcp add byobot -- byobot mcp
```

Other MCP clients: `{"mcpServers": {"byobot": {"command": "byobot", "args": ["mcp"]}}}`

To have it plan your week, run its `weekly_plan` prompt (`/mcp__byobot__weekly_plan` in Claude Code).

## More than one person on a computer

Each person uses their own profile, with its own store, WHOOP sign-in, phone pairing and agent:

```bash
byobot --profile alex init
byobot --profile alex serve
claude mcp add byobot-alex -- byobot --profile alex mcp
```

Or set `BYOBOT_PROFILE=alex`. Without a profile, byobot uses the default one.

## Use it from an assistant on your phone

`byobot remote` serves the same tools over the internet, with sign-in, to assistants that take a remote MCP server (such as custom connectors in Claude). It listens on this computer only, so give it an https address with a tunnel, for example [Tailscale Funnel](https://tailscale.com/kb/1223/funnel). The computer must be awake.

```bash
tailscale funnel --bg 8780    # shows your address, such as https://example-laptop.example.ts.net
byobot remote --url https://example-laptop.example.ts.net
```

Add `https://example-laptop.example.ts.net/mcp` as a custom connector, and enter the code `byobot remote` shows when it asks. Each profile gets its own port and address: the next one is 8781, behind `tailscale funnel --bg --https=8443 8781`. `byobot remote --revoke` disconnects every assistant.

For an assistant that calls an API instead, run `byobot remote --api-key NAME` for a key (add `--copy` to put it on the clipboard instead of the screen), and point it at `https://example-laptop.example.ts.net/api/openapi.json`. Each tool is `POST /api/tools/TOOL` with its arguments as JSON and the key as `Authorization: Bearer KEY` or `X-API-Key: KEY`.

## Pair the iPhone app

Run `byobot serve` and scan the QR code it shows. To build the app, see [`apps/ios`](apps/ios).

## Sync through Google Drive (optional)

When the phone can't reach your computer, it can sync through a hidden folder in your own Google Drive instead. Everything there is encrypted with keys only your devices hold. Your computer stays the one copy that counts; the folder is only a mailbox.

You use your own Google Cloud project, once:

1. Make a project at [console.cloud.google.com](https://console.cloud.google.com) and turn on the Google Drive API.
2. Set up the OAuth consent screen as External, with only the `drive.appdata` scope, then press Publish app so it says In production. In Testing, Google signs every device out after seven days.
3. Make two OAuth clients: a Desktop app (download its JSON) and an iOS client for the app's bundle id.

```bash
byobot cloud setup     # asks for the Desktop client's JSON and the iOS client id, signs you in, shows a recovery code
byobot cloud join      # shows a code; in the app, turn on Drive sync and scan it
byobot cloud status    # devices, when each was last heard from, the folder's size, any error
```

`byobot serve` syncs with Drive every minute, and any command that reads data syncs first. `byobot cloud off` stops; `--delete yes` also deletes the folder's files, leaving one small signed note that tells phones Drive sync ended. Keep byobot's home out of any folder another app syncs (Google Drive for desktop, iCloud Drive, Dropbox): `byobot status` warns if it is in one.

Erasing a record with Drive sync on rewrites the folder under a new key. Once every device has moved to it, the old files and keys are deleted outright, so the record is gone from Drive's folder and from every device that synced. What Google keeps on its own systems after a delete isn't documented, but anything left is encrypted with a key that only a device you removed with `byobot cloud remove` may still hold. `byobot cloud status` names any device the cleanup still waits on, and `byobot cloud remove` retires one that won't come back. Exports made before the erase still hold the data.

## Import from WHOOP

byobot talks to WHOOP's API directly, through your own app. Create one at [developer.whoop.com](https://developer.whoop.com) with the scopes `read:recovery`, `read:cycles`, `read:sleep` and `read:workout`, and the redirect URL `http://localhost:8766/whoop/callback`. Then:

```bash
byobot connect whoop    # once; asks for the app's client id and secret
byobot import whoop     # first run goes back 90 days, then picks up where it left off
```

## More

- [`schema/`](schema): the data contract
- [`library/`](library): the exercises byobot ships
- [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)

## License

[Apache-2.0](LICENSE)
