# Agent conventions

Conventions for any coding agent working in this repository. This file is the only agent instruction file.

## Layout

- `python/src/byobot/` — Python package: `commands.py` (registry), `cli.py` (CLI, JSON on stdout), `mcp.py` (JSON-RPC on stdio), `serve.py` (LAN HTTPS sync server, port 8765, cert pinning + QR pairing), `store.py` (SQLite store), `contract.py` (loads `schema/v0`, validates documents).
- `apps/ios/` — iPhone app; logic lives in the ByobotKit Swift package. `project.yml` is the XcodeGen spec.
- `schema/v0/` — the v0 JSON Schema contract; fixtures in `schema/fixtures/v0/`.
- `library/` — shipped activity library: `activities/<id>.json` plus `vocabulary.json`, loaded by `library.py` (`BYOBOT_LIBRARY` overrides the path) and layered under ledger-defined activities. Names are lowercase with underscores.
- `tools/gen_swift.py` — generates `Generated/Contract.swift` from `schema/v0`.

## Setup and tests

```bash
cd python && pip install -e ".[test]" && pytest
cd apps/ios/ByobotKit && swift test
```

CI (`.github/workflows/ci.yml`) runs the Python tests, contract check, wheel smoke test, and iOS build on push to main and on PRs.

After a schema change regenerate Swift from the repo root: `python tools/gen_swift.py` (`--check` fails if the committed file differs).

## Contract first

`schema/v0` is the source of truth. A schema change ships with fixtures (add a case when a rule gets tighter or looser), regenerated Swift, and Python + Swift tests. Never hand-edit `Generated/Contract.swift`; change the schema and regenerate. `SCHEMA_VERSION` in `contract.py` is currently `0`, separate from the store version.

## Store

`events` is an append-only SQLite log (update/delete triggers abort); telemetry sources stay in `raw_records`. Everything else is derived and rebuilt from those logs (`Store.rebuild` / the `rebuild` command); `sync_peers` is not derived and survives rebuilds. A changed library also rebuilds the ledger on open.

The on-disk version is SQLite `PRAGMA user_version` vs `STORE_VERSION` in `store.py` (currently `4`): a newer database raises `RuntimeError`; version `0` creates the ledger and telemetry tables; an older version rebuilds the derived ledger only, leaving `events`, `raw_records`, `telemetry_samples`, and `sync_peers` in place. Bump `STORE_VERSION` when a derived-ledger change must apply to existing databases.

## Contributing

- Branch names: `type/short-description`, with type one of `feature`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `ci`, `build`, `style`, `revert`, `release`. Branches named by bots and agent platforms (`claude/...`, `dependabot/...`) are exempt.
- Commit messages: one line; attribute the agent at the end with `[agent]` (e.g. `... [claude]`, `... [muse]`).
- Pull requests: small, with tests; description is one summary line, at most three short bullets, one line on testing. Agents open PRs; merging, force-pushing, and releasing are human-only.

## Privacy

Use only example data in fixtures and tests: device `Example iPhone`, hostname `example-laptop`, addresses in `192.0.2.0/24`, location id `example-gym`. Keep real personal names, device names, hostnames, IPs, places, and health data out of fixtures, tests, commit messages, and PRs.
