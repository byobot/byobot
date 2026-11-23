# The byobot contract

These JSON Schemas (draft 2020-12) define what byobot stores and exchanges. They are the source of truth: the Python package bundles them, and any other implementation (the app, a future Rust core, a connector) validates against the same files.

| File | Defines |
|---|---|
| `v0/event.schema.json` | The event envelope (id, schema version, type, timestamp, device, actor, payload) and which payload each event type carries. |
| `v0/training.schema.json` | Activities, locations, plans, planned sessions (blocks of efforts), performed sessions and performed efforts. |
| `v0/telemetry.schema.json` | Telemetry samples: body mass, HRV, resting heart rate and sleep. |
| `v0/common.schema.json` | Shared pieces: ids, timestamps, dates, and the metrics registry. |
| `v0/sync.schema.json` | The LAN sync protocol between `byobot serve` and the app. |
| `fixtures/v0/cases.json` | Conformance cases. Each names a schema, a document and whether it must be accepted. |
| `fixtures/v0/sync.json` | Sync message cases, and pairing links with what they must parse to. |
| `fixtures/v0/library.json` | A small activity library in the format `byobot import-activities` reads. |

## Rules of the road

- **Every implementation passes every fixture.** Add a case whenever you tighten or loosen a rule.
- **Times are RFC 3339 with an explicit offset.** Patterns enforce this, not only `format`, because most validators treat `format` as a hint.
- **Unknown fields are rejected.** A typo should fail loudly rather than vanish.
- **Telemetry types use HealthKit identifiers** where HealthKit has one. Where it doesn't, the type is namespaced `byobot.` (for example `byobot.HeartRateVariabilityRMSSD`, which is what WHOOP reports; HealthKit's HRV type is SDNN, a different measure). Each type has one canonical unit.
- **Versioning.** `schema_version` is `0` while the contract settles. Breaking changes after 1.0 get a new version directory; stores keep reading old versions.

## Training model (v0)

One shape covers anything physical, planned or done.

- **Activity:** what you do, with an id, a name, free tags and the metrics it is usually measured by. No fixed list of modalities: a back squat, a 400 m repeat, a couch stretch and a bouldering problem are all activities. Optionally, the muscles it `targets` (`main` and `synergists`) and the `equipment` it needs, which is how an app suggests a swap, and how it trains: `movement_type`, `fatigue` (1 to 4) and `resistance_curve` (1 to 3 at the `stretch`, `middle` and `contract`), which an agent can use to balance a week.
- **Planned session:** ordered **blocks**, each run for some number of **rounds** (default 1), each holding ordered **efforts**. Straight sets are one effort for 3 rounds; a superset or circuit is several efforts in a block; intervals put the work and the recovery in one block; an EMOM is a block of 10 rounds with a duration of 60 s.
- **Effort:** an activity, an optional side (`left`, `right`, `both`) and a **metrics** map.
- **Performed session and effort:** the same shape as the plan. A performed effort can name the `planned_effort_id` and `round` it answers. A whole session can also be logged as one effort (a tennis match), with totals in the session's metrics.

### Metrics

A metric is `{"value": ...}` or a range `{"min": ..., "max": ...}` (either bound alone is fine, so "at least 8 reps" is `{"min": 8}`), plus a `unit` where the metric has units. Values are numbers, or strings and booleans for custom metrics. Block metrics apply to each round.

| Metric | Units |
|---|---|
| `reps` | none (whole number) |
| `load` | `kg`, `lb`, `%1rm` |
| `duration`, `rest` | `s`, `min` (`duration` also `h`) |
| `distance` | `m`, `km`, `mi`, `yd`, `ft` |
| `pace` | `s/km`, `s/mi`, `s/100m`, `s/500m` |
| `heart_rate` | `bpm` |
| `power` | `W` |
| `rpe` | none (0 to 10) |
| `rir` | none |
| `tempo` | none (a string such as `3010`) |
| `incline` | `%`, `deg` |

Anything else is a custom metric whose name starts with `x.`, for example `{"x.grade": {"value": "V4"}}`. Unknown names without the prefix, unknown units and units on unitless metrics are rejected. Stores keep the value as written and also a standard unit (kg, s, m, s/km) for comparing across units.

### Muscles and swaps

