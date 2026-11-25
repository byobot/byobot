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
