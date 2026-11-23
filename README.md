<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/byobot_banner_dark.svg">
  <img src="docs/assets/byobot_banner_light.svg" alt="byobot" width="100%">
</picture>

byobot is an open source personal health OS. One local database for all your health data, from any device, and a CLI and MCP server so the agent of your choice can read it, write to it, and coach you.

- **Your byobot outlives your devices.** Change watches, scales or phones and your history stays in one standardized store on your machine.
- **The agent of your choice is your coach.** Claude, ChatGPT, a local model or your own script: anything that speaks MCP or runs a command can read your data and write a plan.
- **Log on your phone, keep it on your laptop.** The iPhone app pulls the week your agent planned from your laptop over your local network, logs each set offline, and sends it back. No cloud account, and health data never goes to iCloud.

> **Status: pre-alpha.** The data contract, the Python core, LAN sync (`byobot serve`) and a first iPhone app are here. Importers (WHOOP, Hume and others, built on upstream open source connectors) come next. Nothing is published to PyPI or the App Store yet.

## Install

From inside a checkout (Python 3.10 or newer), install the package at `python/`:

```bash
git clone https://github.com/byobot/byobot.git
cd byobot
pipx install ./python        # or: pip install ./python
```

## Quickstart

```bash
byobot init                                   # creates ~/.byobot/byobot.db

byobot log activity.defined '{"id": "barbell_back_squat", "name": "Barbell back squat", "tags": ["strength"]}'
byobot log effort.performed '{"activity_id": "barbell_back_squat", "metrics": {"reps": {"value": 5}, "load": {"value": 100, "unit": "kg"}, "rpe": {"value": 8}}}'
byobot log effort.performed '{"activity_id": "couch_stretch", "side": "left", "metrics": {"duration": {"value": 90, "unit": "s"}}}'

byobot list efforts
byobot query "SELECT activity_id, reps, load_kg, rpe FROM strength_sets"
```

Commands print JSON; bare `byobot` prints help, and `byobot mcp` and `byobot serve` keep running until stopped. Set `BYOBOT_HOME` (or pass `--home`) to keep the store somewhere else.

### Connect your agent

`byobot mcp` serves the same commands over MCP on stdio. For Claude Code:

```bash
claude mcp add byobot -- byobot mcp
```

For any client that takes a JSON config:

```json
{ "mcpServers": { "byobot": { "command": "byobot", "args": ["mcp"] } } }
```

Writes made over MCP are recorded with the agent as their author (its client name, and its model when it says).

### Pair your phone

```bash
byobot serve        # HTTPS on your local network, port 8765 (--port to change); shows a QR code until a phone is paired
```

Scan the QR code with the iPhone's Camera app to open the byobot app and pair. The app pins the laptop's certificate, so it only talks to the laptop you paired. `byobot devices` lists paired phones and `byobot unpair DEVICE_ID` revokes one. See [`apps/ios`](apps/ios) to build the app.

## Commands

The CLI and the MCP server are generated from one command registry, so people and agents get the same commands and the same results.

| Command | What it does | MCP |
|---|---|---|
| `init` | Create the local store. Safe to run again. | |
| `log TYPE JSON` | Record an activity, a location (`location.defined`), a plan, a planned session, a performed session or a performed effort. | ✓ |
| `correct EFFORT_ID JSON` | Fix a logged effort (metrics, side, notes), keeping when it was done. | ✓ |
| `import-activities @FILE` | Import your own activities with muscles and equipment, on top of the library byobot ships. | ✓ |
| `undo EVENT_ID` | Undo an event (the undo is itself an event). | ✓ |
| `ingest JSON` | Add telemetry samples. Re-ingesting is a no-op. | ✓ |
| `list KIND` | Recent events, activities (`--source library` or `ledger`), locations, plans, planned sessions, sessions, efforts or telemetry. | ✓ |
| `query SQL` | One read-only SQL statement. | ✓ |
| `schema [NAME]` | The JSON Schemas, or the SQL tables (`schema sql`). | ✓ |
| `validate JSON` | Check a document against the contract. | ✓ |
| `status` | Where the store is and what it holds. | ✓ |
| `rebuild` | Rebuild every derived table from the logs. | |
| `devices` | Phones paired with `byobot serve`. | ✓ |
| `unpair DEVICE_ID` | Revoke a paired phone. | |
| `mcp` | Serve the commands above to an agent. | |
| `serve` | Sync with the iPhone app over your local network. | |