byobot ships an activity library (`library/` at the repository root) with each exercise's targets, equipment and ratings; every store sees it, and an activity you define with the same id replaces it. Import more of your own with `byobot import-activities @file.json` (an object with an `activities` list, or a bare list). The app gets every activity with the plan, so swaps work offline. Muscle and equipment names are free slugs; the library's are in `library/vocabulary.json`.

A swap needs no event of its own. A performed effort that names a `planned_effort_id` but a different `activity_id` answered the plan with another activity; `plan_vs_actual` shows it as `swapped`, and the effort's notes say why. The order you did things in is the order of `performed_at`.

### Locations

A **location** is where you train, such as a gym, with an id (`home`, `example-gym`) and a name. The same plates and machines can feel different from one gym to the next, so loads compare only within one location.

- A planned session's `location_id` is where its loads were planned for.
- A performed session's `location_id` is where it happened, and its efforts take it.
- An effort's own `location_id` overrides its session's, and places an effort logged on its own.

The `strength_sets` and `plan_vs_actual` views give each set's location, so progression is a `GROUP BY activity_id, location_id`. Sets without a location happened somewhere unrecorded.

## Event types (v0)

| Type | Payload |
|---|---|
| `activity.defined` | An activity. Defining the same id again replaces it. |
| `location.defined` | A location. Defining the same id again replaces it, so renaming keeps its history. |
| `plan.defined` | A plan. Defining the same id again replaces it. |
| `session.planned` | A planned session with its blocks and efforts. Planning the same id again replaces them. |
| `session.performed` | A performed session, with its totals and optionally its efforts inline. Logging the same id again replaces those inline efforts. |
| `effort.performed` | One performed effort, optionally linked to its session and to the planned effort and round it answers. Logging the same id again corrects it: the new version replaces the old one whole. |
| `event.undone` | Undoes an earlier event. Undoing an undo restores the original. |

## Rules beyond the schema

JSON Schema cannot compare two fields, so implementations check these too. Fixture cases that break one name it in `rule`, and must be rejected:

- `range-order`: in any metric, `min <= max`.
- `date-order`: a plan's `starts_on <= ends_on`.
- `time-order`: a session's `started_at <= ended_at` and a sample's `start <= end`, compared as instants across offsets.

## Order of events

Events apply in order of their own `ts`, compared as instants across offsets; the latest wins. When two events have the same instant, the one with the greater `device_id` applies later, then the greater `id`. The order in which a store received events is kept only as an audit trail. So an event that arrives late (a phone that was offline) slots into its place in time, and a store that receives one older than what it has already applied replays its ledger.

Ids are UUIDv7 and, within one device, increase in the order they were made, so a device's events at the same millisecond keep their order.

### Corrections

To fix a logged effort, log it again with the same `id`, the full effort, and its original `performed_at`, `round` and `planned_effort_id` (unless those are what you're fixing). `performed_at` stays the time the set happened; the event's own `ts` is when the correction was made, so the correction is the newer event and wins. Undoing the correction brings back the version before it. `byobot correct EFFORT_ID CHANGES` does this for you.

## Sync (v0)

`byobot serve` is the hub; the app pulls and pushes, over HTTPS on the local network with the laptop's self-signed certificate pinned by its SHA-256 fingerprint.

1. **Pair.** `byobot serve` shows `byobot://pair?v=0&host=...&port=...&code=......&fp=...` as a QR code (`fp` is the certificate fingerprint, base64url). The app calls `GET /v0/hello`, then `POST /v0/pair` with the one-time six-digit code and its own device id, and keeps the token it gets back. Codes expire after ten minutes or five wrong tries.
2. **Pull.** `GET /v0/plan?from=&to=` returns activities, plans and planned sessions as their current events wrote them, plus efforts already logged against those sessions.
3. **Push.** `POST /v0/events` takes events the app wrote. Each must carry the paired device's id and the actor `human` / `me`; a different device or actor is rejected. Each gets `accepted`, `duplicate` (the store already has that exact event) or `rejected` (will not change on retry). `POST /v0/telemetry` takes samples such as body mass read from HealthKit.

Every call but `hello` and `pair` sends `Authorization: Bearer <token>`. A `401` means the laptop revoked the phone (`byobot unpair`), and the app should pair again.

## Not yet covered

That referenced ids exist (an effort's activity or planned effort) is not checked. Sync is phone to laptop only; two laptops don't sync with each other yet.
