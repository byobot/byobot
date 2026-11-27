# AGENTS.md

## Commands

```bash
cd python && pip install -e ".[test]"
pytest                                   # from python/
pytest tests/test_core_store.py -k NAME  # one test
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

## Privacy

This repo is public and handles health data. Use only example data: device `Example iPhone`, hostname `example-laptop`, addresses in `192.0.2.0/24`, location id `example-gym`. No real names, emails, devices, hostnames, IPs, places or health data in code, tests, commits, commit metadata, branch names or PRs. Commit with a noreply email.
