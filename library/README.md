# The activity library

The exercises that ship with byobot. Every byobot store sees them as activities, so a plan can use them, the app can suggest swaps between them, and your agent can read how each one trains, with nothing to import.

## How it fits with your own activities

- **Shipped, not copied.** The library isn't written to your event log. byobot reads it from the installed package, so fixes reach you when you upgrade.
- **Yours win.** An activity you or your agent define with the same id (`byobot log activity.defined ...`) replaces the library's one for you, and undoing it brings the library's back. Your own exercises live in your log the same way and sync to your phone.
- **Ids are permanent.** Plans and logged sets point at an activity id forever, so a library id never changes or goes away.

`byobot list activities --source library` lists the library; `--source ledger` lists your own.

## File format

One file per activity in `activities/`, named by its id, holding an `activity.defined` payload (see `schema/v0/training.schema.json`):

```json
{
  "id": "example_curl",
  "name": "Example Curl",
  "equipment": ["dumbbell"],
  "movement_type": "isolation",
  "fatigue": 1,
  "resistance_curve": { "stretch": 1, "middle": 3, "contract": 1 },
  "targets": { "main": ["biceps"], "synergists": [] }
}
```

- Ids and names of muscles and equipment are lowercase with underscores.
- `targets`, `equipment` and `movement_type` use the names in `vocabulary.json`. Add a name there, in sorted order, when an exercise needs a new one. Your own activities can use any name.
- `movement_type` is `compound` (several joints) or `isolation` (one joint). How hard it is goes in `fatigue`, from 1 to 4, and each part of the `resistance_curve` is 1 to 3.

## Adding an exercise to byobot

Open a pull request that adds its file. CI (`python/tests/test_library.py`) checks that it matches the contract, that the file name is its id, that its muscles, equipment and movement type are in `vocabulary.json`, that no muscle is both main and synergist, and that no two exercises share a name. Only add ratings and descriptions that are your own work.
