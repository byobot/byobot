# AGENTS.md

## Commands

```bash
cd python && uv sync --extra test        # CI installs from uv.lock; run `uv lock` after changing dependencies
uv run python -m pytest                  # from python/
uv run python -m pytest tests/test_core_store.py -k NAME
python tools/gen_swift.py                # from the repo root, after any schema change; --check verifies
cd apps/ios/ByobotKit && swift test      # macOS only
```

CI runs all of these. A change is done when they pass.

## Rules

- `schema/v1` is the source of truth. A schema change ships with fixtures in `schema/fixtures/v1`, regenerated Swift, and Python and Swift tests.
- Never edit `apps/ios/ByobotKit/Sources/ByobotKit/Generated/Contract.swift` by hand.
- The `events` table, the byobot logbook, is append-only; erase only blanks payloads.
- Derived tables rebuild on open when their DDL changes; raise `RULES` in `core/reducers.py` when records are derived differently. Never bump `STORE_VERSION` for this: byobot refuses every store written at an older one.
- Library activity ids never change or go away. Names are lowercase with underscores; see `library/README.md`.
- Ask first before changing `SCHEMA_VERSION`, `STORE_VERSION` or the sync protocol.
- One logical change per commit, messages as Conventional Commits (see `CONTRIBUTING.md`). Minimal comments.
- Merging, force-pushing and releases are for maintainers.

## Versions

Each marker guards one boundary. Most changes move none of them.

| Marker | Where | Guards | Moves when |
|---|---|---|---|
| `SCHEMA_VERSION` | `core/contract.py`, each event's `schema_version` | the event envelope | the envelope changes; events of a later one are kept and relayed, never applied |
| kind versions | `schema/v1/registry.json`, each event's `kind_version` | one kind's payload | that kind's payload changes; an app can't change a record written at a version it doesn't know |
| `RULES` | `core/reducers.py` | how records are derived | derivation changes; derived tables rebuild on open |
| `STORE_VERSION` | `core/store.py` | the SQLite file | its tables change in a way a rebuild cannot fix; older stores are refused |
| `PROTOCOL` | `serve.py` | the hub's HTTP API | an endpoint changes incompatibly |
| `FORMAT` | `cloud/crypto.py` | every Drive file | a Drive file changes incompatibly |
| `EXPORT_VERSION` | `core/store.py` | the export file | the export file changes |
| `v` | `link.py` | the `byobot://pair` link | the link changes |
| `capabilities` | hub hello and pair, Drive device and epoch files | optional features | a feature is added without a version bump |

## Privacy

This repo is public and handles health data. Use only example data: device `Example iPhone`, hostname `example-laptop`, addresses in `192.0.2.0/24`, location id `example_gym`. No real names, emails, devices, hostnames, IPs, places or health data in code, tests, commits, commit metadata, branch names or PRs. Commit with a noreply email.
