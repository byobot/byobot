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
byobot log effort.performed '{"activity_id": "barbell_back_squat", "metrics": {"reps": {"value": 5}, "load": {"value": 100, "unit": "kg"}}}'
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
