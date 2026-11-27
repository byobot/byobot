# The byobot contract, v1

Schema 1: the byobot logbook, one append-only log of events, each writing one record, written by people, agents and sources alike. Everything you read is computed from it.

| File | Defines |
|---|---|
| `event.schema.json` | The envelope, its three ops, and which payload each known kind carries |
| `records.schema.json` | The header and each kind's payload at kind version 1 |
| `common.schema.json` | Ids, times, refs, measures |
| `registry.schema.json`, `registry.json` | Kinds, settings, derived-id namespaces and codes |
| `sync.schema.json` | Sync protocol 1, between `byobot serve` and the app |
| `cloud.schema.json` | Cloud protocol 1: the same messages as encrypted, signed files in the person's own cloud folder |
| `../fixtures/v1/` | Conformance cases every implementation must pass |

## Records and ids

A record is `kind` + `id`. Each event writes exactly one; its type is `<kind>.<op>`, such as `effort.put`.

| Kinds | Record id |
|---|---|
| `activity`, `location`, `setting` | A slug. Library activity ids never change. |
| `plan`, `planned_session`, `session`, `effort`, `observation` | A UUIDv7, minted when the record is created. |
| `source_record`, `overlay` | A UUIDv5 derived as below, so two devices produce the same id. |

Derived ids are UUIDv5 over the [RFC 8785](https://www.rfc-editor.org/rfc/rfc8785) canonical JSON of an array, with the namespace from `registry.json`:

- `source_record` record: `["<source>", "<source_type>", "<source_id>"]` in `source_record`.
- Its `put` event: `["<record id>", "<mac>"]` in `source_revision`, where `mac` is the lowercase hex HMAC-SHA256 of the canonical JSON of the payload, keyed by the store key.
- Its `retract` event (deleted at the source): `["<record id>", "<prev[0]>"]` in `source_deletion`, or `["<record id>"]` with an empty `prev` when the deletion came before any revision. That one hides the record for good.
- `overlay` record: `["<effect>", "<target kind>", "<target id>"]` in `overlay`, so there is one overlay per effect and target.

The store key is 32 random bytes created with the store. It never enters the logbook; export and restore carry it, and paired devices receive it. It keeps an erased source payload from being recovered by hashing guesses against event ids.

## Order and time

- Events order by `hlc`, then `id`. A writer sets `hlc` to the larger of its wall clock in ms and the highest `hlc` it has seen, raising the counter when the milliseconds don't move. Receiving an event raises the highest seen.
- `ts` is the author's wall time, for display. Neither `ts` nor arrival order decides anything.
- Instants compare as instants, never as text. A record's local date comes from its own offset (or its `zone`); anything without one uses the `home_zone` setting, never the reading machine's zone.

## Writes and conflicts

- `prev` lists the events of the record the author saw as current. A record's heads are its events no other event of it names in `prev`. Two or more heads with different content are a conflict: reads show the head with the highest `hlc` and `id`, flagged, counted once. An ordinary write names the one version it was made from, so a conflict stays until a resolve names the heads the person saw.
- `retract` removes a record and conflicts like any edit.
- Undo is a command, not an op: it writes the record's previous content (or a `retract` if the undone event created it) with `reverts` set and `prev` naming the undone event, which must be the record's only head.
- `erase` always wins and never conflicts. Every replica then blanks the payload of each event of that record, sets `redacted_by`, and keeps ids and order so the logbook still replays. An event of an erased record arriving later is stored blanked. Nothing derived from an erased payload's content may remain, except ids made with the store key.
- Source records never conflict. Only connectors `put` or `retract` them, with `prev` empty on a `put` and naming the deleted revision on a `retract`. The current revision is the latest by the time its mapper reads from `raw`, then event id; a `retract` hides the record only while the revision it names is current. Your changes to a source's record are overlays.

## Delivery

Each delivered event gets one outcome:

- `duplicate`: the id is already held. For a source record's event the id commits to its content, so differing `ts`, `hlc`, `device_id`, `actor` or `batch` don't matter; for any other event, the same id with different content is `rejected`.
- `stored_unprojected`: a valid envelope of a kind, kind version or setting this replica doesn't know. It is kept, relayed and read after an upgrade.
- `stored_unprojected` too: an event of a later `schema_version` whose `id`, `record`, `op`, `hlc`, `prev` and `payload` still parse (`$defs/later` in `event.schema.json`). It is kept and relayed exactly as received and never applied, except that an `erase` is honored. Its record can't be changed until an upgrade.
- `rejected`: a malformed envelope, a payload that breaks its kind's schema, or a rule below. Never for arrival order: an event whose `prev`, `reverts` or links name something not yet here is accepted.
- `accepted`: anything else.

A writer stamps a `put` with the lowest kind version that holds its payload: at least the version that last reshaped the kind (`reshaped` in the registry), and the version that added each field it uses (`since`). Sync servers and apps exchange the kind versions they know (`kinds` in `hello` and pushes), plan entries say the version their payload needs, and a server rejects a pushed `put` on top of a version the app doesn't know, so an older app never drops fields it can't see.

Codes are open too. A code, unit or category value this replica's registry doesn't list is kept as written and not converted. Commands refuse them when writing; writers use registry codes or their own `x.` ones.

## Rules beyond the schemas

Fixture cases that break one of these name it in `rule`, and must be rejected:

- `time_order`: `time.start <= time.end`, as instants.
- `date_order`: a plan's `starts_on <= ends_on`.
- `range_order`: in a measure, `min <= max`, and an estimate's `value` lies within its bounds.
- `code_value`: a known code's value has its type (a quantity carries a unit, others don't), matches its `pattern`, and lies within its bounds once converted to its unit.
- `part_ids`: block and planned effort ids are unique within a planned session.
- `self_reference`: an event's `prev` and `reverts` don't name the event itself.
- `derived_id`: a source record's or overlay's record id is the one derived above.
