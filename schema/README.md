# The byobot contract

JSON Schemas (draft 2020-12) for everything byobot stores and exchanges. Every implementation, in any language, validates against these files and passes the same fixtures. Each schema documents its own fields.

| File | Defines |
|---|---|
| `v0/event.schema.json` | The event envelope and which payload each event type carries |
| `v0/training.schema.json` | Activities, locations, plans, planned and performed sessions and efforts |
| `v0/telemetry.schema.json` | Telemetry samples |
| `v0/common.schema.json` | Ids, times, dates and the metrics registry |
| `v0/sync.schema.json` | The LAN sync messages between `byobot serve` and the app |
| `fixtures/v0/` | Conformance cases every implementation must pass |

## Rules beyond the schemas

- Fixture cases that break one of these name it in `rule`, and must be rejected:
  - `range-order`: in any metric, `min <= max`.
  - `date-order`: a plan's `starts_on <= ends_on`.
  - `time-order`: a session's `started_at <= ended_at` and a sample's `start <= end`, compared as instants.
- Events apply in order of `ts` as an instant, then `device_id`, then `id`. The latest wins; arrival order is only an audit trail.
- Defining or logging a record again with the same id (an activity, plan, session or effort) replaces it. `event.undone` undoes an event, and undoing an undo restores it.

## Changing the contract

Add a fixture case whenever a rule gets tighter or looser, then regenerate Swift and run both test suites (see [`AGENTS.md`](../AGENTS.md)). `schema_version` stays `0` until 1.0; after that a breaking change gets a new version directory.
