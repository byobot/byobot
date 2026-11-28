# The byobot contract

JSON Schemas (draft 2020-12) for everything byobot stores and exchanges. Every implementation, in any language, validates against these files and passes the same fixtures.

| Directory | Defines |
|---|---|
| `v1/` | Schema 1: the byobot logbook's events, records, registry and sync protocol 1; see [`v1/README.md`](v1/README.md) |
| `fixtures/v1/` | Conformance cases every implementation must pass |