JSON arguments take literal JSON, `@path/to/file.json` (JSON or JSON lines), or `-` for stdin. `--actor KIND:ID[:MODEL]` (or `BYOBOT_ACTOR`) says who is acting; the default is `human:me`.

## How your data is kept

- **The contract** in [`schema/`](schema) is plain JSON Schema with shared test fixtures. Any implementation, in any language, must pass the same cases.
- **Exercises ship with byobot.** The [activity library](library) gives every store a set of exercises with the muscles they target, the equipment they need, and how they train. An activity you define with the same id replaces the library's for you. To add one for everyone, open a pull request; see [`library/README.md`](library/README.md).
- **One shape for anything physical.** A lift, a run, a stretch, a climb or a tennis match is an activity; doing it is an effort with an open map of metrics (reps, load, duration, distance, pace, RPE, or your own `x.` metric). Sessions group efforts into blocks with rounds, which covers sets, supersets, circuits, intervals and EMOMs.
- **Ledger:** what someone authored (activities, locations, plans, planned sessions, performed sessions and efforts) is an append-only log of events. Each event records who wrote it (you, an agent and its model, or a connector), from which device, and when. Undo is an event, so nothing is lost.
- **Latest time wins.** Events apply in order of their own timestamp, with the device id breaking exact ties, so a phone that syncs late can't overwrite something newer from your laptop. The order events arrived in is kept only as an audit trail.
- **Plan versus actual is first class.** Plans and logs share one shape. A performed effort can point at the planned effort and round it answers; the `plan_vs_actual` view lines them up, and `strength_sets` gives reps and load in plain columns. Loads are compared only within one location.
- **Telemetry:** device measurements are keyed on (source, source id), so importing the same export twice changes nothing. The source's raw record is kept alongside.
- **Rebuildable:** the tables you query are derived from the event log and raw records, and `byobot rebuild` recreates them. Every row says where it came from.

## Repository layout

```
AGENTS.md    conventions for contributors and coding agents
schema/      the v0 contract (JSON Schema) and shared fixtures
library/     the activity library that ships with byobot
python/      the Python package: store, command registry, CLI, MCP server, LAN sync
apps/ios     the iPhone app (SwiftUI) and ByobotKit, its tested logic
tools/       gen_swift.py, which writes the app's Swift types from the contract
docs/assets  the mascot
```

## Development

Conventions for contributors and coding agents are in [`AGENTS.md`](AGENTS.md).

```bash
cd python
pip install -e ".[test]"
pytest
```

The app's logic tests with `swift test` in `apps/ios/ByobotKit` (macOS). After changing the contract, run `python tools/gen_swift.py` to regenerate the Swift types; CI fails if they are out of date.

## Versions

Python is `0.1.0.dev0` (`python/pyproject.toml` and `byobot.__version__`). The iPhone app is marketing version `0.1.0`, build `1` (`MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` in `apps/ios/project.yml`). Apple requires a numeric marketing version, so the app does not use the Python `.dev0` suffix. The contract `schema_version` is `0` (`SCHEMA_VERSION` in `python/src/byobot/contract.py`). The on-disk store version is `4` (`STORE_VERSION` in `python/src/byobot/store.py`). The sync protocol is `0`, the `/v0` routes (`PROTOCOL` in `python/src/byobot/serve.py`, `ContractVersion.sync` in the app); it is versioned separately from `schema_version`.

## License

Apache-2.0. Copyright The byobot Authors.
